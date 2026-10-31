"""A live J1850 VPW transport over an ELM327/OBDLink adapter, for GM Class 2 monitoring and diagnostics.

ELM327-compatible adapters (and the STN chips in OBDLink dongles) speak SAE J1850 VPW when set to protocol 2
(`AT SP 2`). This module drives such an adapter over its serial link and presents a `J1850Transport` (the
`send`/`receive` coroutines `J1850Client` expects), so the Class 2 client works against a real P01/P59 PCM the
same way it does against the in-memory `Class2EcuSimulator`.

The channel is the serial port, exactly as for the CAN `elm327` backend: a USB port (`COM4`, `/dev/ttyUSB0`), the
serial port of a paired Bluetooth adapter, or a WiFi adapter with pyserial's URL syntax (`socket://192.168.0.10:35000`).

Monitoring and diagnostics are time-multiplexed on a worker thread, as in `elm327.Elm327Bus`: when a frame consumer
(`on_frame`) is attached the adapter monitors the bus with `AT MA` and every received frame is parsed and handed to
the consumer (for the trace and the reverse-engineering tools); when a diagnostic request is sent, monitoring is
interrupted, the header is set with `AT SH`, the payload is written (the adapter adds the J1850 CRC and transmits),
the solicited responses are collected for the client, and monitoring resumes. The adapter is half-duplex, so frames
on the bus during a request are missed — the same trade-off the CAN backend documents. Blocking serial I/O lives on
the worker thread; the coroutines hand work to it and wait without stalling the event loop.
"""

import asyncio
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, List, Optional, Tuple

import can

from can_explorer.core.elm327 import (
    COMMAND_TIMEOUT,
    INIT_TIMEOUT,
    INTERRUPT,
    SERIAL_BAUDRATES,
    LineReader,
)
from can_explorer.core.j1850 import J1850Error, J1850Message

SEND_TIMEOUT = 5.0
# SAE J1850 VPW is ELM327 protocol 2. Echo and line feeds off, spaces and headers on, so responses print as
# "48 6B 10 41 0C 0C 6B" (the three header bytes and the data); the adapter checks and removes the CRC.
INIT_COMMANDS = ('ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP2', 'ATAL')


def _is_hex_byte(token: str) -> bool:
    return len(token) == 2 and all(c in '0123456789ABCDEFabcdef' for c in token)


def parse_vpw_line(line: str) -> Optional[J1850Message]:
    """A J1850 frame printed by the adapter (headers and spaces on): three header bytes then the data. Returns
    None for status lines ('NO DATA', 'OK', 'STOPPED', '?', 'BUS INIT', …) and frames flagged with an error."""
    if '<' in line:  # '<RX ERROR', '<DATA ERROR'
        return None
    tokens = line.split()
    if len(tokens) < 3 or not all(_is_hex_byte(token) for token in tokens):
        return None
    priority, target, source = (int(token, 16) for token in tokens[:3])
    return J1850Message(priority, target, source, bytes(int(token, 16) for token in tokens[3:]))


@dataclass
class _Request:
    message: J1850Message
    done: threading.Event
    error: Optional[Exception] = None


