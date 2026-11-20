"""Request/response over a CAN session for command protocols such as XCP and CCP: send a command frame on the
request identifier and wait for the matching response frame on the response identifier."""

import asyncio
from typing import Callable, Iterable, Optional

import can

from can_explorer.core.session import BusSession, SessionError


class CanCommandError(Exception):
    pass


class CanCommandTimeout(CanCommandError):
    """No response arrived in time (the link may still be fine: a command protocol may retry)."""


CAN_FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def fd_length(size: int) -> int:
    """The smallest CAN FD payload length that holds `size` bytes."""
    for length in CAN_FD_LENGTHS:
        if length >= size:
            return length
    raise ValueError(f'{size} bytes do not fit in a CAN FD frame')


class CanCommandClient:
    """Sends command frames and collects the response frames of one response identifier.

    `unsolicited`, when set, sees every received frame first (its payload and timestamp) and returns True for frames
    that are not command responses — XCP DAQ data and events, CCP DTOs — which are then not queued. Frames of the
    extra `data_ids` (separate DAQ identifiers) only ever go to `unsolicited`.
    """

    def __init__(
        self,
        session: BusSession,
        request_id: int,
        response_id: int,
        extended: bool = False,
        fd: bool = False,
        padding: Optional[int] = 0x00,
        timeout: float = 1.0,
        bitrate_switch: bool = True,
        data_ids: Iterable[int] = (),
    ):
        self.session = session
        self.request_id = request_id
        self.response_id = response_id
        self.extended = extended
        self.fd = fd
        self.padding = padding
        self.timeout = timeout
        self.bitrate_switch = bitrate_switch
        self.data_ids = set(data_ids)
        self.unsolicited: Optional[Callable[[bytes, float], bool]] = None
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def __enter__(self) -> 'CanCommandClient':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def max_payload(self) -> int:
        return 64 if self.fd else 8

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or msg.is_extended_id != self.extended or not msg.data:
            return
        if msg.arbitration_id == self.response_id:
            data = bytes(msg.data)
            if self.unsolicited is not None and self.unsolicited(data, msg.timestamp):
                return
            self._responses.put_nowait(data)
        elif msg.arbitration_id in self.data_ids and self.unsolicited is not None:
            self.unsolicited(bytes(msg.data), msg.timestamp)

    def drain(self) -> None:
        """Forget responses that arrived unasked (late answers to an earlier command)."""
        self._drain()

    def _drain(self) -> None:
        while not self._responses.empty():
            self._responses.get_nowait()

    def send(self, data: bytes) -> None:
        payload = bytes(data)
        if len(payload) > self.max_payload:
            raise CanCommandError(f'{len(payload)} bytes do not fit in one {"CAN FD" if self.fd else "CAN"} frame')
        if self.fd:
            # A CAN FD frame has one of the lengths of CAN_FD_LENGTHS: fill up to the next one
            length = fd_length(len(payload))
            if length != len(payload):
                if self.padding is None:
                    raise CanCommandError(f'{len(payload)} bytes is not a CAN FD frame length (padding is off)')
                payload = payload + bytes([self.padding]) * (length - len(payload))
        elif self.padding is not None and len(payload) < 8:
            payload = payload + bytes([self.padding]) * (8 - len(payload))
        try:
            self.session.send(
                can.Message(
                    arbitration_id=self.request_id,
                    is_extended_id=self.extended,
                    is_fd=self.fd,
                    bitrate_switch=self.fd and self.bitrate_switch,
                    data=payload,
                )
            )
        except SessionError as e:
            raise CanCommandError(str(e)) from e

    async def request(self, data: bytes, timeout: Optional[float] = None) -> bytes:
        self._drain()
        self.send(data)
        return await self.receive(timeout)

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        try:
            return await asyncio.wait_for(self._responses.get(), self.timeout if timeout is None else timeout)
        except asyncio.TimeoutError:
            raise CanCommandTimeout('No response from the ECU') from None
