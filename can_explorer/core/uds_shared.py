"""One ISO-TP endpoint shared by everything talking to an ECU, so that messages the ECU sends between requests
(ResponseOnEvent responses, periodic data on the diagnostic channel, late answers) are received too.

Receiving a segmented message means answering its first frame with a flow control frame. Two ISO-TP receivers on the
same response identifier would both answer, and an ECU seeing two flow control frames may abort the transfer. So
while listening, every user — requests, tester present, flashing, scans — gets a `SharedTransport` of one
`SharedIsoTp`: a received message goes to the most recently opened transport that receives, and with none open to
`on_idle`.
"""

import asyncio
from typing import Callable, List, Optional

from can_explorer.core.isotp_client import IsoTpClient, IsoTpError


class SharedTransport:
    """A UDS transport of a SharedIsoTp; `receiving` False for send-only users (tester present)."""

    def __init__(self, shared: 'SharedIsoTp', receiving: bool = True):
        self._shared = shared
        self.receiving = receiving
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._open = True
        shared._users.append(self)

    async def send(self, payload: bytes) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        await self._shared.client.send(payload)

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        try:
            return await asyncio.wait_for(
                self._queue.get(), self._shared.client.timeout if timeout is None else timeout
            )
        except asyncio.TimeoutError:
            raise IsoTpError('Timeout waiting for a response') from None

    async def request(self, payload: bytes, timeout: Optional[float] = None) -> bytes:
        await self.send(payload)
        return await self.receive(timeout)

    def close(self) -> None:
        if self._open:
            self._open = False
            self._shared._users.remove(self)

    def __enter__(self) -> 'SharedTransport':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class SharedIsoTp:
    def __init__(
        self,
        session,
        tx_id: int,
        rx_id: int,
        extended: bool = False,
        fd: bool = False,
        tx_address: Optional[int] = None,
        rx_address: Optional[int] = None,
        on_idle: Optional[Callable[[bytes], None]] = None,
    ):
        """`tx_address`/`rx_address`: the address byte of extended or mixed addressing, see IsoTpClient."""
        self.session = session
        self.key = (tx_id, rx_id, extended, fd, tx_address, rx_address)
        self.on_idle = on_idle
        self._users: List[SharedTransport] = []
        self.client = IsoTpClient(
            session,
            tx_id,
            rx_id,
            extended,
            fd=fd,
            on_payload=self._deliver,
            tx_address=tx_address,
            rx_address=rx_address,
        )

    def matches(
        self,
        session,
        tx_id: int,
        rx_id: int,
        extended: bool,
        fd: bool,
        tx_address: Optional[int] = None,
        rx_address: Optional[int] = None,
    ) -> bool:
        return session is self.session and self.key == (tx_id, rx_id, extended, fd, tx_address, rx_address)

    def transport(self, receiving: bool = True) -> SharedTransport:
        return SharedTransport(self, receiving)

    @property
    def busy(self) -> bool:
        """Whether a receiving user is open (received messages go to it, not to `on_idle`)."""
        return any(user.receiving for user in self._users)

    def _deliver(self, payload: bytes) -> None:
        for user in reversed(self._users):
            if user.receiving:
                user._queue.put_nowait(payload)
                return
        if self.on_idle is not None:
            self.on_idle(payload)

    def close(self) -> None:
        self.client.close()