class Elm327VpwTransport:
    """A `J1850Transport` backed by an ELM327/OBDLink adapter in SAE J1850 VPW mode, time-multiplexing monitoring
    (`AT MA`) and diagnostic request/response on a worker thread.

    `on_frame` is called from the worker thread for every monitored frame and every diagnostic response; attach it
    (e.g. marshalling to the event loop) to feed a trace. With no `on_frame`, the adapter is not put into monitoring
    and only serves diagnostic requests."""

    def __init__(
        self,
        channel: str,
        on_frame: Optional[Callable[[J1850Message], None]] = None,
        timeout: float = INIT_TIMEOUT,
    ):
        import serial  # pyserial, installed with python-can[serial]

        self.port = str(channel)
        self.version = ''
        self._on_frame = on_frame
        self._reader = LineReader()
        self._lock = threading.Lock()
        # Solicited responses, for the diagnostic client; bounded so it cannot grow when nothing reads it
        self._responses: Deque[J1850Message] = deque(maxlen=256)
        self._requests: queue.Queue = queue.Queue()
        self._idle = True  # At the prompt (not monitoring or sending)
        self._collect: Optional[List[str]] = None  # Lines of the running command
        self._header = ''  # The current AT SH header value, set only when it changes
        self._closed = False
        try:
            self._serial = serial.serial_for_url(self.port, baudrate=SERIAL_BAUDRATES[0], timeout=0.02)
        except (serial.SerialException, ValueError) as e:
            raise J1850Error(f'Could not open {self.port}: {e}') from e
        try:
            self._initialise(timeout)
        except BaseException:
            self._serial.close()
            raise
        self._worker = threading.Thread(target=self._run, name=f'Elm327VpwTransport {self.port}', daemon=True)
        self._worker.start()

    # Blocking adapter protocol (worker thread, and __init__ before it starts)

    def _write(self, text: str) -> None:
        self._serial.write(text.encode('ascii'))

    def _pump(self) -> None:
        """Read what the adapter sent: prompt -> idle; lines of a running command -> collector; else a frame."""
        for event in self._reader.feed(self._serial.read(max(1, self._serial.in_waiting))):
            if event is None:
                self._idle = True
            elif self._collect is not None:
                self._collect.append(event)
            else:
                self._monitored(event)

    def _monitored(self, line: str) -> None:
        frame = parse_vpw_line(line)
        if frame is not None and self._on_frame is not None:
            self._on_frame(frame)

    def _command(self, command: str, timeout: float = COMMAND_TIMEOUT) -> List[str]:
        self._collect = []
        self._idle = False
        self._write(command + '\r')
        deadline = time.monotonic() + timeout
        try:
            while not self._idle:
                if time.monotonic() > deadline:
                    raise J1850Error(f'{self.port} did not answer {command}')
                self._pump()
            return [line for line in self._collect if line.replace(' ', '') != command.replace(' ', '')]
        finally:
            self._collect = None

    def _stop_monitoring(self) -> None:
        """Interrupt a running `AT MA` and wait for the prompt."""
        if self._idle:
            return
        for _ in range(3):
            self._serial.write(INTERRUPT)
            deadline = time.monotonic() + COMMAND_TIMEOUT / 2
            while not self._idle and time.monotonic() < deadline:
                self._pump()
            if self._idle:
                return
        raise J1850Error(f'The adapter on {self.port} did not stop monitoring')

    def _initialise(self, timeout: float) -> None:
        baudrates = (self._serial.baudrate,) if '://' in self.port else SERIAL_BAUDRATES
        for baudrate in baudrates:
            self._serial.baudrate = baudrate
            self._serial.write(INTERRUPT)  # Stop any running monitor
            time.sleep(0.2)
            self._serial.reset_input_buffer()
            self._reader = LineReader()
            try:
                lines = self._command('ATZ', timeout=timeout)
            except J1850Error:
                continue
            self.version = next((line for line in lines if 'ELM' in line or 'STN' in line), '') or ' '.join(lines)
            break
        else:
            raise J1850Error(f'No ELM327 or OBDLink adapter answered on {self.port}')
        for command in INIT_COMMANDS:
            if '?' in self._command(command):
                raise J1850Error(f'The adapter on {self.port} does not support {command} (J1850 VPW)')

    def _transmit(self, message: J1850Message) -> None:
        self._stop_monitoring()
        header = f'{message.priority:02X}{message.target:02X}{message.source:02X}'
        if header != self._header:
            self._command(f'ATSH {header}')
            self._header = header
        data_hex = bytes(message.data).hex().upper()
        self._collect = []
        self._idle = False
        self._write(data_hex + '\r')
        deadline = time.monotonic() + SEND_TIMEOUT
        try:
            while not self._idle:
                if time.monotonic() > deadline:
                    raise J1850Error(f'{self.port} did not answer the request')
                self._pump()
            lines = [line for line in self._collect if line.replace(' ', '').upper() != data_hex]
        finally:
            self._collect = None
        responses = [frame for frame in (parse_vpw_line(line) for line in lines) if frame is not None]
        with self._lock:
            self._responses.extend(responses)
        for frame in responses:  # The responses are bus frames too, so the trace sees them
            if self._on_frame is not None:
                self._on_frame(frame)

    def _run(self) -> None:
        try:
            while not self._closed:
                try:
                    request: Optional[_Request] = self._requests.get_nowait()
                except queue.Empty:
                    request = None
                if request is not None:
                    try:
                        self._transmit(request.message)
                    except Exception as e:
                        request.error = e
                    request.done.set()
                elif self._idle and self._on_frame is not None:
                    self._idle = False
                    self._write('ATMA\r')  # Monitor all frames until a request interrupts it
                elif self._idle:
                    time.sleep(0.01)  # No frame consumer: stay at the prompt and wait for requests
                else:
                    self._pump()
        except Exception:
            pass  # The adapter was unplugged or the connection dropped
        while True:  # Fail any pending requests on close
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            request.error = J1850Error(f'{self.port} is closed')
            request.done.set()

    # J1850Transport interface

    def submit(self, message: J1850Message, timeout: float = SEND_TIMEOUT) -> None:
        """Transmit a message on the worker and wait for it to be sent (blocking). Used by the async `send` and by
        `Elm327VpwBus`, which drives the same adapter from python-can's synchronous send."""
        if self._closed or not self._worker.is_alive():
            raise J1850Error(f'{self.port} is closed')
        request = _Request(message, threading.Event())
        self._requests.put(request)
        if not request.done.wait(timeout):
            raise J1850Error(f'Sending on {self.port} timed out')
        if request.error is not None:
            raise J1850Error(str(request.error))

    async def send(self, message: J1850Message) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self.submit, message)

    async def receive(self, timeout: Optional[float] = None) -> J1850Message:
        with self._lock:
            if self._responses:
                return self._responses.popleft()
        raise asyncio.TimeoutError('No J1850 response')

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker.is_alive():
            self._worker.join(1)
        try:
            self._serial.write(INTERRUPT)  # Leave the adapter at the prompt
        except Exception:
            pass
        self._serial.close()


