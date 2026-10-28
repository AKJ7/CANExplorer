"""GMLAN diagnostics (GMW3110): the GM diagnostic protocol used by Opel/Vauxhall and other GM vehicles.

GMLAN runs over normal CAN frames with a transport (USDT) that uses the same protocol-control bytes as ISO-TP,
so it works over `IsoTpClient`. The service layer, though, is GM-specific: one-byte data identifiers, a different
set of service IDs (ReadDataByIdentifier 0x1A, ReturnToNormalMode 0x20, ReadDiagnosticInformation 0xA9, …) and
the GM DTC format (a two-byte code with a failure-type byte). This module is the GMLAN counterpart of `uds.py`.

The client works over any transport offering `send(payload)` and `receive(timeout)` coroutines, the same
`IsoTpClient` / `LinTpClient` used for UDS.
"""

import asyncio
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol

import can

from can_explorer.core.frames import format_hex

# GMW3110 diagnostic services (request service identifier -> name); the positive response id is the request + 0x40
SERVICES = {
    0x04: 'ClearDiagnosticInformation',
    0x10: 'InitiateDiagnosticOperation',
    0x12: 'ReadFailureRecordData',
    0x1A: 'ReadDataByIdentifier',
    0x20: 'ReturnToNormalMode',
    0x22: 'ReadDataByParameterIdentifier',
    0x23: 'ReadMemoryByAddress',
    0x27: 'SecurityAccess',
    0x28: 'DisableNormalCommunication',
    0x2C: 'DynamicallyDefineMessage',
    0x2D: 'DefinePIDByAddress',
    0x34: 'RequestDownload',
    0x36: 'TransferData',
    0x3B: 'WriteDataByIdentifier',
    0x3E: 'TesterPresent',
    0xA2: 'ReportProgrammedState',
    0xA5: 'ProgrammingMode',
    0xA9: 'ReadDiagnosticInformation',
    0xAA: 'ReadDataByPacketIdentifier',
    0xAE: 'DeviceControl',
}

NEGATIVE_RESPONSE_CODES = {
    0x11: 'serviceNotSupported',
    0x12: 'subFunctionNotSupported',
    0x22: 'conditionsNotCorrect',
    0x31: 'requestOutOfRange',
    0x33: 'securityAccessDenied',
    0x35: 'invalidKey',
    0x36: 'exceedNumberOfAttempts',
    0x37: 'requiredTimeDelayNotExpired',
    0x78: 'requestCorrectlyReceived-ResponsePending',
    0x81: 'schedulerFull',
    0x83: 'voltageOutOfRange',
    0x85: 'generalProgrammingFailure',
    0x89: 'deviceTypeError',
    0x99: 'readyForDownload-DTCStored',
    0xE3: 'deviceControlLimitsExceeded',
}

# InitiateDiagnosticOperation (0x10) sub-levels
DIAGNOSTIC_OPERATIONS = {
    0x02: 'disableAllDTCs',
    0x03: 'enableDTCsDuringDeviceControl',
    0x04: 'wakeUpLinks',
}

NEGATIVE_RESPONSE = 0x7F
RESPONSE_PENDING = 0x78

P2_TIMEOUT = 1.0
P2_EXTENDED_TIMEOUT = 5.0
TESTER_PRESENT_INTERVAL = 2.0

# Typical GMLAN diagnostic CAN identifiers (11 bit). Physical responses are the request id plus 0x400; functional
# (all-node) requests go on 0x101. Real buses vary, so the connection view lets these be set.
DEFAULT_REQUEST_ID = 0x241
DEFAULT_RESPONSE_ID = 0x641
FUNCTIONAL_REQUEST_ID = 0x101


def service_name(sid: int) -> str:
    return SERVICES.get(sid, f'0x{sid:02X}')


def nrc_name(code: int) -> str:
    return NEGATIVE_RESPONSE_CODES.get(code, f'0x{code:02X}')


