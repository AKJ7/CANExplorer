"""GVRET devices: a python-can bus (interface "gvret", class GvretBus) for the CAN interfaces speaking the binary
GVRET protocol of SavvyCAN, such as ESP32RET boards, the Macchina M2 (M2RET) and the CANDue (GVRET firmware).

The channel is the serial port, optionally followed by the device's bus number: `/dev/ttyACM0` (bus 0),
`COM5@1` (bus 1). ESP32RET boards on WiFi are reached over TCP with pyserial's URL syntax, e.g.
`socket://192.168.4.1:23`. The bitrate and listen only mode of the bus are configured when it is opened; the other
bus keeps its settings. Classic CAN only: GVRET's CAN FD extension and remote frames are not supported.

Protocol: `E7 E7` switches the device to binary mode. Commands are `F1 <command> ...`; received frames are
`F1 00 <timestamp µs, 4> <id, 4: bit 31 extended> <length + bus << 4> <data> <checksum, unused>`.
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

import can

BINARY_MODE = b'\xe7\xe7'
START = 0xF1
CMD_FRAME, CMD_TIME_SYNC, CMD_SETUP_CANBUS, CMD_GET_CANBUS_PARAMS = 0x00, 0x01, 0x05, 0x06
CMD_GET_DEVICE_INFO, CMD_KEEPALIVE, CMD_GET_NUM_BUSES = 0x07, 0x09, 0x0C
# Length of the fixed size messages from the device after F1 <command>
RESPONSE_LENGTHS = {CMD_TIME_SYNC: 4, CMD_GET_CANBUS_PARAMS: 10, CMD_GET_DEVICE_INFO: 6, CMD_KEEPALIVE: 2}
SPEED_GIVEN, SPEED_ENABLE, SPEED_LISTEN_ONLY = 0x80000000, 0x40000000, 0x20000000
SPEED_MASK = 0xFFFFF
EXTENDED_FLAG = 0x80000000
CONFIGURABLE_BUSES = 2  # SETUP_CANBUS configures buses 0 and 1
SERIAL_BAUDRATE = 1_000_000  # Native USB devices ignore it; USB serial bridges of ESP32 boards need it
INIT_TIMEOUT = 2.0
TIMESTAMP_WRAP = 1 << 32
_CLOSED = object()


@dataclass
class BusParameters:
    enabled: bool
    listen_only: bool
    bitrate: int

    def encode(self) -> bytes:
        """The 4 bytes of SETUP_CANBUS: the speed with the 'flags given', enable and listen only bits."""
        value = SPEED_GIVEN | (self.bitrate & SPEED_MASK)
        value |= (SPEED_ENABLE if self.enabled else 0) | (SPEED_LISTEN_ONLY if self.listen_only else 0)
        return value.to_bytes(4, 'little')


@dataclass
class GvretFrame:
    timestamp_us: int
    arbitration_id: int
    is_extended_id: bool
    bus: int
    data: bytes


@dataclass
class DeviceInfo:
    build: int = 0
    buses: int = CONFIGURABLE_BUSES
    parameters: List[BusParameters] = field(default_factory=list)


def parse_channel(channel: str) -> Tuple[str, int]:
    """'COM5@1' → ('COM5', 1); without '@' the bus is 0."""
    port, separator, bus = str(channel).rpartition('@')
    if separator and bus.isdigit():
        return port, int(bus)
    return str(channel), 0


def encode_frame(msg: can.Message, bus: int) -> bytes:
    if msg.is_remote_frame or msg.is_fd or msg.is_error_frame:
        raise can.CanOperationError('GVRET devices send classic CAN data frames only')
    if msg.dlc > 8:
        raise can.CanOperationError(f'Classic CAN frames have up to 8 bytes, got {msg.dlc}')
    identifier = msg.arbitration_id | (EXTENDED_FLAG if msg.is_extended_id else 0)
    data = bytes(msg.data[: msg.dlc])
    return bytes([START, CMD_FRAME]) + identifier.to_bytes(4, 'little') + bytes([bus, len(data)]) + data + b'\x00'


def setup_command(parameters: List[BusParameters]) -> bytes:
    return bytes([START, CMD_SETUP_CANBUS]) + b''.join(item.encode() for item in parameters[:CONFIGURABLE_BUSES])


class GvretParser:
    """Splits the byte stream of a device into messages. Bytes before a message (such as the text of the
    device's console before binary mode) and unknown commands are skipped."""

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Tuple[int, Any]]:
        """(command, payload) for every complete message; frames as GvretFrame."""
        self._buffer += data
        buffer = self._buffer
        while True:
            start = buffer.find(START)
            if start < 0:
                buffer.clear()
                return
            del buffer[:start]
            if len(buffer) < 2:
                return
            command = buffer[1]
            if command == CMD_FRAME:
                if len(buffer) < 11:
                    return
                length = buffer[10] & 0x0F
                if length > 8:  # Not a frame: resynchronise
                    del buffer[:1]
                    continue
                total = 11 + length + 1
                if len(buffer) < total:
                    return
                identifier = int.from_bytes(buffer[6:10], 'little')
                frame = GvretFrame(
                    int.from_bytes(buffer[2:6], 'little'),
                    identifier & 0x1FFFFFFF,
                    bool(identifier & EXTENDED_FLAG),
                    buffer[10] >> 4,
                    bytes(buffer[11 : 11 + length]),
                )
                del buffer[:total]
                yield command, frame
            elif command == CMD_GET_NUM_BUSES:
                if len(buffer) < 3:
                    return
                count = buffer[2]
                del buffer[:3]
                yield command, count
            elif command in RESPONSE_LENGTHS:
                total = 2 + RESPONSE_LENGTHS[command]
                if len(buffer) < total:
                    return
                payload = bytes(buffer[2:total])
                del buffer[:total]
                yield command, payload
            else:
                del buffer[:1]  # Unknown command (or a data byte 0xF1 outside a message)


