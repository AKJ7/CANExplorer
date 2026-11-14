"""SAE J1708 serial bus: a python-can bus (interface "j1708", class J1708Bus) for the 9600-baud two-wire (RS-485)
bus of older heavy trucks and buses, so a live J1708 capture can be monitored, recorded and decoded in CANExplorer.

J1708 is **not** CAN. It is a half-duplex serial bus: each message is a MID (the module that sent it), up to 19
data bytes and a one-byte checksum, and messages are separated by an idle period on the line (the bus is free for
the next transmitter after about ten bit times of silence). The application layer carried on it is SAE J1587, which
the existing **J1587** trace decoder describes.

This backend reads the serial port of a J1708 interface (a USB RS-485/J1708 adapter) and presents each J1708
message as a python-can frame: the MID as the arbitration id (so the trace groups by transmitting module) and the
whole message — MID, parameters and the trailing checksum — as the data. Messages longer than eight bytes are
marked CAN FD so the payload fits. Select the **J1587** protocol in the trace to decode the parameters.

Only the framing of J1708 is implemented here; it is open and documented (MID, data, checksum, idle-gap framing).
The J1587 *application* layer (which PIDs mean what) is a separate, partly paywalled standard — the J1587 decoder
names only the well-known parameters and shows the rest as raw bytes, and does not guess the others.

Channel: the serial port, with an optional `@<baud>` suffix for a non-standard rate (`/dev/ttyUSB0`, `COM5`,
`/dev/ttyUSB0@9600`). The default is 9600 baud, the J1708 bit rate.

Limitations.

- **Message framing is by idle gaps.** The adapter delivers a byte stream with no explicit message boundaries, so
  messages are split where the line falls idle (a read returns no data) or at the 21-byte maximum. Two messages
  sent back to back with no detectable gap can be merged into one frame; the J1587 decoder then reads the second
  MID as a parameter. A J1708 adapter that frames messages itself avoids this.
- **Sending is best-effort.** J1708 arbitrates by MID priority and inter-message timing on the wire, which a plain
  UART cannot reproduce; a sent message may collide on a busy bus. The checksum is computed and appended if the
  payload does not already carry a valid one.
- Not CAN: the bitrate option selects the serial rate; there is no CAN FD, remote or error frame.
"""

import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import can

logger = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 9600  # The SAE J1708 bit rate
MAX_MESSAGE_LENGTH = 21  # MID + up to 19 data bytes + checksum
MIN_MESSAGE_LENGTH = 2  # MID + checksum
# The line is idle between messages; read with this timeout and treat a read that returns nothing as a boundary.
IDLE_GAP = 0.003  # Seconds (about 29 bit times at 9600 baud; the standard's end-of-message idle is ~10 bit times)
_CLOSED = object()


def j1708_checksum(data: bytes) -> int:
    """The J1708 checksum of a message's bytes: the value that makes the sum of all bytes a multiple of 256 (the
    two's complement of the sum)."""
    return (-sum(data)) & 0xFF


def has_valid_checksum(frame: bytes) -> bool:
    """Whether a frame already ends in a valid J1708 checksum (the sum of all its bytes is a multiple of 256)."""
    return len(frame) >= MIN_MESSAGE_LENGTH and sum(frame) % 256 == 0


def parse_channel(channel: str) -> Tuple[str, int]:
    """Split a channel into the serial port and the baud rate, from an optional `@<baud>` suffix
    (`/dev/ttyUSB0@9600`). Defaults to 9600 baud. Raises CanInitializationError for a non-numeric rate."""
    port = str(channel)
    baudrate = DEFAULT_BAUDRATE
    if '@' in port:
        port, _, suffix = port.rpartition('@')
        try:
            baudrate = int(suffix)
        except ValueError:
            raise can.CanInitializationError(f'The baud rate after @ must be a number, not {suffix!r}') from None
    return port, baudrate


def frame_to_message(frame: bytes, channel: Optional[str] = None) -> can.Message:
    """A J1708 message as a python-can frame: the MID (the first byte) as the arbitration id, the whole message
    (MID, data and the trailing checksum) as the data. Messages over eight bytes are marked CAN FD so they fit."""
    frame = bytes(frame)
    return can.Message(
        timestamp=time.time(),
        arbitration_id=frame[0],
        is_extended_id=False,
        is_fd=len(frame) > 8,
        dlc=len(frame),
        data=frame,
        channel=channel,
        is_rx=True,
    )