def format_dtc(code: int, failure_type: int) -> str:
    """A GMLAN DTC as SAE J2012 text with the failure-type byte, e.g. (0x0123, 0x04) -> 'P0123-04'."""
    letter = 'PCBU'[(code >> 14) & 0x3]
    return f'{letter}{(code >> 12) & 0x3}{code & 0xFFF:03X}-{failure_type & 0xFF:02X}'


@dataclass(frozen=True, slots=True)
class GmlanDtc:
    code: int  # Two-byte DTC number
    failure_type: int  # Failure type byte (FTB)
    status: int

    @property
    def text(self) -> str:
        return format_dtc(self.code, self.failure_type)


class GmlanTransport(Protocol):
    async def send(self, payload: bytes) -> None: ...

    async def receive(self, timeout: Optional[float] = None) -> bytes: ...


class GmlanError(Exception):
    pass


class GmlanNegativeResponse(GmlanError):
    def __init__(self, service: int, code: int):
        self.service, self.code = service, code
        super().__init__(f'{service_name(service)}: negative response {nrc_name(code)} (0x{code:02X})')


def describe_request(payload: bytes) -> str:
    if not payload:
        return '(empty)'
    return f'{service_name(payload[0])} {format_hex(payload[1:])}'.strip()


def describe_response(payload: bytes) -> str:
    if not payload:
        return 'No response'
    if payload[0] == NEGATIVE_RESPONSE and len(payload) >= 3:
        return f'Negative response to {service_name(payload[1])}: {nrc_name(payload[2])} (0x{payload[2]:02X})'
    if payload[0] >= 0x40:
        return f'{service_name(payload[0] - 0x40)} response {format_hex(payload[1:])}'.strip()
    return format_hex(payload)


@dataclass(frozen=True, slots=True)
class GmlanResult:
    request: bytes
    response: Optional[bytes]
    error: Optional[str] = None

    @property
    def positive(self) -> bool:
        if not self.response or self.response[0] == NEGATIVE_RESPONSE:
            return False
        return self.response[0] == self.request[0] + 0x40

    @property
    def description(self) -> str:
        if self.error:
            return self.error
        if self.response is None:
            return 'No response expected'
        return describe_response(self.response)


