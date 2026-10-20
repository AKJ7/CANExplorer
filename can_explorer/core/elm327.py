"""ELM327 and OBDLink adapters: a python-can bus (interface "elm327", class Elm327Bus) for OBD dongles with the
ELM327 command set, such as the OBDLink MX+, LX, SX and EX (STN chips) and ELM327 compatible adapters.

The channel is the serial port: a USB port (`COM4`, `/dev/ttyUSB0`), the serial port of a paired Bluetooth
adapter (`COM7`, `/dev/rfcomm0`, `/dev/cu.OBDLinkMXP-STN-SPP`), or a WiFi adapter with pyserial's URL syntax
(`socket://192.168.0.10:35000`). Bluetooth Low Energy adapters (OBDLink CX) are not supported.

These adapters are made for diagnostics, not as CAN interfaces, so the bus is limited:
- The adapter monitors the bus (`AT MA`) and prints every frame as text; the serial link carries a few hundred
  frames per second, so frames are lost on busy buses (the adapter reports BUFFER FULL).
- Sending a frame stops monitoring, sets the header and sends the data; frames on the bus in the meantime are lost,
  and a frame takes tens of milliseconds. Responses to it are received.
- Classic CAN with 1 to 8 data bytes at 500 kbit/s divided by a whole number (500, 250, 125, 100, 83.3, 50 …).
  No CAN FD, remote frames to send or error frames; timestamps are taken on arrival at the computer.

Protocol: commands are text lines ending in CR, answered by lines and the `>` prompt; any character interrupts
monitoring. The bus runs on the user protocol B (`AT PB`), which accepts any 500 kbit/s divisor and both identifier
lengths; adapters without `AT PB` fall back to the OBD protocols 6 to 9 (500 and 250 kbit/s). With headers, DLC and
spaces on and CAN formatting off, frames are printed as `7E8 8 06 41 00 BE 3F A8 13 00` or
`18 DA F1 10 8 03 41 0D 00 00 00 00 00`.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import can

logger = logging.getLogger(__name__)

BASE_BITRATE = 500_000  # Protocol B bitrates are 500 kbit/s divided by 1 to 64
MAX_DIVISOR = 64
# OBD protocols for adapters without AT PB: bitrate → (11 bit, 29 bit)
STANDARD_PROTOCOLS = {500_000: ('6', '7'), 250_000: ('8', '9')}
PB_RECEIVE_BOTH, PB_VARIABLE_DLC, PB_TRANSMIT_11_BIT = 0x20, 0x40, 0x80
SERIAL_BAUDRATES = (115_200, 38_400, 9_600)  # OBDLink USB adapters use 115200, ELM327 compatibles 38400 or 9600
INIT_TIMEOUT = 3.0  # Seconds for the adapter to answer a reset
COMMAND_TIMEOUT = 2.0
INTERRUPT = b' '  # Stops monitoring; at the prompt it is ignored, while a lone CR would repeat the last command
LISTEN_MIN = 0.05  # Seconds the adapter listens after sending before it is interrupted, so the frame goes out
SEND_TIMEOUT = 5.0
STATUS_WARNINGS = ('BUFFER FULL', 'CAN ERROR', 'BUS ERROR', 'BUS BUSY', 'FB ERROR', 'DATA ERROR', 'LV RESET')
_CLOSED = object()


@dataclass
class ElmFrame:
    arbitration_id: int
    is_extended_id: bool
    data: bytes
    is_remote_frame: bool = False


@dataclass
class AdapterInfo:
    version: str = ''  # ATI, e.g. 'ELM327 v1.5'
    device: str = ''  # STI of STN chips (OBDLink), e.g. 'STN2120 v5.6.19'
    protocol_b: bool = True  # AT PB supported


def bitrate_divisor(bitrate: int) -> int:
    """The protocol B divisor of 500 kbit/s for a bitrate; CanInitializationError if it has none."""
    divisor, remainder = divmod(BASE_BITRATE, bitrate) if bitrate > 0 else (0, 1)
    if remainder or not 1 <= divisor <= MAX_DIVISOR:
        rates = ', '.join(f'{BASE_BITRATE / d / 1000:g}' for d in (1, 2, 4, 5, 6, 10, 20))
        raise can.CanInitializationError(
            f'ELM327 adapters use 500 kbit/s divided by a whole number ({rates} … kbit/s), not {bitrate} bit/s'
        )
    return divisor


def protocol_b_parameters(bitrate: int, extended: bool) -> str:
    """AT PB flags and divisor: variable DLC, receive both identifier lengths, send 11 or 29 bit identifiers."""
    flags = PB_RECEIVE_BOTH | PB_VARIABLE_DLC | (0 if extended else PB_TRANSMIT_11_BIT)
    return f'{flags:02X} {bitrate_divisor(bitrate):02X}'


def header_commands(arbitration_id: int, extended: bool) -> List[str]:
    """AT SH (and AT CP for the top 5 bits of a 29 bit identifier) to send with this identifier."""
    if extended:
        return [f'ATCP {arbitration_id >> 24 & 0x1F:02X}', f'ATSH {arbitration_id & 0xFFFFFF:06X}']
    return [f'ATSH {arbitration_id & 0x7FF:03X}']


def _is_hex(token: str) -> bool:
    return bool(token) and all(c in '0123456789ABCDEFabcdef' for c in token)


def parse_frame_line(line: str) -> Optional[ElmFrame]:
    """A frame printed with headers, DLC and spaces on; None for status lines and frames received with errors."""
    if '<' in line:  # '<RX ERROR', '<DATA ERROR'
        return None
    tokens = line.split()
    dlc_index = next((i for i, token in enumerate(tokens) if len(token) == 1), None)
    if dlc_index is not None:
        header, rest = tokens[:dlc_index], tokens[dlc_index + 1 :]
    elif tokens and len(tokens[0]) in (3, 8):  # Adapters that do not show the DLC
        header, rest = tokens[:1], tokens[1:]
    else:
        header, rest = tokens[:4], tokens[4:]
    if not all(_is_hex(token) for token in header):
        return None
    if len(header) == 1 and len(header[0]) == 3:
        extended = False
    elif (len(header) == 1 and len(header[0]) == 8) or (len(header) == 4 and all(len(t) == 2 for t in header)):
        extended = True
    else:
        return None
    arbitration_id = int(''.join(header), 16)
    if rest == ['RTR']:
        return ElmFrame(arbitration_id, extended, b'', True)
    if len(rest) > 8 or not all(len(token) == 2 and _is_hex(token) for token in rest):
        return None
    return ElmFrame(arbitration_id, extended, bytes.fromhex(''.join(rest)))


class LineReader:
    """Splits the adapter's output into lines and prompts."""

    def __init__(self):
        self._line: List[str] = []

    def feed(self, data: bytes) -> List[Optional[str]]:
        """The complete lines, and None for each prompt."""
        events: List[Optional[str]] = []
        for char in data.decode('ascii', errors='ignore'):
            if char in '\r\n>':
                if self._line:
                    events.append(''.join(self._line).strip())
                    self._line.clear()
                if char == '>':
                    events.append(None)
            elif char.isprintable():
                self._line.append(char)
        return [event for event in events if event != '']


