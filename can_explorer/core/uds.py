"""Unified Diagnostic Services (ISO 14229-1): service client, response decoding and a passive decoder.

The client works over any transport offering `send(payload)` and `receive(timeout)` coroutines:
ISO-TP on CAN (IsoTpClient) or the LIN transport layer (LinTpClient).
"""

import asyncio
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple

import can

from can_explorer.core.frames import format_hex

SERVICES = {
    0x10: 'DiagnosticSessionControl',
    0x11: 'ECUReset',
    0x14: 'ClearDiagnosticInformation',
    0x19: 'ReadDTCInformation',
    0x22: 'ReadDataByIdentifier',
    0x23: 'ReadMemoryByAddress',
    0x27: 'SecurityAccess',
    0x28: 'CommunicationControl',
    0x2E: 'WriteDataByIdentifier',
    0x2F: 'InputOutputControlByIdentifier',
    0x31: 'RoutineControl',
    0x34: 'RequestDownload',
    0x35: 'RequestUpload',
    0x36: 'TransferData',
    0x37: 'RequestTransferExit',
    0x3D: 'WriteMemoryByAddress',
    0x3E: 'TesterPresent',
    0x85: 'ControlDTCSetting',
}

NEGATIVE_RESPONSE_CODES = {
    0x10: 'generalReject',
    0x11: 'serviceNotSupported',
    0x12: 'subFunctionNotSupported',
    0x13: 'incorrectMessageLengthOrInvalidFormat',
    0x14: 'responseTooLong',
    0x21: 'busyRepeatRequest',
    0x22: 'conditionsNotCorrect',
    0x24: 'requestSequenceError',
    0x25: 'noResponseFromSubnetComponent',
    0x26: 'failurePreventsExecutionOfRequestedAction',
    0x31: 'requestOutOfRange',
    0x33: 'securityAccessDenied',
    0x35: 'invalidKey',
    0x36: 'exceedNumberOfAttempts',
    0x37: 'requiredTimeDelayNotExpired',
    0x70: 'uploadDownloadNotAccepted',
    0x71: 'transferDataSuspended',
    0x72: 'generalProgrammingFailure',
    0x73: 'wrongBlockSequenceCounter',
    0x78: 'requestCorrectlyReceived-ResponsePending',
    0x7E: 'subFunctionNotSupportedInActiveSession',
    0x7F: 'serviceNotSupportedInActiveSession',
}

SESSIONS = {0x01: 'default', 0x02: 'programming', 0x03: 'extended', 0x04: 'safety system'}
RESET_TYPES = {0x01: 'hard reset', 0x02: 'key off on reset', 0x03: 'soft reset'}
ROUTINE_CONTROL_TYPES = {0x01: 'start', 0x02: 'stop', 0x03: 'request results'}

# Standardised data identifiers (ISO 14229-1 annex C)
DATA_IDENTIFIERS = {
    0xF180: 'Boot software identification',
    0xF181: 'Application software identification',
    0xF182: 'Application data identification',
    0xF183: 'Boot software fingerprint',
    0xF184: 'Application software fingerprint',
    0xF186: 'Active diagnostic session',
    0xF187: 'Spare part number',
    0xF188: 'ECU software number',
    0xF189: 'ECU software version',
    0xF18A: 'System supplier identifier',
    0xF18B: 'ECU manufacturing date',
    0xF18C: 'ECU serial number',
    0xF190: 'VIN',
    0xF191: 'ECU hardware number',
    0xF192: 'Supplier ECU hardware number',
    0xF193: 'Supplier ECU hardware version',
    0xF194: 'Supplier ECU software number',
    0xF195: 'Supplier ECU software version',
    0xF197: 'System name',
    0xF198: 'Repair shop code',
    0xF199: 'Programming date',
}

DTC_STATUS_BITS = (
    'testFailed',
    'testFailedThisOperationCycle',
    'pendingDTC',
    'confirmedDTC',
    'testNotCompletedSinceLastClear',
    'testFailedSinceLastClear',
    'testNotCompletedThisOperationCycle',
    'warningIndicatorRequested',
)

RESPONSE_PENDING = 0x78
NEGATIVE_RESPONSE = 0x7F
SUPPRESS_POSITIVE_RESPONSE = 0x80
# P2 server timeout, and P2* for answers after "response pending"
P2_TIMEOUT = 1.0
P2_EXTENDED_TIMEOUT = 5.0
TESTER_PRESENT_INTERVAL = 2.0


class UdsTransport(Protocol):
    async def send(self, payload: bytes) -> None: ...

    async def receive(self, timeout: Optional[float] = None) -> bytes: ...


class UdsError(Exception):
    pass


class UdsNegativeResponse(UdsError):
    def __init__(self, service: int, code: int):
        self.service, self.code = service, code
        super().__init__(f'Negative response to {service_name(service)}: {nrc_name(code)}')