def message_to_frame(msg: can.Message) -> bytes:
    """The J1708 bytes to put on the wire for a python-can frame: its data, with a checksum appended if the data
    does not already end in a valid one."""
    data = bytes(msg.data)
    if has_valid_checksum(data):
        return data
    return data + bytes([j1708_checksum(data)])


class J1708FrameAssembler:
    """Groups a J1708 byte stream into messages. `feed` returns the messages completed by reaching the 21-byte
    maximum; `flush`, called when the line falls idle, returns the buffered message. Kept free of I/O and timing so
    it is unit-testable."""

    def __init__(self, max_length: int = MAX_MESSAGE_LENGTH):
        self._buffer = bytearray()
        self._max = max_length

    def feed(self, data: bytes) -> List[bytes]:
        frames = []
        for byte in data:
            self._buffer.append(byte)
            if len(self._buffer) >= self._max:
                frames.append(bytes(self._buffer))
                self._buffer.clear()
        return frames

    def flush(self) -> Optional[bytes]:
        if not self._buffer:
            return None
        frame = bytes(self._buffer)
        self._buffer.clear()
        return frame

    def reset(self) -> None:
        self._buffer.clear()


class J1708Bus(can.BusABC):
    """The SAE J1708 serial bus, as a python-can interface ("j1708"). Each J1708 message is a python-can frame with
    the MID in the arbitration id and the whole message as the data; select the **J1587** trace decoder to describe
    the parameters. This is not CAN (see the module docstring)."""

    def __init__(
        self,
        channel: str,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        fd: bool = False,
        **kwargs: Any,
    ):
        import serial  # pyserial, installed with python-can[serial]

        if fd:
            raise can.CanInterfaceNotImplementedError('J1708 is a 9600-baud serial bus, not CAN FD; turn off CAN FD')
        self.port, baudrate = parse_channel(channel)
        self.baudrate = bitrate or baudrate  # The connection dialog's bitrate selects the serial rate
        self.receive_own_messages = receive_own_messages
        self.listen_only = listen_only
        self._assembler = J1708FrameAssembler()
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        try:
            self._serial = serial.serial_for_url(self.port, baudrate=self.baudrate, timeout=IDLE_GAP)
        except (serial.SerialException, ValueError, OSError) as e:
            raise can.CanInitializationError(f'Could not open {self.port}: {e}') from e
        self.channel_info = f'J1708 on {self.port} at {self.baudrate} baud'
        self._worker = threading.Thread(target=self._run, name=f'J1708Bus {self.port}', daemon=True)
        self._worker.start()
        super().__init__(channel=channel, **kwargs)

    def _emit(self, frame: bytes) -> None:
        if len(frame) < MIN_MESSAGE_LENGTH:
            return  # Noise or a truncated message
        if not has_valid_checksum(frame):
            logger.debug('%s: J1708 frame with a bad checksum: %s', self.port, frame.hex())
        self._queue.put(frame_to_message(frame, self.port))

    def _run(self) -> None:
        error = None
        try:
            while not self._closed:
                data = self._serial.read(max(1, self._serial.in_waiting))
                if data:
                    for frame in self._assembler.feed(data):
                        self._emit(frame)
                else:  # The read timed out: the line is idle, so the buffered bytes are a complete message
                    frame = self._assembler.flush()
                    if frame is not None:
                        self._emit(frame)
        except Exception as e:  # The adapter was unplugged or the port closed
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
        if self.listen_only:
            raise can.CanOperationError('The connection is listen only')
        if msg.is_remote_frame or msg.is_error_frame:
            raise can.CanOperationError('J1708 carries data messages only')
        if self._closed or not self._worker.is_alive():
            raise can.CanOperationError(f'{self.port} is closed')
        try:
            self._serial.write(message_to_frame(msg))
        except Exception as e:
            raise can.CanOperationError(f'Could not send on {self.port}: {e}') from e
        if self.receive_own_messages:
            echo = msg.__copy__()
            echo.timestamp, echo.is_rx, echo.channel = time.time(), False, self.port
            self._queue.put(echo)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._worker.join(1)
        try:
            self._serial.close()
        except Exception:
            pass
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """J1708 needs a dedicated RS-485/J1708 adapter and an ordinary serial port cannot be told from one, so
        there is nothing to offer: type the serial port (with an optional `@<baud>` suffix) instead."""
        return []
