"""XCP on Ethernet (UDP/IP or TCP/IP): the master's transport and a simulated slave server.

Every XCP packet is framed by a 4-byte header: LEN (the packet's length) and CTR (a counter), both 16-bit Intel.
A UDP datagram may carry several framed packets. The transport has the interface `XcpClient` expects from a
`CanCommandClient` (send/receive/request/drain/close, `unsolicited`, `max_payload`).
"""

import asyncio
import time
from typing import Callable, Optional, Tuple

from can_explorer.core.can_command import CanCommandError, CanCommandTimeout
from can_explorer.core.xcp_slave import XcpSlave

HEADER = 4
MAX_PACKET = 0xFFFF
DEFAULT_PORT = 5555


def frame(counter: int, packet: bytes) -> bytes:
    return len(packet).to_bytes(2, 'little') + (counter & 0xFFFF).to_bytes(2, 'little') + bytes(packet)


def unframe(data: bytes):
    """Yield the packets of a datagram or stream chunk (complete ones only)."""
    offset = 0
    while offset + HEADER <= len(data):
        length = int.from_bytes(data[offset : offset + 2], 'little')
        if offset + HEADER + length > len(data):
            break
        yield bytes(data[offset + HEADER : offset + HEADER + length])
        offset += HEADER + length


class XcpEthernetTransport:
    """XCP on UDP or TCP to `host`:`port`. Call `await open()` before use."""

    max_payload = 255  # MAX_CTO is one byte; MAX_DTO can be larger but the client caps both to this

    def __init__(self, host: str, port: int = DEFAULT_PORT, protocol: str = 'udp', timeout: float = 1.0):
        self.host = host
        self.port = port
        self.protocol = protocol.lower()
        if self.protocol not in ('udp', 'tcp'):
            raise ValueError(f'XCP on Ethernet runs on UDP or TCP, not {protocol!r}')
        self.timeout = timeout
        self.unsolicited: Optional[Callable[[bytes, float], bool]] = None
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()
        self._counter = 0
        self._datagram: Optional[asyncio.DatagramTransport] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader_task: Optional[asyncio.Task] = None

    @property
    def description(self) -> str:
        return f'XCP on {self.protocol.upper()} {self.host}:{self.port}'

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            if self.protocol == 'udp':
                transport = self

                class _Protocol(asyncio.DatagramProtocol):
                    def datagram_received(self, data, addr):
                        transport._received(data)

                self._datagram, _ = await loop.create_datagram_endpoint(_Protocol, remote_addr=(self.host, self.port))
            else:
                reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), self.timeout
                )
                self._reader_task = loop.create_task(self._read(reader))
        except (OSError, asyncio.TimeoutError) as e:
            raise CanCommandError(f'Cannot reach {self.description}: {e}') from e

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                header = await reader.readexactly(HEADER)
                length = int.from_bytes(header[:2], 'little')
                self._dispatch(await reader.readexactly(length))
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            return

    def _received(self, data: bytes) -> None:
        for packet in unframe(data):
            self._dispatch(packet)

    def _dispatch(self, packet: bytes) -> None:
        if not packet:
            return
        # Timestamps on the clock of CAN frame timestamps
        if self.unsolicited is not None and self.unsolicited(packet, time.time()):
            return
        self._responses.put_nowait(packet)

    def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._datagram is not None:
            self._datagram.close()
            self._datagram = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def drain(self) -> None:
        while not self._responses.empty():
            self._responses.get_nowait()

    def send(self, data: bytes) -> None:
        self._counter = (self._counter + 1) & 0xFFFF
        packet = frame(self._counter, data)
        if self._datagram is not None:
            self._datagram.sendto(packet)
        elif self._writer is not None:
            self._writer.write(packet)
        else:
            raise CanCommandError(f'{self.description} is not open')

    async def request(self, data: bytes, timeout: Optional[float] = None) -> bytes:
        self.drain()
        self.send(data)
        return await self.receive(timeout)

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        try:
            return await asyncio.wait_for(self._responses.get(), self.timeout if timeout is None else timeout)
        except asyncio.TimeoutError:
            raise CanCommandTimeout(f'No response from {self.description}') from None


class XcpEthernetSlave:
    """A simulated XCP slave serving UDP or TCP on `host` (an ephemeral port by default; `port` after start).
    Keyword options configure the `XcpSlave` engine (max_cto defaults to 255 and max_dto to 1024 here)."""

    def __init__(self, host: str = '127.0.0.1', port: int = 0, protocol: str = 'udp', **options):
        self.host = host
        self.port = port
        self.protocol = protocol.lower()
        options.setdefault('max_cto', 255)
        options.setdefault('max_dto', 1024)
        self.slave = XcpSlave(**options)
        self._counter = 0
        self._udp: Optional[asyncio.DatagramTransport] = None
        self._peer: Optional[Tuple[str, int]] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    @property
    def memory(self) -> bytearray:
        return self.slave.memory

    def _send(self, packet: bytes) -> None:
        self._counter = (self._counter + 1) & 0xFFFF
        data = frame(self._counter, packet)
        if self._udp is not None and self._peer is not None:
            self._udp.sendto(data, self._peer)
        elif self._writer is not None:
            self._writer.write(data)

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        self.slave.send = self._send
        if self.protocol == 'udp':
            server = self

            class _Protocol(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    server._peer = addr
                    for packet in unframe(data):
                        server.slave.handle(packet)

            self._udp, _ = await loop.create_datagram_endpoint(_Protocol, local_addr=(self.host, self.port))
            self.port = self._udp.get_extra_info('sockname')[1]
        else:
            self._server = await asyncio.start_server(self._client, self.host, self.port)
            self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        try:
            while True:
                header = await reader.readexactly(HEADER)
                self.slave.handle(await reader.readexactly(int.from_bytes(header[:2], 'little')))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            if self._writer is writer:
                self._writer = None

    async def stop(self) -> None:
        self.slave.close()
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