def service_name(sid: int) -> str:
    return SERVICES.get(sid, f'service 0x{sid:02X}')


def nrc_name(code: int) -> str:
    return NEGATIVE_RESPONSE_CODES.get(code, f'0x{code:02X}')


def describe_request(payload: bytes, description=None) -> str:
    if not payload:
        return ''
    text = service_name(payload[0]) + (f' {format_hex(payload[1:])}' if len(payload) > 1 else '')
    if payload[0] in (0x22, 0x2E) and len(payload) >= 3 and description is not None:
        name = description.name(int.from_bytes(payload[1:3], 'big'))
        if name:
            text += f' ({name})'
    return text


def describe_response(payload: bytes, description=None) -> str:
    """Describe a response; with a DiagnosticDescription, ReadDataByIdentifier data is decoded into fields."""
    if not payload:
        return 'empty response'
    if payload[0] == NEGATIVE_RESPONSE and len(payload) >= 3:
        return f'Negative response to {service_name(payload[1])}: {nrc_name(payload[2])}'
    if payload[0] == 0x62 and len(payload) >= 3 and description is not None:
        decoded = description.describe(int.from_bytes(payload[1:3], 'big'), payload[3:])
        if decoded is not None:
            return f'Positive response to ReadDataByIdentifier: {decoded}'
    if payload[0] >= 0x40:
        rest = payload[1:]
        return f'Positive response to {service_name(payload[0] - 0x40)}' + (f': {format_hex(rest)}' if rest else '')
    return f'Unexpected response: {format_hex(payload)}'


def describe_payload(payload: bytes, description=None) -> str:
    """Describe a UDS payload of unknown direction (passive decoding)."""
    if not payload:
        return ''
    if payload[0] == NEGATIVE_RESPONSE or (payload[0] - 0x40) in SERVICES:
        return describe_response(payload, description)
    if payload[0] in SERVICES:
        return describe_request(payload, description)
    return ''


def format_dtc(code: int) -> str:
    """24-bit DTC in the SAE J2012 notation, e.g. 0x012345 -> 'P0123-45' (failure type after the dash)."""
    letter = 'PCBU'[(code >> 22) & 0x3]
    return f'{letter}{(code >> 20) & 0x3}{(code >> 8) & 0xFFF:03X}-{code & 0xFF:02X}'


@dataclass(frozen=True, slots=True)
class Dtc:
    code: int
    status: int

    @property
    def text(self) -> str:
        return format_dtc(self.code)

    @property
    def status_flags(self) -> List[str]:
        return [name for bit, name in enumerate(DTC_STATUS_BITS) if self.status & (1 << bit)]


def format_did_value(did: int, data: bytes) -> str:
    """Readable form of a data identifier value: session names, ASCII strings, otherwise hex."""
    if did == 0xF186 and len(data) == 1:
        return SESSIONS.get(data[0], f'0x{data[0]:02X}')
    text = data.rstrip(b'\x00\xff ')
    if text and all(32 <= byte < 127 for byte in text):
        return text.decode('ascii')
    return format_hex(data)


def did_name(did: int) -> str:
    return DATA_IDENTIFIERS.get(did, f'DID 0x{did:04X}')


