"""Interactive UDS discovery for reverse engineering: enumerate the services, subfunctions, data identifiers and
routines an ECU exposes, and sweep one byte of a request over a range.

The scans run over any `UdsTransport` (ISO-TP on CAN, the LIN transport layer, DoIP) through a `UdsClient`, which
never raises: each probe becomes a `ScanResult`, classified so the interesting answers (a positive response, a
"security access denied", a wrong-session or wrong-state rejection) stand out from the "not supported" ones.

This is meant for ECUs you own or are authorised to test. Some services change the ECU's state (session changes,
resets, routines, writes): the service scan sends read-only probes, and the GUI keeps the state-changing scans and
the byte sweep behind an explicit choice.

Example:
    ```python
    from can_explorer.core.isotp_client import IsoTpClient
    from can_explorer.core.uds import UdsClient
    from can_explorer.core.uds_scan import scan_data_identifiers

    client = UdsClient(IsoTpClient(session, 0x7E0, 0x7E8), timeout=0.2)
    async for result in scan_data_identifiers(client, 0xF180, 0xF1FF):
        if result.interesting:
            print(f'{result.identifier:04X}: {result.summary}')
    ```
"""

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Iterable, Optional

from can_explorer.core.frames import format_hex
from can_explorer.core.uds import NEGATIVE_RESPONSE, UdsClient, describe_response, nrc_name, service_name

# Scan kinds
SERVICES = 'services'
SUBFUNCTIONS = 'subfunctions'
DATA_IDENTIFIERS = 'dids'
ROUTINES = 'routines'
SWEEP = 'sweep'
FUZZ = 'fuzz'

# The service identifiers worth probing by default: the ISO 14229 request SIDs CANExplorer knows, read-only ones
# first. A full scan can still try every SID 0x00–0xFF.
READ_ONLY_SERVICES = (0x3E, 0x22, 0x19, 0x23, 0x27, 0x10)
KNOWN_SERVICES = (
    0x10,
    0x11,
    0x14,
    0x19,
    0x22,
    0x23,
    0x27,
    0x28,
    0x2E,
    0x2F,
    0x31,
    0x34,
    0x35,
    0x36,
    0x37,
    0x3D,
    0x3E,
    0x85,
)

# Negative response codes that mean the probed item exists but is locked or needs another state; a fuzzer should
# flag these rather than skip them.
_SECURITY_CODES = (0x33, 0x35, 0x36, 0x37)
_CONDITION_CODES = (0x21, 0x22, 0x24, 0x7E, 0x7F)
# Codes that mean "the thing you asked for is not there", per scan kind
_ABSENT_CODES = {
    SERVICES: (0x11,),
    SUBFUNCTIONS: (0x11, 0x12),
    DATA_IDENTIFIERS: (0x11, 0x31),
    ROUTINES: (0x11, 0x31),
    SWEEP: (0x11,),
    # A fuzzer sends malformed and unsupported requests, so a well-formed rejection of one is the boring case
    FUZZ: (0x11, 0x12, 0x13, 0x31, 0x7E, 0x7F),
}

# Categories, most interesting first (the GUI colours and filters by these)
POSITIVE = 'positive'
SECURITY = 'security'
CONDITIONS = 'conditions'
REJECTED = 'rejected'
UNRESPONSIVE = 'unresponsive'  # Fuzzing: no answer where one was expected — a possible hang or crash
ABSENT = 'absent'
NO_RESPONSE = 'no-response'
_INTERESTING = (POSITIVE, SECURITY, CONDITIONS, REJECTED, UNRESPONSIVE)


def classify(kind: str, request: bytes, response: Optional[bytes], error: Optional[str]) -> str:
    """One of POSITIVE, SECURITY, CONDITIONS, REJECTED, UNRESPONSIVE, ABSENT, NO_RESPONSE for a probe's outcome."""
    if error is not None or response is None:
        # For a fuzzer a missing answer is an anomaly worth flagging; for a scan it just means "nothing there"
        return UNRESPONSIVE if kind == FUZZ else NO_RESPONSE
    if response and request and response[0] == request[0] + 0x40:
        return POSITIVE
    if len(response) >= 3 and response[0] == NEGATIVE_RESPONSE:
        nrc = response[2]
        if nrc in _SECURITY_CODES:
            return SECURITY
        if nrc in _CONDITION_CODES:
            return CONDITIONS
        if nrc in _ABSENT_CODES.get(kind, (0x11,)):
            return ABSENT
        return REJECTED
    return REJECTED


