"""Request/response over a CAN session for command protocols such as XCP and CCP: send a command frame on the
request identifier and wait for the matching response frame on the response identifier."""

import asyncio
from typing import Optional

import can

from can_explorer.core.session import BusSession, SessionError


class CanCommandError(Exception):
    pass


class CanCommandClient:
    """Sends command frames and collects the response frames of one response identifier."""

    def __init__(
        self,
        session: BusSession,
        request_id: int,
        response_id: int,
        extended: bool = False,
        fd: bool = False,
        padding: Optional[int] = 0x00,
        timeout: float = 1.0,
    ):
        self.session = session
        self.request_id = request_id
        self.response_id = response_id
        self.extended = extended
        self.fd = fd
        self.padding = padding
        self.timeout = timeout
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def __enter__(self) -> 'CanCommandClient':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _on_message(self, msg: can.Message) -> None:
        if msg.is_rx and msg.arbitration_id == self.response_id and msg.is_extended_id == self.extended and msg.data:
            self._responses.put_nowait(bytes(msg.data))

    def _drain(self) -> None:
        while not self._responses.empty():
            self._responses.get_nowait()

    def send(self, data: bytes) -> None:
        payload = bytes(data)
        if not self.fd and self.padding is not None and len(payload) < 8:
            payload = payload + bytes([self.padding]) * (8 - len(payload))
        try:
            self.session.send(
                can.Message(arbitration_id=self.request_id, is_extended_id=self.extended, is_fd=self.fd, data=payload)
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
            raise CanCommandError('No response from the ECU') from None