def load_key_function(path: str | Path) -> Callable[[int, bytes], bytes]:
    """Load a seed/key algorithm from a Python file defining `compute_key(level: int, seed: bytes) -> bytes`.

    Seed/key algorithms are manufacturer specific, so they are provided by the user. The file is executed.
    """
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f'can_explorer_key_{path.stem}', path)
    if spec is None or spec.loader is None:
        raise UdsError(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise UdsError(f'Error in key file {path.name}: {e}') from e
    function = getattr(module, 'compute_key', None)
    if not callable(function):
        raise UdsError(f'{path.name} does not define compute_key(level, seed)')
    return function


@dataclass(frozen=True, slots=True)
class UdsResult:
    request: bytes
    response: Optional[bytes]
    error: Optional[str] = None

    @property
    def positive(self) -> bool:
        return bool(self.response) and self.response[0] == self.request[0] + 0x40

    @property
    def description(self) -> str:
        if self.error:
            return self.error
        if self.response is None:
            return 'No response expected'
        return describe_response(self.response)


class UdsClient:
    """UDS client over a transport (ISO-TP on CAN, LIN transport layer)."""

    def __init__(self, transport: UdsTransport, timeout: float = P2_TIMEOUT):
        self.transport = transport
        self.timeout = timeout

    async def request(self, payload: bytes) -> UdsResult:
        """Send a request and wait for the final response, skipping 'response pending' answers.

        Never raises: transport errors are reported in the result.
        """
        payload = bytes(payload)
        suppressed = len(payload) >= 2 and payload[0] in (0x10, 0x11, 0x3E, 0x85, 0x28) and payload[1] & 0x80
        try:
            await self.transport.send(payload)
            if suppressed:
                return UdsResult(payload, None)
            response = await self.transport.receive(self.timeout)
            while len(response) >= 3 and response[0] == NEGATIVE_RESPONSE and response[2] == RESPONSE_PENDING:
                response = await self.transport.receive(P2_EXTENDED_TIMEOUT)
        except Exception as e:  # Transport errors (timeouts, bus errors)
            return UdsResult(payload, None, str(e))
        return UdsResult(payload, response)

    async def call(self, payload: bytes) -> bytes:
        """Like request(), but returns the positive response and raises UdsError otherwise."""
        result = await self.request(payload)
        if result.error:
            raise UdsError(result.error)
        response = result.response
        if response is None:
            return b''
        if response[0] == NEGATIVE_RESPONSE and len(response) >= 3:
            raise UdsNegativeResponse(response[1], response[2])
        if response[0] != payload[0] + 0x40:
            raise UdsError(f'Unexpected response {format_hex(response)} to {describe_request(payload)}')
        return response

    async def diagnostic_session_control(self, session: int) -> bytes:
        return (await self.call(bytes([0x10, session])))[2:]

    async def ecu_reset(self, reset_type: int) -> None:
        await self.call(bytes([0x11, reset_type]))

    async def tester_present(self, suppress_response: bool = True) -> None:
        await self.call(bytes([0x3E, SUPPRESS_POSITIVE_RESPONSE if suppress_response else 0x00]))

    async def read_data_by_identifier(self, did: int) -> bytes:
        response = await self.call(bytes([0x22, did >> 8, did & 0xFF]))
        if response[1:3] != bytes([did >> 8, did & 0xFF]):
            raise UdsError(f'Response for DID 0x{int.from_bytes(response[1:3], "big"):04X}, expected 0x{did:04X}')
        return response[3:]

    async def write_data_by_identifier(self, did: int, data: bytes) -> None:
        await self.call(bytes([0x2E, did >> 8, did & 0xFF]) + bytes(data))

    async def security_access(self, level: int, compute_key: Callable[[int, bytes], bytes]) -> bool:
        """Unlock a security level (odd request-seed level). Returns False if it was already unlocked."""
        if level % 2 == 0:
            raise UdsError('Security access levels for requesting a seed are odd')
        seed = (await self.call(bytes([0x27, level])))[2:]
        if not any(seed):
            return False  # A zero seed means the level is already unlocked
        key = bytes(compute_key(level, bytes(seed)))
        await self.call(bytes([0x27, level + 1]) + key)
        return True

    async def read_dtcs(self, status_mask: int = 0xFF) -> Tuple[int, List[Dtc]]:
        """ReadDTCInformation reportDTCByStatusMask. Returns (status availability mask, DTCs)."""
        response = await self.call(bytes([0x19, 0x02, status_mask]))
        if len(response) < 3:
            raise UdsError('ReadDTCInformation response too short')
        records = response[3:]
        dtcs = [Dtc(int.from_bytes(records[i : i + 3], 'big'), records[i + 3]) for i in range(0, len(records) - 3, 4)]
        return response[2], dtcs

    async def clear_diagnostic_information(self, group: int = 0xFFFFFF) -> None:
        await self.call(bytes([0x14]) + group.to_bytes(3, 'big'))

    async def routine_control(self, control_type: int, routine: int, data: bytes = b'') -> bytes:
        response = await self.call(bytes([0x31, control_type, routine >> 8, routine & 0xFF]) + bytes(data))
        return response[4:]


async def uds_request(transport: UdsTransport, payload: bytes) -> UdsResult:
    return await UdsClient(transport).request(payload)


class SessionKeepAlive:
    """Keeps a non-default diagnostic session alive by sending TesterPresent (suppressed response)."""

    def __init__(self, transport: UdsTransport, interval: float = TESTER_PRESENT_INTERVAL):
        self._transport = transport
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self.sent = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name='TesterPresent')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._transport.send(bytes([0x3E, SUPPRESS_POSITIVE_RESPONSE]))
                self.sent += 1
            except Exception:
                pass  # Keep trying: the bus may come back
            await asyncio.sleep(self._interval)


class UdsDecoder:
    """Passive decoder: ISO-TP on CAN with the reassembled payloads described as UDS services."""

    name = 'UDS'

    def __init__(self):
        from can_explorer.core.protocols.isotp import IsoTpDecoder

        self._isotp = IsoTpDecoder()
        # Optional DiagnosticDescription (CDD) naming data identifiers and decoding their data
        self.description = None

    def reset(self) -> None:
        self._isotp.reset()

    def describe(self, msg: can.Message) -> Optional[str]:
        description, payload = self._isotp.feed(msg)
        if payload is None:
            return description
        uds = describe_payload(payload, self.description)
        return f'{description} | UDS {uds}' if uds else description
