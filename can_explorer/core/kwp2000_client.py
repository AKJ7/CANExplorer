"""Active Keyword Protocol 2000 (KWP2000, ISO 14230-3) client: the diagnostic protocol before UDS, still the one of
VW-group modules on TP 2.0 and of K-Line ECUs.

It runs over any transport offering `send(payload)` and `receive(timeout)` coroutines (the `UdsTransport`
protocol): `Tp20Client` (`core/tp20.py`, VW TP 2.0 on CAN), `IsoTpClient` (KWP2000 on ISO-TP) or `KLineTransport`
below (a K-Line bus through the `elm327_kline` interface).

Why not `UdsClient`. KWP2000 shares the response convention of UDS — a positive response is the service id plus
0x40, a negative one is 0x7F, the service id and a response code, and 0x78 means "response pending" — but not its
sub-function rule: UDS reads bit 7 of a sub-function byte as suppressPosRspMsgIndicationBit, while KWP2000 session
and routine identifiers use the whole byte (the VW session 0x89 has bit 7 set), so a UDS client would stop waiting
for the answer. The service names and response codes are those of the passive decoder (`core/protocols/kwp2000.py`).

Structured calls cover the services whose layout is the service id plus one identifier byte, as the TP 2.0 source's
example exchange shows (`10 89` → `50 89`, `21 01` → `61 01 …`): sessions, ECU reset, identification, security
access, local identifiers (read and write), routines, and the measuring blocks (`core/measuring_blocks.py`). Not
structured: the trouble-code services (0x18 ReadDTCByStatus, 0x17 ReadStatusOfDTC, 0x14
ClearDiagnosticInformation) — their request and response layouts are in the paywalled ISO 14230-3 and could not be
confirmed from an open source, so they are left to `request()` with the bytes your ECU documentation gives, rather
than guessed. VW's own 0x27 seed/key algorithms are not included: `security_access` takes a `compute_key(level,
seed)` function, e.g. from the key store.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import can

from can_explorer.core.frames import format_hex
from can_explorer.core.protocols.kwp2000 import describe_payload, nrc_name, service_name
from can_explorer.core.session import SessionError
from can_explorer.core.uds import UdsTransport

NEGATIVE_RESPONSE = 0x7F
RESPONSE_PENDING = 0x78
P2_TIMEOUT = 1.0  # Wait for a response
P2_EXTENDED_TIMEOUT = 5.0  # Wait after "response pending"
KEEP_ALIVE_INTERVAL = 2.0  # Idle time after which KwpKeepAlive sends TesterPresent

# Services whose positive response echoes the identifier byte after the service id (session, reset mode,
# identification option, security level, local identifier, routine identifier)
ECHOED_SERVICES = (0x10, 0x11, 0x1A, 0x21, 0x27, 0x30, 0x31, 0x32, 0x33, 0x3B)

TESTER_ADDRESS = 0xF1  # The usual K-Line tester (source) address
KLINE_PHYSICAL = 0x80  # ISO 14230-2 format byte: physical addressing (bits 7-6 = 10)...
KLINE_FUNCTIONAL = 0xC0  # ...or functional (11); bits 5-0 carry the length
KLINE_MAX_PAYLOAD = 0x3F  # The six length bits of the format byte (longer needs a length byte the adapter can't set)


class KwpError(Exception):
    pass


class KwpNegativeResponse(KwpError):
    def __init__(self, service: int, code: int):
        self.service, self.code = service, code
        super().__init__(f'Negative response to {service_name(service)}: {nrc_name(code)}')


def _is_pending(response: bytes) -> bool:
    return len(response) >= 3 and response[0] == NEGATIVE_RESPONSE and response[2] == RESPONSE_PENDING


def is_response_to(request: bytes, response: bytes) -> bool:
    """Whether `response` answers `request`: a negative response echoing its service, or a positive response
    (service + 0x40) that, for the services in ECHOED_SERVICES, echoes the identifier byte too."""
    if not request or not response:
        return False
    if response[0] == NEGATIVE_RESPONSE:
        return len(response) >= 2 and response[1] == request[0]
    if response[0] != (request[0] + 0x40) & 0xFF:
        return False
    if request[0] in ECHOED_SERVICES and len(request) >= 2:
        return len(response) >= 2 and response[1] == request[1]
    return True


@dataclass(frozen=True, slots=True)
class KwpResult:
    request: bytes
    response: Optional[bytes]
    error: Optional[str] = None

    @property
    def positive(self) -> bool:
        return bool(self.response) and self.response[0] == (self.request[0] + 0x40) & 0xFF

    @property
    def description(self) -> str:
        if self.error:
            return self.error
        if self.response is None:
            return 'No response'
        return describe_payload(self.response) or format_hex(self.response)


class KwpClient:
    """KWP2000 client over a transport (TP 2.0, ISO-TP, K-Line).

    `timeout` is the wait for a response and `extended_timeout` the wait after "response pending" (0x78). Answers
    that do not match the request (stale traffic on a shared identifier) are skipped and passed to the
    `unsolicited` callbacks. One request runs at a time: concurrent callers (e.g. `KwpKeepAlive` beside a user's
    request) wait their turn.
    """

    def __init__(
        self, transport: UdsTransport, timeout: float = P2_TIMEOUT, extended_timeout: float = P2_EXTENDED_TIMEOUT
    ):
        self.transport = transport
        self.timeout, self.extended_timeout = timeout, extended_timeout
        self.unsolicited: List[Callable[[bytes], None]] = []
        self.last_activity = time.monotonic()  # When the last request finished
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def request(self, payload: bytes) -> KwpResult:
        """Send a request and wait for the final response, skipping 'response pending'. Never raises: transport
        errors are reported in the result."""
        async with self._lock:
            try:
                return await self._request(bytes(payload))
            finally:
                self.last_activity = time.monotonic()

    async def _request(self, payload: bytes) -> KwpResult:
        try:
            await self.transport.send(payload)
            response = await self.transport.receive(self.timeout)
            while _is_pending(response) or not is_response_to(payload, response):
                pending = _is_pending(response)
                if not pending:
                    for listener in list(self.unsolicited):
                        listener(response)
                response = await self.transport.receive(self.extended_timeout if pending else self.timeout)
        except Exception as e:  # Transport errors (timeouts, bus errors, a closed channel)
            return KwpResult(payload, None, str(e) or type(e).__name__)
        return KwpResult(payload, bytes(response))

    async def call(self, payload: bytes) -> bytes:
        """Like request(), but returns the positive response and raises KwpError otherwise."""
        result = await self.request(payload)
        if result.error:
            raise KwpError(result.error)
        response = result.response or b''
        if len(response) >= 3 and response[0] == NEGATIVE_RESPONSE:
            raise KwpNegativeResponse(response[1], response[2])
        if not result.positive:
            raise KwpError(f'Unexpected response {format_hex(response)} to {service_name(payload[0])}')
        return response

    # Services

    async def start_diagnostic_session(self, session: int, baud_rate: Optional[int] = None) -> bytes:
        """StartDiagnosticSession (0x10), e.g. 0x89 on VW-group modules; returns the bytes after the echo.
        `baud_rate` adds the optional baud-rate identifier byte."""
        payload = bytes([0x10, session]) + (bytes([baud_rate]) if baud_rate is not None else b'')
        return (await self.call(payload))[2:]

    async def stop_diagnostic_session(self) -> None:
        await self.call(bytes([0x20]))

    async def ecu_reset(self, mode: int = 0x01) -> None:
        await self.call(bytes([0x11, mode]))

    async def tester_present(self) -> None:
        await self.call(bytes([0x3E]))

    async def read_ecu_identification(self, option: int) -> bytes:
        """ReadECUIdentification (0x1A) with an identification option; returns the data after the echo."""
        return (await self.call(bytes([0x1A, option])))[2:]

    async def security_access(self, level: int, compute_key: Callable[[int, bytes], bytes]) -> bool:
        """SecurityAccess (0x27): request the seed of an odd level, send compute_key(level, seed) as level + 1.
        Returns False if the level was already unlocked (a zero seed)."""
        if level % 2 == 0:
            raise KwpError('Security access levels for requesting a seed are odd')
        seed = (await self.call(bytes([0x27, level])))[2:]
        if not any(seed):
            return False
        key = bytes(compute_key(level, bytes(seed)))
        await self.call(bytes([0x27, level + 1]) + key)
        return True

    async def read_local_identifier(self, identifier: int) -> bytes:
        """ReadDataByLocalIdentifier (0x21); returns the data after the echoed identifier. On VW-group modules the
        identifier is a measuring-block group: see `core/measuring_blocks.py` to parse it."""
        return (await self.call(bytes([0x21, identifier])))[2:]

    async def write_local_identifier(self, identifier: int, data: bytes) -> None:
        """WriteDataByLocalIdentifier (0x3B)."""
        await self.call(bytes([0x3B, identifier]) + bytes(data))

    async def start_routine(self, identifier: int, data: bytes = b'') -> bytes:
        """StartRoutineByLocalIdentifier (0x31); returns the bytes after the echoed identifier."""
        return (await self.call(bytes([0x31, identifier]) + bytes(data)))[2:]

    async def stop_routine(self, identifier: int, data: bytes = b'') -> bytes:
        """StopRoutineByLocalIdentifier (0x32)."""
        return (await self.call(bytes([0x32, identifier]) + bytes(data)))[2:]

    async def routine_results(self, identifier: int) -> bytes:
        """RequestRoutineResultsByLocalIdentifier (0x33)."""
        return (await self.call(bytes([0x33, identifier])))[2:]


class KwpKeepAlive:
    """Keeps a KWP2000 diagnostic session open by sending TesterPresent (0x3E) after `interval` seconds without a
    request — on TP 2.0 the channel test keeps the *channel* open, not the session. Its answers are not checked
    (a module that rejects TesterPresent is left as it is); `sent` counts the requests."""

    def __init__(self, client: KwpClient, interval: float = KEEP_ALIVE_INTERVAL):
        self.client = client
        self.interval = interval
        self.sent = 0
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name='KWP TesterPresent')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            idle = time.monotonic() - self.client.last_activity
            if idle < self.interval or self.client.busy:
                await asyncio.sleep(max(self.interval - idle, self.interval / 10))
                continue
            await self.client.request(bytes([0x3E]))  # Never raises
            self.sent += 1


class KLineTransport:
    """KWP2000 over a K-Line bus read through the `elm327_kline` interface, where each frame is one whole service
    payload with the three ISO 14230-2 header bytes — format, target, source — in the extended identifier (the
    adapter adds and checks the checksum). Requests go to `target` from `source`; the answers are the frames from
    `target` to `source`. At most 63 payload bytes a request, the length the format byte can carry."""

    def __init__(self, session, target: int, source: int = TESTER_ADDRESS, functional: bool = False):
        self.session = session
        self.target, self.source, self.functional = target, source, functional
        self._received: asyncio.Queue[bytes] = asyncio.Queue()
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def __enter__(self) -> 'KLineTransport':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or not msg.is_extended_id or not msg.data:
            return
        if (msg.arbitration_id >> 8) & 0xFF == self.source and msg.arbitration_id & 0xFF == self.target:
            self._received.put_nowait(bytes(msg.data))

    async def send(self, payload: bytes) -> None:
        if not 0 < len(payload) <= KLINE_MAX_PAYLOAD:
            raise KwpError(f'A K-Line request is 1-{KLINE_MAX_PAYLOAD} bytes, got {len(payload)}')
        fmt = (KLINE_FUNCTIONAL if self.functional else KLINE_PHYSICAL) | len(payload)
        arbitration_id = (fmt << 16) | (self.target << 8) | self.source
        msg = can.Message(
            arbitration_id=arbitration_id, is_extended_id=True, is_fd=len(payload) > 8, data=bytes(payload)
        )  # Marked CAN FD over eight bytes, as the elm327_kline backend marks the frames it receives
        try:
            self.session.send(msg)
        except SessionError as e:
            raise KwpError(str(e)) from e

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        try:
            return await asyncio.wait_for(self._received.get(), timeout)
        except asyncio.TimeoutError:
            raise KwpError('Timeout waiting for a response') from None