def message_to_vpw(msg: can.Message) -> J1850Message:
    """A python-can frame back to a J1850 message: the extended id holds the three header bytes."""
    arb = msg.arbitration_id
    return J1850Message((arb >> 16) & 0xFF, (arb >> 8) & 0xFF, arb & 0xFF, bytes(msg.data))


def vpw_to_message(frame: J1850Message, channel: Optional[str] = None) -> can.Message:
    """A J1850 message as a python-can frame: the header (priority, target, source) packed into the extended
    arbitration id, the payload as the data. Payloads over eight bytes are marked CAN FD so they fit."""
    data = bytes(frame.data)
    return can.Message(
        timestamp=time.time(),
        arbitration_id=(frame.priority << 16) | (frame.target << 8) | frame.source,
        is_extended_id=True,
        is_fd=len(data) > 8,
        dlc=len(data),
        data=data,
        channel=channel,
        is_rx=True,
    )


class SessionJ1850Transport:
    """A `J1850Transport` over a `BusSession` that carries VPW pseudo-CAN frames (an `Elm327VpwBus` connection).

    Diagnostics run over the connection's existing session — the request is sent as a pseudo-CAN frame and the
    responses are read from the session's frame stream — so the single adapter is shared with monitoring rather
    than opened a second time. The session carries all bus traffic, so `J1850Client` matches its response by
    content."""

    def __init__(self, session: Any):
        self.session = session
        self._received: asyncio.Queue = asyncio.Queue()
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def _on_message(self, msg: can.Message) -> None:
        if msg.is_rx and msg.is_extended_id and len(msg.data):
            self._received.put_nowait(message_to_vpw(msg))

    async def send(self, message: J1850Message) -> None:
        arbitration_id = (message.priority << 16) | (message.target << 8) | message.source
        self.session.send(can.Message(arbitration_id=arbitration_id, is_extended_id=True, data=bytes(message.data)))

    async def receive(self, timeout: Optional[float] = None) -> J1850Message:
        return await asyncio.wait_for(self._received.get(), timeout)


class Elm327VpwBus(can.BusABC):
    """The GM Class 2 (SAE J1850 VPW) bus at the OBD port, as a python-can interface ("elm327_vpw"), so the whole
    application — the connection dialog, trace, dashboard, reverse engineering and recording — works on a VPW bus
    like on any CAN interface. Each VPW frame is a python-can frame with the three header bytes in the extended
    arbitration id and the payload as the data; the adapter is monitored and sent to through the one serial link.

    This is not real CAN: it is fixed at 10.4 kbit/s (the bitrate is ignored), it is not CAN FD, and the id is a
    J1850 header, not an arbitration id. Diagnostics (`J1850Client`) run over the connection's session, so no
    second connection to the adapter is opened."""

    def __init__(
        self,
        channel: str,
        receive_own_messages: bool = False,
        fd: bool = False,
        **kwargs: Any,
    ):
        if fd:
            raise can.CanInterfaceNotImplementedError('J1850 VPW is classic Class 2, not CAN FD; turn off CAN FD')
        self.port = str(channel)
        self.receive_own_messages = receive_own_messages
        self._queue: queue.Queue = queue.Queue()
        try:
            self._transport = Elm327VpwTransport(channel, on_frame=self._on_frame)
        except J1850Error as e:
            raise can.CanInitializationError(str(e)) from e
        name = f'ELM327 J1850 VPW on {self.port}'
        self.channel_info = f'{self._transport.version} ({name})' if self._transport.version else name
        super().__init__(channel=channel, **kwargs)

    def _on_frame(self, frame: J1850Message) -> None:  # Called from the transport's worker thread
        self._queue.put(vpw_to_message(frame, self.port))

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        try:
            item = self._queue.get(timeout=timeout) if timeout is None or timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None, False
        if isinstance(item, Exception):
            raise item
        return item, False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        try:
            self._transport.submit(message_to_vpw(msg), timeout or SEND_TIMEOUT)
        except J1850Error as e:
            raise can.CanOperationError(str(e)) from e
        if self.receive_own_messages:
            echo = msg.__copy__()
            echo.timestamp, echo.is_rx, echo.channel = time.time(), False, self.port
            self._queue.put(echo)

    def shutdown(self) -> None:
        self._transport.close()
        super().shutdown()