@dataclass
class _Request:
    message: can.Message
    done: threading.Event
    error: Optional[Exception] = None


class Elm327Bus(can.BusABC):
    """The CAN bus at the OBD port, through an ELM327 compatible adapter."""

    def __init__(
        self,
        channel: str,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        fd: bool = False,
        serial_baudrate: Optional[int] = None,
        timeout: float = INIT_TIMEOUT,
        **kwargs: Any,
    ):
        import serial  # pyserial, installed with python-can[serial]

        if fd:
            raise can.CanInterfaceNotImplementedError('ELM327 adapters are used for classic CAN only; turn off CAN FD')
        self.port = str(channel)
        self.bitrate = bitrate or BASE_BITRATE
        bitrate_divisor(self.bitrate)
        self.receive_own_messages = receive_own_messages
        self.listen_only = listen_only
        self.info = AdapterInfo()
        self.overflows = 0  # BUFFER FULL reports: frames were lost
        self._reader = LineReader()
        self._queue: queue.Queue = queue.Queue()
        self._requests: queue.Queue = queue.Queue()
        self._idle = True  # At the prompt (not monitoring or listening)
        self._collect: Optional[List[str]] = None  # Lines of the running command
        self._sent_at = 0.0
        self._tx_extended: Optional[bool] = None
        self._header: List[str] = []
        self._closed = False
        try:
            self._serial = serial.serial_for_url(
                self.port, baudrate=serial_baudrate or SERIAL_BAUDRATES[0], timeout=0.02
            )
        except (serial.SerialException, ValueError) as e:
            raise can.CanInitializationError(f'Could not open {self.port}: {e}') from e
        try:
            self._initialise(serial_baudrate, timeout)
        except BaseException:
            self._serial.close()
            raise
        name = self.info.device or self.info.version
        self.channel_info = f'{name} on {self.port}'
        self._worker = threading.Thread(target=self._run, name=f'Elm327Bus {self.port}', daemon=True)
        self._worker.start()
        super().__init__(channel=channel, **kwargs)

    # Commands (at the prompt)

    def _write(self, text: str) -> None:
        self._serial.write(text.encode('ascii'))

    def _pump(self) -> None:
        """Read what the adapter sent: frames to the queue, lines of a running command to the collector."""
        for event in self._reader.feed(self._serial.read(max(1, self._serial.in_waiting))):
            if event is None:
                self._idle = True
            elif self._collect is not None:
                self._collect.append(event)
            else:
                self._handle_line(event)

    def _command(self, command: str, optional: bool = False, timeout: float = COMMAND_TIMEOUT) -> List[str]:
        """Send a command at the prompt and return its answer (without the echo). A '?' answer raises
        CanOperationError, or is returned for optional commands that not every adapter knows."""
        self._collect = []
        self._idle = False
        self._write(command + '\r')
        deadline = time.monotonic() + timeout
        try:
            while not self._idle:
                if time.monotonic() > deadline:
                    raise can.CanOperationError(f'{self.port} did not answer {command}')
                self._pump()
            lines = [line for line in self._collect if line.replace(' ', '') != command.replace(' ', '')]
        finally:
            self._collect = None
        if '?' in lines and not optional:
            raise can.CanOperationError(f'The adapter on {self.port} does not support {command}')
        return lines

    def _stop_listening(self) -> None:
        """Interrupt monitoring (or listening for responses) and wait for the prompt."""
        if self._idle:
            return
        wait = self._sent_at + LISTEN_MIN - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        for _ in range(3):
            self._serial.write(INTERRUPT)
            deadline = time.monotonic() + COMMAND_TIMEOUT / 2
            while not self._idle and time.monotonic() < deadline:
                self._pump()
            if self._idle:
                return
        raise can.CanOperationError(f'The adapter on {self.port} does not stop monitoring')

    # Set-up

    def _reset(self, timeout: float) -> bool:
        """Stop a running monitor, reset the adapter and read its version; False if nothing answered."""
        self._serial.write(INTERRUPT)
        time.sleep(0.2)
        self._serial.reset_input_buffer()
        self._reader = LineReader()
        try:
            lines = self._command('ATZ', optional=True, timeout=timeout)
        except can.CanOperationError:
            return False
        self.info.version = next((line for line in lines if 'ELM' in line or 'STN' in line), '') or ' '.join(lines)
        return True

    def _initialise(self, serial_baudrate: Optional[int], timeout: float) -> None:
        if serial_baudrate or '://' in self.port:
            baudrates: Tuple[int, ...] = (self._serial.baudrate,)
        else:
            baudrates = SERIAL_BAUDRATES
        for baudrate in baudrates:
            self._serial.baudrate = baudrate
            if self._reset(timeout):
                break
        else:
            raise can.CanInitializationError(f'No ELM327 or OBDLink adapter answered on {self.port}')
        for command in ('ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATD1', 'ATCAF0'):
            self._command(command)
        # Optional: variable DLC, responses after sending, no CAN filter, ACK while monitoring unless listen only
        for command in ('ATV1', 'ATR1', 'ATCF 000', 'ATCM 000', 'ATCF 00000000', 'ATCM 00000000'):
            self._command(command, optional=True)
        self._command('ATCSM1' if self.listen_only else 'ATCSM0', optional=True)
        device = self._command('STI', optional=True)
        if device and device[0] != '?':
            self.info.device = device[0]
        self._select_protocol(False)
        logger.info(f'{self.info.device or self.info.version} on {self.port} at {self.bitrate} bit/s')

    def _select_protocol(self, extended: bool) -> None:
        """Protocol B at the bitrate, sending 11 or 29 bit identifiers (or the OBD protocol for it)."""
        if self.info.protocol_b:
            answer = self._command(f'ATPB {protocol_b_parameters(self.bitrate, extended)}', optional=True)
            self.info.protocol_b = '?' not in answer
        if self.info.protocol_b:
            self._command('ATSP B')
        elif self.bitrate in STANDARD_PROTOCOLS:
            self._command(f'ATSP {STANDARD_PROTOCOLS[self.bitrate][extended]}')
        else:
            raise can.CanInitializationError(
                f'The adapter on {self.port} only supports 500 and 250 kbit/s (it has no AT PB), not {self.bitrate} bit/s'
            )
        self._tx_extended = extended
        self._header = []

    # Worker

    def _handle_line(self, line: str) -> None:
        frame = parse_frame_line(line)
        if frame is not None:
            self._queue.put(
                can.Message(
                    timestamp=time.time(),
                    arbitration_id=frame.arbitration_id,
                    is_extended_id=frame.is_extended_id,
                    is_remote_frame=frame.is_remote_frame,
                    dlc=0 if frame.is_remote_frame else len(frame.data),
                    data=frame.data,
                    channel=self.port,
                    is_rx=True,
                )
            )
        elif line.startswith(STATUS_WARNINGS):
            if line.startswith('BUFFER FULL'):
                self.overflows += 1
            logger.warning(f'{self.port}: {line}')

    def _transmit(self, msg: can.Message) -> None:
        self._stop_listening()
        if msg.is_extended_id != self._tx_extended:
            self._select_protocol(msg.is_extended_id)
        header = header_commands(msg.arbitration_id, msg.is_extended_id)
        for command in header:
            if command not in self._header:
                self._command(command)
        self._header = header
        self._idle = False
        self._write(bytes(msg.data[: msg.dlc]).hex().upper() + '\r')
        self._sent_at = time.monotonic()

    def _run(self) -> None:
        error = None
        try:
            while not self._closed:
                try:
                    request = self._requests.get_nowait()
                except queue.Empty:
                    request = None
                if request is not None:
                    try:
                        self._transmit(request.message)
                    except can.CanError as e:
                        request.error = e
                    request.done.set()
                elif self._idle:
                    self._idle = False
                    self._write('ATMA\r')
                else:
                    self._pump()
        except Exception as e:  # The adapter was unplugged or the connection dropped
            error = e
        if not self._closed:
            self._queue.put(can.CanOperationError(f'Connection to {self.port} lost: {error}'))
        self._queue.put(_CLOSED)
        while not self._requests.empty():
            request = self._requests.get_nowait()
            request.error = can.CanOperationError(f'{self.port} is closed')
            request.done.set()

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        try:
            item = self._queue.get(timeout=timeout) if timeout is None or timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None, False
        if item is _CLOSED:
            self._queue.put(_CLOSED)  # Keep reporting it
            return None, False
        if isinstance(item, Exception):
            raise item
        return item, False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        if self.listen_only:
            raise can.CanOperationError('The connection is listen only')
        if msg.is_remote_frame or msg.is_fd or msg.is_error_frame:
            raise can.CanOperationError('ELM327 adapters send classic CAN data frames only')
        if not 1 <= msg.dlc <= 8:
            raise can.CanOperationError(f'ELM327 adapters send 1 to 8 data bytes, not {msg.dlc}')
        if self._closed or not self._worker.is_alive():
            raise can.CanOperationError(f'{self.port} is closed')
        request = _Request(msg, threading.Event())
        self._requests.put(request)
        if not request.done.wait(timeout or SEND_TIMEOUT):
            raise can.CanOperationError(f'Sending on {self.port} timed out')
        if request.error is not None:
            raise request.error
        if self.receive_own_messages:  # The adapter does not show sent frames
            echo = msg.__copy__()
            echo.timestamp, echo.is_rx, echo.channel = time.time(), False, self.port
            self._queue.put(echo)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._worker.join(1)
        try:
            self._serial.write(INTERRUPT)  # Leave the adapter at the prompt
        except Exception:
            pass
        self._serial.close()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """USB serial ports and the serial ports of paired Bluetooth devices; which of them has an ELM327 adapter
        is only known once one is opened."""
        try:
            from serial.tools import list_ports
        except ImportError:
            return []
        configs = []
        for port in sorted(list_ports.comports(), key=lambda port: port.device):
            description = f'{port.device} {port.description} {port.hwid}'.lower()
            if port.vid is not None or any(key in description for key in ('bluetooth', 'bthenum', 'rfcomm', 'obd')):
                configs.append({'interface': 'elm327', 'channel': port.device})
        return configs
