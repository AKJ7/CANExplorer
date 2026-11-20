"""The common part of the python-can buses for LIN adapters with a vendor API (PEAK PLIN, Kvaser LIN, Vector LIN):
they present LIN as CAN frames in the sllin convention of core/lin.py, so the trace, the LDF decoding, the LIN
schedule and LIN diagnostics work with them as with an sllin SocketCAN interface.

The adapter is a LIN **master** (the default) or a **slave**, chosen with a channel suffix: `1` or `1@master`,
`1@slave`. Sending, in the sllin convention:

- master, data frame: a header and its response (the adapter publishes the data);
- master, remote (RTR) frame: a header only, answered by a slave;
- slave, data frame: the response the adapter gives from now on when the master sends that frame's header.

Received frames: a frame with a response is a data frame (`is_rx=False` when the adapter itself published the
response, shown only with receive_own_messages); a header no slave answered is a remote frame; a frame with a
checksum, parity, sync or framing error is an error frame with the LIN identifier.

Checksums: the enhanced checksum (LIN 2.x) for frames 0x00-0x3B and the classic one for the diagnostic frames
0x3C/0x3D; `checksum='classic'` uses the classic checksum for every frame (LIN 1.x buses).
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, List, Optional, Tuple

import can

from can_explorer.core.lin import MASTER_REQUEST_ID, MAX_FRAME_ID, SLAVE_RESPONSE_ID

MIN_BAUDRATE, MAX_BAUDRATE = 1000, 20000
DEFAULT_BAUDRATE = 19200
MODES = ('master', 'slave')


@dataclass
class LinEvent:
    """One frame seen by a LIN adapter, in the adapter-neutral form the base class turns into a can.Message."""

    frame_id: int
    data: bytes = b''
    timestamp: Optional[float] = None  # Unix time; None: now
    transmitted: bool = False  # The adapter published the response itself
    no_response: bool = False  # A header that no slave answered
    error: str = ''  # Checksum, parity, sync … error: reported as an error frame


def parse_lin_channel(channel: Any) -> Tuple[str, str]:
    """'1@slave' → ('1', 'slave'); without a suffix the adapter is the master."""
    text = str(channel if channel is not None else '').strip()
    base, separator, mode = text.rpartition('@')
    if separator:
        mode = mode.strip().lower()
        if mode not in MODES:
            raise ValueError(f'Mode after "@" must be master or slave, got {mode!r}')
        return base.strip(), mode
    return text, 'master'


def uses_classic_checksum(frame_id: int, checksum: str) -> bool:
    return checksum == 'classic' or frame_id in (MASTER_REQUEST_ID, SLAVE_RESPONSE_ID)


def lin_frame_of(msg: can.Message) -> Tuple[int, bytes]:
    """The LIN identifier and response data of a message to send; raises for messages LIN cannot carry."""
    if msg.is_extended_id or msg.is_fd or msg.is_error_frame:
        raise can.CanOperationError('LIN frames have a 6-bit identifier and at most 8 data bytes')
    if not 0 <= msg.arbitration_id <= MAX_FRAME_ID:
        raise can.CanOperationError(f'LIN frame identifiers are 0x00-0x3F, got 0x{msg.arbitration_id:X}')
    data = b'' if msg.is_remote_frame else bytes(msg.data[: msg.dlc])
    if not msg.is_remote_frame and not 1 <= len(data) <= 8:
        raise can.CanOperationError('A LIN response has 1 to 8 data bytes')
    return msg.arbitration_id, data


class LinAdapterBus(can.BusABC):
    """Base class: subclasses implement _open, _publish, _request, _respond, _poll and _close. Subclasses set their
    own attributes before calling this __init__, which calls _open."""

    interface_name = 'LIN adapter'

    def __init__(
        self,
        channel: Any,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        fd: bool = False,
        checksum: str = 'enhanced',
        **kwargs: Any,
    ):
        if fd:
            raise can.CanInterfaceNotImplementedError('LIN has no CAN FD; turn off CAN FD')
        try:
            self.device, self.mode = parse_lin_channel(channel)
        except ValueError as e:
            raise can.CanInitializationError(str(e)) from e
        if listen_only and self.mode == 'master':
            # A listener must not send headers: a slave without responses only listens
            self.mode = 'slave'
        self.listen_only = listen_only
        self.baudrate = int(bitrate or DEFAULT_BAUDRATE)
        if not MIN_BAUDRATE <= self.baudrate <= MAX_BAUDRATE:
            raise can.CanInitializationError(
                f'LIN baud rates are {MIN_BAUDRATE} to {MAX_BAUDRATE}, got {self.baudrate}'
            )
        if checksum not in ('enhanced', 'classic'):
            raise can.CanInitializationError(f'checksum must be enhanced or classic, got {checksum!r}')
        self.checksum = checksum
        self.receive_own_messages = receive_own_messages
        self._pending: Deque[can.Message] = deque()
        self._closed = False
        self.channel_info = f'{self.interface_name} {self.device} LIN {self.mode} @ {self.baudrate} baud'
        self._open()  # Before python-can's init: a failed open leaves nothing to shut down
        super().__init__(channel=channel, **kwargs)

    # Implemented by the adapters

    def _open(self) -> None:
        """Open and configure the adapter (self.device, self.mode, self.baudrate); clean up itself on failure."""
        raise NotImplementedError

    def _publish(self, frame_id: int, data: bytes, classic: bool) -> None:
        """Master: send a header and the response."""
        raise NotImplementedError

    def _request(self, frame_id: int, length: int, classic: bool) -> None:
        """Master: send a header for a slave to answer (length: the expected response length, 0 if unknown)."""
        raise NotImplementedError

    def _respond(self, frame_id: int, data: bytes, classic: bool) -> None:
        """Slave: answer the header of frame_id with data from now on."""
        raise NotImplementedError

    def _poll(self, timeout: float) -> List[LinEvent]:
        """The frames received within timeout seconds (may return early, may be empty)."""
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

    # python-can

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        if self._closed:
            raise can.CanOperationError(f'{self.channel_info} is closed')
        if self.listen_only:
            raise can.CanOperationError(f'{self.channel_info} is listen only')
        frame_id, data = lin_frame_of(msg)
        classic = uses_classic_checksum(frame_id, self.checksum)
        if self.mode == 'slave':
            if msg.is_remote_frame:
                raise can.CanOperationError('Only the LIN master sends headers; a slave sets responses')
            self._respond(frame_id, data, classic)
        elif msg.is_remote_frame:
            self._request(frame_id, msg.dlc if 0 < msg.dlc <= 8 else 0, classic)
        else:
            self._publish(frame_id, data, classic)

    def _message(self, event: LinEvent) -> Optional[can.Message]:
        if event.transmitted and not self.receive_own_messages and not event.error:
            return None
        timestamp = event.timestamp if event.timestamp is not None else time.time()
        if event.error:
            return can.Message(
                timestamp=timestamp,
                arbitration_id=event.frame_id,
                is_extended_id=False,
                is_error_frame=True,
                data=event.data[:8],
                channel=self.device,
                is_rx=True,
            )
        return can.Message(
            timestamp=timestamp,
            arbitration_id=event.frame_id,
            is_extended_id=False,
            is_remote_frame=event.no_response,
            dlc=0 if event.no_response else len(event.data),
            data=None if event.no_response else event.data,
            channel=self.device,
            is_rx=not event.transmitted,
        )

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._pending:
            if self._closed:
                return None, False
            remaining = 0.5 if deadline is None else max(0.0, deadline - time.monotonic())
            for event in self._poll(min(remaining, 0.5)):
                message = self._message(event)
                if message is not None:
                    self._pending.append(message)
            if not self._pending and deadline is not None and time.monotonic() >= deadline:
                return None, False
        return self._pending.popleft(), False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._close()
        finally:
            super().shutdown()


class DeviceClock:
    """Turns a device's counter (in `unit` seconds, wrapping at `wrap` counts, or not at all) into Unix time."""

    def __init__(self, unit: float, wrap: Optional[int] = None):
        self.unit, self.wrap = unit, wrap
        self._offset: Optional[float] = None
        self._last = 0
        self._wraps = 0

    def __call__(self, count: int) -> float:
        if self._offset is None:
            self._offset = time.time() - count * self.unit
        elif self.wrap and count < self._last and self._last - count > self.wrap // 2:
            self._wraps += 1
        self._last = count
        return self._offset + ((self._wraps * (self.wrap or 0)) + count) * self.unit