def parse_parameters(payload: bytes) -> List[BusParameters]:
    """The bus parameters of GET_CANBUS_PARAMS: per bus a flag byte (bit 0 enabled, bit 4 listen only) and the speed."""
    return [
        BusParameters(
            bool(payload[offset] & 0x01),
            bool(payload[offset] & 0x10),
            int.from_bytes(payload[offset + 1 : offset + 5], 'little'),
        )
        for offset in (0, 5)
    ]


class TimestampClock:
    """Turns the device's 32 bit microsecond counter (wrapping after 71 minutes) into Unix time."""

    def __init__(self):
        self._offset: Optional[float] = None
        self._last = 0
        self._wraps = 0

    def __call__(self, timestamp_us: int) -> float:
        if self._offset is None:
            self._offset = time.time() - timestamp_us / 1e6
        elif timestamp_us < self._last and self._last - timestamp_us > TIMESTAMP_WRAP // 2:
            self._wraps += 1
        self._last = timestamp_us
        return self._offset + (self._wraps * TIMESTAMP_WRAP + timestamp_us) / 1e6


class GvretBus(can.BusABC):
    """One CAN bus of a GVRET device."""

    def __init__(
        self,
        channel: str,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        fd: bool = False,
        serial_baudrate: int = SERIAL_BAUDRATE,
        timeout: float = INIT_TIMEOUT,
        **kwargs: Any,
    ):
        import serial  # pyserial, installed with python-can[serial]

        if fd:
            raise can.CanInterfaceNotImplementedError('GVRET devices are used for classic CAN only; turn off CAN FD')
        self.port, self.bus = parse_channel(channel)
        self.channel_info = f'GVRET {self.port} bus {self.bus}'
        self.receive_own_messages = receive_own_messages
        self.info = DeviceInfo()
        self._parser = GvretParser()
        self._clock = TimestampClock()
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        try:
            self._serial = serial.serial_for_url(self.port, baudrate=serial_baudrate, timeout=0.05)
        except (serial.SerialException, ValueError) as e:
            raise can.CanInitializationError(f'Could not open {self.port}: {e}') from e
        try:
            self._initialise(bitrate, listen_only, timeout)
        except BaseException:
            self._serial.close()
            raise
        self._reader = threading.Thread(target=self._read, name=f'GvretBus {self.port}', daemon=True)
        self._reader.start()
        super().__init__(channel=channel, **kwargs)

    def _initialise(self, bitrate: Optional[int], listen_only: bool, timeout: float) -> None:
        """Binary mode, then the device's buses, version and bus parameters; configure the bus when asked."""
        self._serial.reset_input_buffer()
        self._serial.write(
            BINARY_MODE + bytes([START, CMD_GET_NUM_BUSES, START, CMD_GET_DEVICE_INFO, START, CMD_GET_CANBUS_PARAMS])
        )
        deadline = time.monotonic() + timeout
        parameters: Optional[List[BusParameters]] = None
        while parameters is None:
            if time.monotonic() > deadline:
                raise can.CanInitializationError(
                    f'No GVRET device answered on {self.port} (is it running GVRET firmware such as ESP32RET?)'
                )
            for command, payload in self._parser.feed(self._serial.read(max(1, self._serial.in_waiting))):
                if command == CMD_GET_NUM_BUSES:
                    self.info.buses = payload
                elif command == CMD_GET_DEVICE_INFO:
                    self.info.build = int.from_bytes(payload[0:2], 'little')
                elif command == CMD_GET_CANBUS_PARAMS:
                    parameters = parse_parameters(payload)
        self.info.parameters = parameters
        if self.bus >= max(self.info.buses, 1):
            raise can.CanInitializationError(f'{self.port} has {self.info.buses} buses (0 to {self.info.buses - 1})')
        if bitrate is None and not listen_only:
            return
        if self.bus >= CONFIGURABLE_BUSES:
            raise can.CanInitializationError(f'Only buses 0 and 1 can be configured; open bus {self.bus} as it is')
        current = parameters[self.bus]
        parameters[self.bus] = BusParameters(True, listen_only, bitrate or current.bitrate)
        self._serial.write(setup_command(parameters))

    def _read(self) -> None:
        error = None
        try:
            while not self._closed:
                data = self._serial.read(max(1, self._serial.in_waiting))
                for command, payload in self._parser.feed(data):
                    if command == CMD_FRAME and payload.bus == self.bus:
                        self._queue.put(
                            can.Message(
                                timestamp=self._clock(payload.timestamp_us),
                                arbitration_id=payload.arbitration_id,
                                is_extended_id=payload.is_extended_id,
                                data=payload.data,
                                channel=self.bus,
                                is_rx=True,
                            )
                        )
        except Exception as e:  # The device was unplugged or the connection dropped
            error = e
        if not self._closed:
            self._queue.put(can.CanOperationError(f'Connection to {self.port} lost: {error}'))
        self._queue.put(_CLOSED)

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
        packet = encode_frame(msg, self.bus)
        try:
            self._serial.write(packet)
        except Exception as e:
            raise can.CanOperationError(f'Could not send to {self.port}: {e}') from e
        if self.receive_own_messages:  # The device does not echo sent frames
            echo = msg.__copy__()
            echo.timestamp, echo.is_rx, echo.channel = time.time(), False, self.bus
            self._queue.put(echo)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader.join(1)
        self._serial.close()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """The USB serial ports; which of them run GVRET firmware is only known once one is opened."""
        try:
            from serial.tools import list_ports
        except ImportError:
            return []
        return [
            {'interface': 'gvret', 'channel': port.device}
            for port in sorted(list_ports.comports(), key=lambda port: port.device)
            if port.vid is not None
        ]
