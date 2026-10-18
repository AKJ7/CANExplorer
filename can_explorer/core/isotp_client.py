import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import can

from can_explorer.core.session import BusSession, SessionError

CLASSIC_FRAME = 8
FD_FRAME = 64
FD_LENGTHS = (8, 12, 16, 20, 24, 32, 48, 64)
MAX_WAIT_FRAMES = 10
# Waits shorter than this are busy-waited: the event loop cannot time them (Windows: 15.6 ms resolution)
SPIN_TIME = 0.002


class IsoTpError(Exception):
    pass


async def _wait_until(deadline: float) -> None:
    """Wait until a time.perf_counter() deadline, precisely: event loop timers are only as fine as the system
    timer (about 15.6 ms on Windows), which would make separation times below that (e.g. 100 µs) far longer."""
    remaining = deadline - time.perf_counter()
    if remaining > SPIN_TIME:
        await asyncio.sleep(remaining - SPIN_TIME)
    while time.perf_counter() < deadline:
        pass  # At most SPIN_TIME of busy waiting


def parse_st_min(st_min: int) -> float:
    """Minimum separation time in seconds (ISO 15765-2); reserved values mean the maximum of 127 ms."""
    if st_min <= 0x7F:
        return st_min / 1000
    if 0xF1 <= st_min <= 0xF9:
        return (st_min - 0xF0) / 10_000
    return 0.127


@dataclass(slots=True)
class _Reception:
    length: int
    data: bytearray
    next_sn: int = 1
    block_count: int = 0


class IsoTpClient:
    """Active ISO 15765-2 endpoint (normal addressing) on top of a BusSession.

    Sends single or segmented messages honouring the receiver's flow control, and receives
    segmented messages sending flow control itself.
    """

    def __init__(
        self,
        session: BusSession,
        tx_id: int,
        rx_id: int,
        extended: bool = False,
        fd: bool = False,
        padding: Optional[int] = 0xCC,
        block_size: int = 0,
        st_min: int = 0,
        timeout: float = 1.0,
    ):
        self.session = session
        self.tx_id, self.rx_id, self.extended, self.fd = tx_id, rx_id, extended, fd
        self.padding = padding
        self.block_size, self.st_min = block_size, st_min
        self.timeout = timeout
        self._frame_size = FD_FRAME if fd else CLASSIC_FRAME
        self._received: asyncio.Queue[bytes] = asyncio.Queue()
        self._flow_control: asyncio.Queue[bytes] = asyncio.Queue()
        self._reception: Optional[_Reception] = None
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def __enter__(self) -> 'IsoTpClient':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # Transmission

    def _send_frame(self, data: bytes) -> None:
        if self.padding is not None:
            target = CLASSIC_FRAME if len(data) <= CLASSIC_FRAME else next(n for n in FD_LENGTHS if n >= len(data))
            data = data + bytes([self.padding]) * (target - len(data))
        try:
            self.session.send(
                can.Message(arbitration_id=self.tx_id, is_extended_id=self.extended, is_fd=self.fd, data=data)
            )
        except SessionError as e:
            raise IsoTpError(str(e)) from e

    async def send(self, payload: bytes) -> None:
        length = len(payload)
        if length == 0:
            raise IsoTpError('Cannot send an empty payload')
        if length <= 7:
            self._send_frame(bytes([length]) + payload)
            return
        if self.fd and length <= FD_FRAME - 2:
            self._send_frame(bytes([0x00, length]) + payload)
            return
        if length <= 0xFFF:
            header = bytes([0x10 | (length >> 8), length & 0xFF])
        else:
            header = bytes([0x10, 0x00]) + length.to_bytes(4, 'big')
        first = self._frame_size - len(header)
        self._drain(self._flow_control)
        self._send_frame(header + payload[:first])
        offset, sequence = first, 1
        chunk = self._frame_size - 1
        last_sent = time.perf_counter()
        while offset < length:
            block_size, separation = await self._wait_flow_control()
            sent_in_block = 0
            while offset < length and (block_size == 0 or sent_in_block < block_size):
                if separation:
                    await _wait_until(last_sent + separation)
                elif sent_in_block:
                    await asyncio.sleep(0)  # Let the receiver and the GUI run between frames
                self._send_frame(bytes([0x20 | sequence]) + payload[offset : offset + chunk])
                last_sent = time.perf_counter()
                offset += chunk
                sequence = (sequence + 1) & 0x0F
                sent_in_block += 1

    async def _wait_flow_control(self) -> tuple[int, float]:
        for _ in range(MAX_WAIT_FRAMES + 1):
            try:
                frame = await asyncio.wait_for(self._flow_control.get(), self.timeout)
            except asyncio.TimeoutError:
                raise IsoTpError('Timeout waiting for flow control (N_Bs)') from None
            status = frame[0] & 0x0F
            if status == 0:
                return frame[1], parse_st_min(frame[2])
            if status == 2:
                raise IsoTpError('Receiver reported overflow')
            if status != 1:
                raise IsoTpError(f'Invalid flow status {status}')
        raise IsoTpError('Too many flow control WAIT frames')

    # Reception

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        try:
            return await asyncio.wait_for(self._received.get(), self.timeout if timeout is None else timeout)
        except asyncio.TimeoutError:
            raise IsoTpError('Timeout waiting for a response') from None

    async def request(self, payload: bytes, timeout: Optional[float] = None) -> bytes:
        self._drain(self._received)
        await self.send(payload)
        return await self.receive(timeout)

    @staticmethod
    def _drain(queue: asyncio.Queue) -> None:
        while not queue.empty():
            queue.get_nowait()

    def _send_own_flow_control(self) -> None:
        self._send_frame(bytes([0x30, self.block_size, self.st_min]))

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or msg.arbitration_id != self.rx_id or msg.is_extended_id != self.extended or not msg.data:
            return
        data = msg.data
        match data[0] >> 4:
            case 0:
                length, offset = data[0] & 0x0F, 1
                if length == 0 and len(data) > 2:
                    length, offset = data[1], 2
                if 0 < length <= len(data) - offset:
                    self._reception = None
                    self._received.put_nowait(bytes(data[offset : offset + length]))
            case 1:
                if len(data) < 2:
                    return
                length, offset = ((data[0] & 0x0F) << 8) | data[1], 2
                if length == 0 and len(data) >= 6:
                    length, offset = int.from_bytes(data[2:6], 'big'), 6
                self._reception = _Reception(length, bytearray(data[offset:]))
                self._send_own_flow_control()
            case 2:
                reception = self._reception
                if reception is None:
                    return
                if data[0] & 0x0F != reception.next_sn:
                    self._reception = None  # Sequence error: drop the transfer
                    return
                reception.data.extend(data[1:])
                reception.next_sn = (reception.next_sn + 1) & 0x0F
                if len(reception.data) >= reception.length:
                    self._reception = None
                    self._received.put_nowait(bytes(reception.data[: reception.length]))
                    return
                reception.block_count += 1
                if self.block_size and reception.block_count % self.block_size == 0:
                    self._send_own_flow_control()
            case 3:
                if len(data) >= 3:
                    self._flow_control.put_nowait(bytes(data[:3]))