@dataclass(frozen=True, slots=True)
class ScanResult:
    kind: str
    identifier: int  # The value scanned: service/subfunction/DID/routine id, or the swept byte value
    request: bytes
    response: Optional[bytes]
    error: Optional[str] = None

    @property
    def category(self) -> str:
        return classify(self.kind, self.request, self.response, self.error)

    @property
    def interesting(self) -> bool:
        """Whether the ECU answered in a way that reveals the probed item exists."""
        return self.category in _INTERESTING

    @property
    def nrc(self) -> Optional[int]:
        """The negative response code, if the answer was a negative response."""
        response = self.response
        if response is not None and len(response) >= 3 and response[0] == NEGATIVE_RESPONSE:
            return response[2]
        return None

    @property
    def summary(self) -> str:
        if self.error is not None:
            return self.error
        if self.response is None:
            return 'no response'
        return describe_response(self.response)


async def _scan(
    client: UdsClient,
    kind: str,
    values: Iterable[int],
    build: Callable[[int], bytes],
    *,
    delay: float = 0.0,
    stop: Optional[asyncio.Event] = None,
) -> AsyncIterator[ScanResult]:
    for value in values:
        if stop is not None and stop.is_set():
            return
        request = build(value)
        result = await client.request(request)
        yield ScanResult(kind, value, request, result.response, result.error)
        if delay:
            await asyncio.sleep(delay)


def scan_services(
    client: UdsClient,
    services: Iterable[int] = KNOWN_SERVICES,
    *,
    delay: float = 0.0,
    stop: Optional[asyncio.Event] = None,
) -> AsyncIterator[ScanResult]:
    """Probe each service identifier with a bare request; anything but 'serviceNotSupported' means it is handled."""
    return _scan(client, SERVICES, services, lambda sid: bytes([sid]), delay=delay, stop=stop)


def scan_subfunctions(
    client: UdsClient,
    service: int,
    start: int = 0x00,
    end: int = 0x7F,
    *,
    delay: float = 0.0,
    stop: Optional[asyncio.Event] = None,
) -> AsyncIterator[ScanResult]:
    """Probe the subfunctions of a service. The suppress-positive-response bit (0x80) is never set, so positive
    answers come back."""
    return _scan(client, SUBFUNCTIONS, range(start, end + 1), lambda sub: bytes([service, sub]), delay=delay, stop=stop)


def scan_data_identifiers(
    client: UdsClient,
    start: int = 0x0000,
    end: int = 0xFFFF,
    *,
    delay: float = 0.0,
    stop: Optional[asyncio.Event] = None,
) -> AsyncIterator[ScanResult]:
    """ReadDataByIdentifier (0x22) over a range of DIDs; a positive response returns the data, a locked DID answers
    'securityAccessDenied'."""
    return _scan(
        client,
        DATA_IDENTIFIERS,
        range(start, end + 1),
        lambda did: bytes([0x22, did >> 8, did & 0xFF]),
        delay=delay,
        stop=stop,
    )


def scan_routines(
    client: UdsClient,
    start: int = 0x0000,
    end: int = 0xFFFF,
    control_type: int = 0x03,
    *,
    delay: float = 0.0,
    stop: Optional[asyncio.Event] = None,
) -> AsyncIterator[ScanResult]:
    """RoutineControl (0x31) over a range of routine identifiers. The default control type is 'request results'
    (0x03), which does not start anything; pass 0x01 to start routines (state-changing)."""
    return _scan(
        client,
        ROUTINES,
        range(start, end + 1),
        lambda rid: bytes([0x31, control_type, rid >> 8, rid & 0xFF]),
        delay=delay,
        stop=stop,
    )


def sweep(
    client: UdsClient,
    template: bytes,
    position: int,
    start: int = 0x00,
    end: int = 0xFF,
    width: int = 1,
    *,
    delay: float = 0.0,
    stop: Optional[asyncio.Event] = None,
) -> AsyncIterator[ScanResult]:
    """Send `template` repeatedly, replacing `width` bytes at `position` with each value in the range: a free-form,
    one-byte-at-a-time fuzz of a known request (e.g. iterate the subfunction of a custom service)."""
    if position < 0 or position + width > len(template):
        raise ValueError('The swept position is outside the template')
    if not 1 <= width <= 2:
        raise ValueError('Sweep width must be 1 or 2 bytes')

    def build(value: int) -> bytes:
        return template[:position] + value.to_bytes(width, 'big') + template[position + width :]

    return _scan(client, SWEEP, range(start, end + 1), build, delay=delay, stop=stop)


def describe_probe(result: ScanResult) -> str:
    """A one-line label for a result, for logs: what was asked and what came back."""
    if result.kind == SERVICES:
        asked = service_name(result.identifier)
    elif result.kind == DATA_IDENTIFIERS:
        asked = f'DID 0x{result.identifier:04X}'
    elif result.kind == ROUTINES:
        asked = f'routine 0x{result.identifier:04X}'
    elif result.kind == SUBFUNCTIONS:
        asked = f'subfunction 0x{result.identifier:02X}'
    else:
        asked = format_hex(result.request)
    detail = result.summary
    if result.nrc is not None:
        detail = nrc_name(result.nrc)
    return f'{asked}: {detail}'