class GmlanClient:
    """GMLAN (GMW3110) diagnostic client over a transport (ISO-TP on CAN, the LIN transport layer)."""

    def __init__(self, transport: GmlanTransport, timeout: float = P2_TIMEOUT):
        self.transport = transport
        self.timeout = timeout

    async def request(self, payload: bytes) -> GmlanResult:
        """Send a request and wait for the final response, skipping 'response pending' answers. Never raises."""
        payload = bytes(payload)
        try:
            await self.transport.send(payload)
            response = await self.transport.receive(self.timeout)
            while len(response) >= 3 and response[0] == NEGATIVE_RESPONSE and response[2] == RESPONSE_PENDING:
                response = await self.transport.receive(P2_EXTENDED_TIMEOUT)
        except Exception as e:  # Transport errors (timeouts, bus errors)
            return GmlanResult(payload, None, str(e))
        return GmlanResult(payload, response)

    async def call(self, payload: bytes) -> bytes:
        """Like request(), but returns the positive response and raises GmlanError otherwise."""
        result = await self.request(payload)
        if result.error:
            raise GmlanError(result.error)
        response = result.response or b''
        if response and response[0] == NEGATIVE_RESPONSE and len(response) >= 3:
            raise GmlanNegativeResponse(response[1], response[2])
        if not response or response[0] != payload[0] + 0x40:
            raise GmlanError(f'Unexpected response {format_hex(response)} to {describe_request(payload)}')
        return response

    async def initiate_diagnostic_operation(self, level: int = 0x02) -> None:
        await self.call(bytes([0x10, level]))

    async def return_to_normal_mode(self) -> None:
        await self.call(bytes([0x20]))

    async def tester_present(self) -> None:
        await self.call(bytes([0x3E]))

    async def disable_normal_communication(self) -> None:
        await self.call(bytes([0x28]))

    async def read_data_by_identifier(self, did: int) -> bytes:
        """ReadDataByIdentifier (0x1A): GMLAN uses a single-byte data identifier."""
        response = await self.call(bytes([0x1A, did & 0xFF]))
        if len(response) < 2 or response[1] != (did & 0xFF):
            raise GmlanError(f'Response for DID 0x{response[1] if len(response) > 1 else 0:02X}, expected 0x{did:02X}')
        return response[2:]

    async def write_data_by_identifier(self, did: int, data: bytes) -> None:
        await self.call(bytes([0x3B, did & 0xFF]) + bytes(data))

    async def read_data_by_parameter(self, pid: int) -> bytes:
        """ReadDataByParameterIdentifier (0x22): a two-byte parameter identifier."""
        response = await self.call(bytes([0x22, pid >> 8, pid & 0xFF]))
        return response[1:]

    async def security_access(self, level: int, compute_key: Callable[[int, bytes], bytes]) -> bool:
        """Unlock a security level (odd request-seed level). Returns False if it was already unlocked."""
        if level % 2 == 0:
            raise GmlanError('Security access levels for requesting a seed are odd')
        seed = (await self.call(bytes([0x27, level])))[2:]
        if not any(seed):
            return False  # A zero seed means the level is already unlocked
        key = bytes(compute_key(level, bytes(seed)))
        await self.call(bytes([0x27, level + 1]) + key)
        return True

    async def report_programmed_state(self) -> int:
        response = await self.call(bytes([0xA2]))
        return response[1] if len(response) > 1 else 0

    async def read_dtcs(self, status_mask: int = 0xFF) -> List[GmlanDtc]:
        """ReadDiagnosticInformation (0xA9) readStatusOfDTCByStatusMask (0x80). The records are four bytes each:
        DTC high and low byte, the failure-type byte and the status byte."""
        response = await self.call(bytes([0xA9, 0x80, status_mask & 0xFF]))
        records = response[2:]
        return [
            GmlanDtc((records[i] << 8) | records[i + 1], records[i + 2], records[i + 3])
            for i in range(0, len(records) - 3, 4)
        ]

    async def clear_dtcs(self) -> None:
        """ClearDiagnosticInformation (0x04)."""
        await self.call(bytes([0x04]))

    async def device_control(self, cpid: int, data: bytes = b'') -> bytes:
        """DeviceControl (0xAE): actuate an output by its control parameter identifier (CPID)."""
        return (await self.call(bytes([0xAE, cpid & 0xFF]) + bytes(data)))[1:]


async def gmlan_request(transport: GmlanTransport, payload: bytes) -> GmlanResult:
    return await GmlanClient(transport).request(payload)


class GmlanKeepAlive:
    """Keeps diagnostics alive by sending TesterPresent (0x3E) periodically."""

    def __init__(self, transport: GmlanTransport, interval: float = TESTER_PRESENT_INTERVAL):
        self._transport = transport
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self.sent = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name='GmlanTesterPresent')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._transport.send(bytes([0x3E]))
                self.sent += 1
            except Exception:
                pass
            await asyncio.sleep(self._interval)


class GmlanDecoder:
    """Annotates GMLAN diagnostic frames in the trace, pairing requests with responses by identifier."""

    def __init__(self, request_id: int = DEFAULT_REQUEST_ID, response_id: int = DEFAULT_RESPONSE_ID):
        self.request_id, self.response_id = request_id, response_id

    def describe(self, msg: can.Message) -> Optional[str]:
        if msg.arbitration_id not in (self.request_id, self.response_id, FUNCTIONAL_REQUEST_ID):
            return None
        data = bytes(msg.data)
        if len(data) < 2 or (data[0] & 0xF0):  # Only single-frame USDT (PCI 0x0L) is annotated here
            return None
        payload = data[1 : 1 + data[0]]
        if not payload:
            return None
        if msg.arbitration_id == self.response_id:
            return f'GMLAN ← {describe_response(payload)}'
        return f'GMLAN → {describe_request(payload)}'
