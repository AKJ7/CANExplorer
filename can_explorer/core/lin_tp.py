"""Active LIN transport layer (ISO 17987-2) client for diagnostics over LIN.

Requests go out in master request frames (0x3C). As LIN slaves cannot send on their own, the
response is collected by polling slave response headers (0x3D) until it is complete.
"""

import asyncio
from typing import Optional

import can

from can_explorer.core.lin import MASTER_REQUEST_ID, SLAVE_RESPONSE_ID, LinTpReassembler
from can_explorer.core.session import BusSession, SessionError

PADDING = 0xFF
FRAME_SPACING = 0.01
POLL_INTERVAL = 0.01
MAX_PAYLOAD = 4095


class LinTpError(Exception):
    pass


class LinTpClient:
    def __init__(
        self,
        session: BusSession,
        nad: int,
        timeout: float = 1.0,
        frame_spacing: float = FRAME_SPACING,
        poll_interval: float = POLL_INTERVAL,
    ):
        if not 0x01 <= nad <= 0x7F:
            raise ValueError(f'NAD must be 0x01-0x7F, got 0x{nad:02X}')
        self.session = session
        self.nad = nad
        self.timeout = timeout
        self.frame_spacing = frame_spacing
        self.poll_interval = poll_interval
        self._reassembler = LinTpReassembler()
        self._received: asyncio.Queue[bytes] = asyncio.Queue()
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def __enter__(self) -> 'LinTpClient':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _send(self, msg: can.Message) -> None:
        try:
            self.session.send(msg)
        except SessionError as e:
            raise LinTpError(str(e)) from e

    def _send_request_frame(self, data: bytes) -> None:
        self._send(
            can.Message(arbitration_id=MASTER_REQUEST_ID, is_extended_id=False, data=data.ljust(8, bytes([PADDING])))
        )

    async def send(self, payload: bytes) -> None:
        length = len(payload)
        if not 0 < length <= MAX_PAYLOAD:
            raise LinTpError(f'Payload length must be 1-{MAX_PAYLOAD}, got {length}')
        if length <= 6:
            self._send_request_frame(bytes([self.nad, length]) + payload)
            return
        self._send_request_frame(bytes([self.nad, 0x10 | (length >> 8), length & 0xFF]) + payload[:5])
        offset, sequence = 5, 1
        while offset < length:
            await asyncio.sleep(self.frame_spacing)
            self._send_request_frame(bytes([self.nad, 0x20 | sequence]) + payload[offset : offset + 6])
            offset += 6
            sequence = (sequence + 1) & 0x0F

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        """Poll slave response headers until a complete response arrives."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.timeout if timeout is None else timeout)
        while self._received.empty():
            if loop.time() >= deadline:
                raise LinTpError('Timeout waiting for a response')
            self._send(can.Message(arbitration_id=SLAVE_RESPONSE_ID, is_extended_id=False, is_remote_frame=True, dlc=8))
            await asyncio.sleep(self.poll_interval)
        return self._received.get_nowait()

    async def request(self, payload: bytes, timeout: Optional[float] = None) -> bytes:
        while not self._received.empty():
            self._received.get_nowait()
        await self.send(payload)
        return await self.receive(timeout)

    def _on_message(self, msg: can.Message) -> None:
        if msg.arbitration_id != SLAVE_RESPONSE_ID or msg.is_remote_frame or not msg.is_rx or msg.is_extended_id:
            return
        if not msg.data or msg.data[0] != self.nad:
            return
        _, payload = self._reassembler.feed(SLAVE_RESPONSE_ID, bytes(msg.data))
        if payload is not None:
            self._received.put_nowait(payload)
