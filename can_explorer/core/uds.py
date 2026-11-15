"""Unified Diagnostic Services (ISO 14229-1): service client, response decoding and a passive decoder.

The client works over any transport offering `send(payload)` and `receive(timeout)` coroutines:
ISO-TP on CAN (IsoTpClient), the LIN transport layer (LinTpClient) or DoIP (DoipTransport).

The services beyond the basic ones live in their own modules, reached through `UdsClient` methods:
ReadDTCInformation sub-functions (core/uds_dtc.py), ReadDataByPeriodicIdentifier and
DynamicallyDefineDataIdentifier (core/uds_periodic.py), RequestFileTransfer (core/uds_file_transfer.py),
ResponseOnEvent (core/uds_events.py) and functional addressing (core/uds_functional.py). ISO 14229 is paywalled:
their layouts were checked against the free AUTOSAR Dcm specification (R23-11), the udsoncan library and Wireshark's
UDS dissector; parts none of them pins down are labelled and kept raw rather than guessed.
"""

import asyncio
import importlib.util
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple

import can

from can_explorer.core import uds_auth
from can_explorer.core.frames import format_hex

SERVICES = {
    0x10: 'DiagnosticSessionControl',
    0x11: 'ECUReset',
    0x14: 'ClearDiagnosticInformation',
    0x19: 'ReadDTCInformation',
    0x22: 'ReadDataByIdentifier',
    0x23: 'ReadMemoryByAddress',
    0x24: 'ReadScalingDataByIdentifier',
    0x27: 'SecurityAccess',
    0x28: 'CommunicationControl',
    0x29: 'Authentication',
    0x2A: 'ReadDataByPeriodicIdentifier',
    0x2C: 'DynamicallyDefineDataIdentifier',
    0x2E: 'WriteDataByIdentifier',
    0x2F: 'InputOutputControlByIdentifier',
    0x31: 'RoutineControl',
    0x34: 'RequestDownload',
    0x35: 'RequestUpload',
    0x36: 'TransferData',
    0x37: 'RequestTransferExit',
    0x38: 'RequestFileTransfer',
    0x3D: 'WriteMemoryByAddress',
    0x3E: 'TesterPresent',
    0x84: 'SecuredDataTransmission',
    0x85: 'ControlDTCSetting',
    0x86: 'ResponseOnEvent',
    0x87: 'LinkControl',
}

# Services whose sub-function byte carries the suppressPosRspMsgIndicationBit (0x80)
SUBFUNCTION_SERVICES = (0x10, 0x11, 0x27, 0x28, 0x2C, 0x31, 0x3E, 0x85, 0x86, 0x87)

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
    **uds_auth.NEGATIVE_RESPONSE_CODES,  # authenticationRequired (0x34) and the Authentication NRCs (0x50..0x5D)
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

# CommunicationControl (0x28): control types, and the communication types of the low two bits of the
# communicationType byte (its high nibble is the subnet: 0x0 all networks, 0xF the network of the request)
COMMUNICATION_CONTROL_TYPES = {
    0x00: 'enableRxAndTx',
    0x01: 'enableRxAndDisableTx',
    0x02: 'disableRxAndEnableTx',
    0x03: 'disableRxAndTx',
    0x04: 'enableRxAndDisableTxWithEnhancedAddressInformation',
    0x05: 'enableRxAndTxWithEnhancedAddressInformation',
}
COMMUNICATION_TYPES = {0x01: 'normal communication', 0x02: 'network management', 0x03: 'normal and network management'}
DTC_SETTING_TYPES = {0x01: 'on', 0x02: 'off'}  # ControlDTCSetting (0x85)

# LinkControl (0x87)
LINK_CONTROL_TYPES = {
    0x01: 'verifyModeTransitionWithFixedParameter',
    0x02: 'verifyModeTransitionWithSpecificParameter',
    0x03: 'transitionMode',
}
LINK_CONTROL_MODES = {  # linkControlModeIdentifier of verifyModeTransitionWithFixedParameter
    0x01: 'PC 9600 baud',
    0x02: 'PC 19200 baud',
    0x03: 'PC 38400 baud',
    0x04: 'PC 57600 baud',
    0x05: 'PC 115200 baud',
    0x10: 'CAN 125 kbit/s',
    0x11: 'CAN 250 kbit/s',
    0x12: 'CAN 500 kbit/s',
    0x13: 'CAN 1 Mbit/s',
    0x20: 'programming setup',
}

# ReadScalingDataByIdentifier (0x24): the high nibble of a scalingByte; its low nibble is a byte count
SCALING_DATA_TYPES = {
    0x0: 'unsigned numeric',
    0x1: 'signed numeric',
    0x2: 'bit mapped reported without mask',
    0x3: 'bit mapped reported with mask',
    0x4: 'binary coded decimal',
    0x5: 'state encoded variable',
    0x6: 'ASCII',
    0x7: 'signed floating point',
    0x8: 'packet',
    0x9: 'formula',
    0xA: 'unit/format',
    0xB: 'state and connection type',
}

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
# Added to the P2/P2* times an ECU reports in its DiagnosticSessionControl response, for the network and the tester
TIMING_MARGIN = 0.5
TESTER_PRESENT_INTERVAL = 2.0


def _is_pending(response: bytes) -> bool:
    return len(response) >= 3 and response[0] == NEGATIVE_RESPONSE and response[2] == RESPONSE_PENDING


def _is_response_to(request: bytes, response: bytes) -> bool:
    """Whether `response` answers `request`: a positive response (service + 0x40) or a negative response that
    echoes the request's service. Anything else is stale traffic on a shared response id."""
    if not response or not request:
        return False
    if response[0] == NEGATIVE_RESPONSE:
        return len(response) >= 2 and response[1] == request[0]
    if response[0] != request[0] + 0x40:
        return False
    if request[0] == 0x2A:
        return len(response) == 1  # 0x6A alone; 0x6A with a PDID and data is a periodic message
    # The echoed sub-function or identifier must match too: an event-triggered (ResponseOnEvent) or stale answer of
    # the same service for another DID is not this request's answer
    echo = _ECHOED_BYTES.get(request[0], 0)
    if echo and len(request) > echo:
        first = request[1] & 0x7F if request[0] in _ECHOED_SUBFUNCTION else request[1]
        return len(response) > echo and response[1] == first and response[2 : 1 + echo] == request[2 : 1 + echo]
    return True


# Bytes after the service ID that a positive response echoes (sub-function, DID or routine control type and id)
_ECHOED_BYTES = {
    0x10: 1, 0x11: 1, 0x19: 1, 0x22: 2, 0x24: 2, 0x27: 1, 0x28: 1, 0x2C: 1, 0x2E: 2, 0x2F: 2, 0x31: 3, 0x3E: 1,
    0x85: 1, 0x87: 1,
}  # fmt: skip
_ECHOED_SUBFUNCTION = (0x10, 0x11, 0x19, 0x27, 0x28, 0x2C, 0x31, 0x3E, 0x85, 0x87)  # Without the suppress bit


@dataclass(frozen=True, slots=True)
class SessionTiming:
    """The timing an ECU reports in its DiagnosticSessionControl response (sessionParameterRecord)."""

    p2: float  # P2server_max, seconds (sent in 1 ms steps)
    p2_extended: float  # P2*server_max, seconds (sent in 10 ms steps)

    @classmethod
    def parse(cls, record: bytes) -> Optional['SessionTiming']:
        if len(record) < 4:
            return None
        return cls(int.from_bytes(record[0:2], 'big') / 1000, int.from_bytes(record[2:4], 'big') / 100)

    def describe(self) -> str:
        return f'P2 {self.p2 * 1000:g} ms, P2* {self.p2_extended * 1000:g} ms'


@dataclass(frozen=True, slots=True)
class ScalingByte:
    """One scalingByte of a ReadScalingDataByIdentifier response, with its scalingByteExtension (if any)."""

    data_type: int
    length: int
    extension: bytes = b''

    @property
    def type_name(self) -> str:
        return SCALING_DATA_TYPES.get(self.data_type, f'type 0x{self.data_type:X}')

    def describe(self) -> str:
        text = f'{self.type_name}, {self.length} byte{"s" if self.length != 1 else ""}'
        return text + (f' (extension {format_hex(self.extension)})' if self.extension else '')


@dataclass(frozen=True, slots=True)
class ScalingData:
    did: int
    scaling: Tuple[ScalingByte, ...]
    undecoded: bytes = b''  # Bytes after a scalingByte whose extension layout is not decoded (bit-mapped types)

    def describe(self) -> str:
        parts = [item.describe() for item in self.scaling]
        if self.undecoded:
            parts.append(f'not decoded: {format_hex(self.undecoded)}')
        return f'{did_name(self.did)}: ' + '; '.join(parts)


def parse_scaling_data(response: bytes) -> ScalingData:
    """A ReadScalingDataByIdentifier positive response (0x64, DID, scalingByte [scalingByteExtension]...).

    A scalingByte is a data type (high nibble) and a byte count (low nibble). For formula and unit/format types the
    count is the length of the extension that follows. For the bit-mapped types the extension's layout is not pinned
    down by the free references, so the remaining bytes are kept undecoded rather than guessed."""
    if len(response) < 3 or response[0] != 0x64:
        raise UdsError('Not a ReadScalingDataByIdentifier response')
    did = int.from_bytes(response[1:3], 'big')
    scaling: List[ScalingByte] = []
    offset = 3
    while offset < len(response):
        byte = response[offset]
        data_type, length = byte >> 4, byte & 0x0F
        offset += 1
        if data_type in (0x2, 0x3):
            scaling.append(ScalingByte(data_type, length))
            return ScalingData(did, tuple(scaling), bytes(response[offset:]))
        extension = b''
        if data_type in (0x9, 0xA):
            extension = bytes(response[offset : offset + length])
            offset += length
        scaling.append(ScalingByte(data_type, length, extension))
    return ScalingData(did, tuple(scaling))


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


def subfunction_text(sid: int, value: int) -> Optional[str]:
    """The name of a request's sub-function (or mode) byte for the services that name them here."""
    sub = value & 0x7F
    if sid == 0x10:
        return SESSIONS.get(sub)
    if sid == 0x11:
        return RESET_TYPES.get(sub)
    if sid == 0x19:
        from can_explorer.core import uds_dtc

        return uds_dtc.SUBFUNCTIONS.get(sub)
    if sid == 0x28:
        return COMMUNICATION_CONTROL_TYPES.get(sub)
    if sid == 0x2A:
        from can_explorer.core import uds_periodic

        return uds_periodic.TRANSMISSION_MODES.get(value)
    if sid == 0x2C:
        from can_explorer.core import uds_periodic

        return uds_periodic.DDDI_TYPES.get(sub)
    if sid == 0x31:
        return ROUTINE_CONTROL_TYPES.get(sub)
    if sid == 0x38:
        from can_explorer.core import uds_file_transfer

        return uds_file_transfer.MODES.get(value)
    if sid == 0x85:
        return DTC_SETTING_TYPES.get(sub)
    if sid == 0x86:
        from can_explorer.core import uds_events

        return uds_events.EVENT_TYPES.get(value & 0x3F)
    if sid == 0x87:
        return LINK_CONTROL_TYPES.get(sub)
    return None


_NAMED_SUBFUNCTIONS = (0x19, 0x28, 0x2A, 0x2C, 0x38, 0x85, 0x86, 0x87)


def describe_request(payload: bytes, description=None) -> str:
    if not payload:
        return ''
    if payload[0] == uds_auth.SID and len(payload) >= 2:
        return uds_auth.describe(payload)
    text = service_name(payload[0]) + (f' {format_hex(payload[1:])}' if len(payload) > 1 else '')
    if payload[0] in _NAMED_SUBFUNCTIONS and len(payload) >= 2:
        name = subfunction_text(payload[0], payload[1])
        if name:
            text += f' ({name})'
    if payload[0] == 0x84:
        text += ' (secured: content not decoded)'  # SecuredDataTransmission: OEM-specific security layout
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
    if payload[0] == uds_auth.SID + 0x40 and len(payload) >= 3:
        return f'Positive response to {uds_auth.describe(payload)}'
    if payload[0] == 0x62 and len(payload) >= 3 and description is not None:
        decoded = description.describe(int.from_bytes(payload[1:3], 'big'), payload[3:])
        if decoded is not None:
            return f'Positive response to ReadDataByIdentifier: {decoded}'
    if payload[0] == 0x50 and len(payload) >= 6:
        timing = SessionTiming.parse(payload[2:6])
        name = SESSIONS.get(payload[1], f'session 0x{payload[1]:02X}')
        return f'Positive response to DiagnosticSessionControl: {name}, {timing.describe()}'
    if payload[0] >= 0x40:
        rest = payload[1:]
        text = f'Positive response to {service_name(payload[0] - 0x40)}'
        if payload[0] - 0x40 in _NAMED_SUBFUNCTIONS and rest:
            name = subfunction_text(payload[0] - 0x40, rest[0])
            if name:
                text += f' ({name})'
        return text + (f': {format_hex(rest)}' if rest else '')
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


KEYSTORE_PREFIX = 'keystore:'  # A key source naming a seed/key entry of the key store instead of a file


def load_key_source(source: str, store=None) -> Callable[[int, bytes], bytes]:
    """The seed/key function of a key source: a Python file, or 'keystore:<entry>' for a seed/key algorithm kept in
    the key store (`store`, by default the application's; it must be unlocked)."""
    from can_explorer.core import security_profiles

    if source.startswith(security_profiles.PREFIX):  # A security profile computes the key
        name = source[len(security_profiles.PREFIX) :]
        try:
            profile = security_profiles.registry().get(name)
        except security_profiles.SecurityProfileError as e:
            raise UdsError(str(e)) from e
        if not callable(getattr(profile, 'compute_key', None)):
            raise UdsError(f'The security profile {name!r} has no seed/key algorithm')
        from can_explorer.core import audit

        audit.record('seed/key algorithm used', profile=name)

        def compute_key(level: int, seed: bytes) -> bytes:
            try:
                key = profile.compute_key(level, seed)
            except security_profiles.SecurityProfileError as e:
                raise UdsError(str(e)) from e
            if key is None:
                raise UdsError(f'The security profile {name!r} has no key for level {level}')
            return bytes(key)

        return compute_key
    if not source.startswith(KEYSTORE_PREFIX):
        return load_key_function(source)
    from can_explorer.core import keystore

    name = source[len(KEYSTORE_PREFIX) :]
    store = store if store is not None else keystore.default_store()
    try:
        script = store.secrets(name)['script']
    except (keystore.KeyStoreError, KeyError) as e:
        raise UdsError(f'Seed/key algorithm {name!r}: {e}') from e
    from can_explorer.core import audit

    audit.record('seed/key algorithm used', entry=name)
    module = types.ModuleType(f'can_explorer_key_{name}')
    try:
        exec(compile(script, f'<key store: {name}>', 'exec'), module.__dict__)  # noqa: S102  (the user's own algorithm)
    except Exception as e:
        raise UdsError(f'Error in seed/key algorithm {name!r}: {e}') from e
    function = getattr(module, 'compute_key', None)
    if not callable(function):
        raise UdsError(f'Seed/key algorithm {name!r} does not define compute_key(level, seed)')
    return function


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
    """UDS client over a transport (ISO-TP on CAN, LIN transport layer, DoIP).

    `timeout` is the P2 client time (the wait for a response) and `extended_timeout` the P2* time (the wait after
    "response pending"). With `adopt_timing`, a DiagnosticSessionControl response's reported P2/P2* times (plus
    TIMING_MARGIN) lengthen them for the new session; they are never shortened below the times given here, so a slow
    transport (DoIP over WiFi, an ELM327) is not cut short by an optimistic ECU.

    Responses that do not answer the current request — stale answers, ResponseOnEvent messages, periodic data sent
    on the diagnostic channel — are passed to the `add_unsolicited_listener` callbacks instead of being lost.
    """

    def __init__(
        self,
        transport: UdsTransport,
        timeout: float = P2_TIMEOUT,
        extended_timeout: float = P2_EXTENDED_TIMEOUT,
        adopt_timing: bool = True,
    ):
        self.transport = transport
        self.timeout = timeout
        self.extended_timeout = extended_timeout
        self.base_timeout, self.base_extended_timeout = timeout, extended_timeout
        self.adopt_timing = adopt_timing
        self.server_timing: Optional[SessionTiming] = None  # From the last DiagnosticSessionControl response
        self._unsolicited: List[Callable[[bytes], None]] = []

    # Unsolicited responses

    def add_unsolicited_listener(self, listener: Callable[[bytes], None]) -> None:
        self._unsolicited.append(listener)

    def remove_unsolicited_listener(self, listener: Callable[[bytes], None]) -> None:
        if listener in self._unsolicited:
            self._unsolicited.remove(listener)

    def _dispatch_unsolicited(self, payload: bytes) -> None:
        for listener in list(self._unsolicited):
            listener(payload)

    async def listen(self, duration: float) -> List[bytes]:
        """Receive for `duration` seconds without sending, passing every message to the unsolicited listeners;
        returns the messages (e.g. ResponseOnEvent or periodic data between requests)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        received: List[bytes] = []
        while (remaining := deadline - loop.time()) > 0:
            try:
                payload = await self.transport.receive(remaining)
            except Exception:
                break  # Timeout (or the transport ended): nothing more arrived
            received.append(payload)
            self._dispatch_unsolicited(payload)
        return received

    # Timing

    def apply_session_timing(self, timing: Optional[SessionTiming]) -> None:
        """Use an ECU's reported P2/P2* (plus TIMING_MARGIN), never below the base times."""
        self.server_timing = timing
        if timing is None or not self.adopt_timing:
            return
        self.timeout = max(self.base_timeout, timing.p2 + TIMING_MARGIN)
        self.extended_timeout = max(self.base_extended_timeout, timing.p2_extended + TIMING_MARGIN)

    # Requests

    async def request(self, payload: bytes) -> UdsResult:
        """Send a request and wait for the final response, skipping 'response pending' answers.

        Never raises: transport errors are reported in the result.
        """
        payload = bytes(payload)
        suppressed = (
            len(payload) >= 2 and payload[0] in SUBFUNCTION_SERVICES and payload[1] & SUPPRESS_POSITIVE_RESPONSE
        )
        try:
            await self.transport.send(payload)
            if suppressed:
                return UdsResult(payload, None)
            response = await self.transport.receive(self.timeout)
            while _is_pending(response) or not _is_response_to(payload, response):
                # Skip "response pending". A response that does not match this request — one left on a shared
                # response id by an earlier or cancelled transaction, or an event-triggered message — goes to the
                # unsolicited listeners.
                pending = _is_pending(response)
                if not pending:
                    self._dispatch_unsolicited(response)
                response = await self.transport.receive(self.extended_timeout if pending else self.timeout)
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
        """Change the session; returns the sessionParameterRecord, whose P2/P2* times are adopted (see the class)."""
        record = (await self.call(bytes([0x10, session])))[2:]
        if record:  # Not with a suppressed response
            self.apply_session_timing(SessionTiming.parse(record))
        return record

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

    async def read_scaling_data_by_identifier(self, did: int) -> ScalingData:
        """ReadScalingDataByIdentifier (0x24): the data types and lengths of a DID's data record."""
        response = await self.call(bytes([0x24, did >> 8, did & 0xFF]))
        if response[1:3] != bytes([did >> 8, did & 0xFF]):
            raise UdsError(f'Scaling data for DID 0x{int.from_bytes(response[1:3], "big"):04X}, expected 0x{did:04X}')
        return parse_scaling_data(response)

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

    async def communication_control(
        self, control_type: int, communication_type: int = 0x01, node: Optional[int] = None, suppress: bool = False
    ) -> None:
        """CommunicationControl (0x28), e.g. (0x03, 0x01): disable sending and receiving normal messages.
        `node` is the nodeIdentificationNumber of the enhanced-address control types 0x04/0x05."""
        payload = bytes([0x28, control_type | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0), communication_type])
        if control_type in (0x04, 0x05):
            if node is None:
                raise UdsError('Control types 0x04 and 0x05 need a node identification number')
            payload += node.to_bytes(2, 'big')
        await self.call(payload)

    async def control_dtc_setting(self, on: bool, option_record: bytes = b'', suppress: bool = False) -> None:
        """ControlDTCSetting (0x85): resume (on) or stop (off) updating the DTC status bits."""
        setting = (0x01 if on else 0x02) | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0)
        await self.call(bytes([0x85, setting]) + bytes(option_record))

    async def link_control(
        self, control_type: int, mode: Optional[int] = None, baudrate: Optional[int] = None, suppress: bool = False
    ) -> None:
        """LinkControl (0x87): verify a transition with a fixed mode (0x01, `mode` from LINK_CONTROL_MODES) or a
        specific baud rate (0x02, `baudrate` in bit/s), then perform it (0x03, usually suppressed and sent
        functionally). Changing the connection's own bitrate afterwards is up to the caller (reconnect at the new
        rate)."""
        payload = bytes([0x87, control_type | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0)])
        if control_type == 0x01:
            if mode is None:
                raise UdsError('verifyModeTransitionWithFixedParameter needs a link control mode')
            payload += bytes([mode])
        elif control_type == 0x02:
            if baudrate is None or not 0 < baudrate < 1 << 24:
                raise UdsError('verifyModeTransitionWithSpecificParameter needs a baud rate below 16777216 bit/s')
            payload += baudrate.to_bytes(3, 'big')
        await self.call(payload)

    async def read_dtcs(self, status_mask: int = 0xFF) -> Tuple[int, List[Dtc]]:
        """ReadDTCInformation reportDTCByStatusMask. Returns (status availability mask, DTCs)."""
        response = await self.call(bytes([0x19, 0x02, status_mask]))
        if len(response) < 3:
            raise UdsError('ReadDTCInformation response too short')
        records = response[3:]
        dtcs = [Dtc(int.from_bytes(records[i : i + 3], 'big'), records[i + 3]) for i in range(0, len(records) - 3, 4)]
        return response[2], dtcs

    async def read_dtc_information(self, subfunction: int, *args, **kwargs):
        """Any supported ReadDTCInformation sub-function: see `can_explorer.core.uds_dtc.read`."""
        from can_explorer.core import uds_dtc

        return await uds_dtc.read(self, subfunction, *args, **kwargs)

    async def clear_diagnostic_information(self, group: int = 0xFFFFFF) -> None:
        await self.call(bytes([0x14]) + group.to_bytes(3, 'big'))

    async def routine_control(self, control_type: int, routine: int, data: bytes = b'') -> bytes:
        response = await self.call(bytes([0x31, control_type, routine >> 8, routine & 0xFF]) + bytes(data))
        return response[4:]

    async def dynamically_define_by_identifier(self, dddid: int, sources) -> None:
        """DynamicallyDefineDataIdentifier defineByIdentifier: see `can_explorer.core.uds_periodic`."""
        from can_explorer.core import uds_periodic

        await uds_periodic.define_by_identifier(self, dddid, sources)

    async def dynamically_define_by_memory_address(
        self, dddid: int, areas, address_bytes: int = 4, size_bytes: int = 1
    ) -> None:
        """DynamicallyDefineDataIdentifier defineByMemoryAddress: see `can_explorer.core.uds_periodic`."""
        from can_explorer.core import uds_periodic

        await uds_periodic.define_by_memory_address(self, dddid, areas, address_bytes, size_bytes)

    async def clear_dynamically_defined(self, dddid: Optional[int] = None) -> None:
        from can_explorer.core import uds_periodic

        await uds_periodic.clear_dynamically_defined(self, dddid)

    async def read_data_by_periodic_identifier(self, transmission_mode: int, periodic_ids=()) -> None:
        """ReadDataByPeriodicIdentifier (0x2A): see `can_explorer.core.uds_periodic.read_periodic`."""
        from can_explorer.core import uds_periodic

        await uds_periodic.read_periodic(self, transmission_mode, periodic_ids)

    async def write_file(self, path: str, data: bytes, replace: bool = False, **kwargs) -> None:
        """RequestFileTransfer addFile/replaceFile: see `can_explorer.core.uds_file_transfer.write_file`."""
        from can_explorer.core import uds_file_transfer

        await uds_file_transfer.write_file(self, path, data, replace, **kwargs)

    async def read_file(self, path: str, **kwargs) -> bytes:
        from can_explorer.core import uds_file_transfer

        return await uds_file_transfer.read_file(self, path, **kwargs)

    async def read_directory(self, path: str) -> bytes:
        from can_explorer.core import uds_file_transfer

        return await uds_file_transfer.read_directory(self, path)

    async def delete_file(self, path: str) -> None:
        from can_explorer.core import uds_file_transfer

        await uds_file_transfer.delete_file(self, path)

    async def response_on_event(self, *args, **kwargs):
        """ResponseOnEvent (0x86): see `can_explorer.core.uds_events.response_on_event`."""
        from can_explorer.core import uds_events

        return await uds_events.response_on_event(self, *args, **kwargs)

    async def authenticate(
        self, certificate, private_key, scheme: str, bidirectional: bool = False, trusted=(), crls=()
    ):
        """Authentication (0x29) by certificate exchange: see `can_explorer.core.uds_auth.authenticate`."""
        return await uds_auth.authenticate(self, certificate, private_key, scheme, bidirectional, trusted, crls=crls)

    async def authenticate_challenge_response(self, prove, bidirectional: bool = False, verify_server=None, **kwargs):
        """Authentication (0x29) by challenge-response: see `uds_auth.authenticate_challenge_response`."""
        return await uds_auth.authenticate_challenge_response(self, prove, bidirectional, verify_server, **kwargs)

    async def deauthenticate(self) -> str:
        return await uds_auth.deauthenticate(self)

    async def seed_analysis(self, level: int, count: int = 32, between: str = 'session', session: int = 0x03):
        """Request seeds and report weaknesses: see `can_explorer.core.uds_security_test.seed_analysis`."""
        from can_explorer.core import uds_security_test

        return await uds_security_test.seed_analysis(self, level, count, between, session)

    async def lockout_test(self, level: int, max_attempts: int = 10, wrong_key=None, test_reset: bool = False):
        """Send wrong keys and report the lockout: see `can_explorer.core.uds_security_test.lockout_test`."""
        from can_explorer.core import uds_security_test

        return await uds_security_test.lockout_test(self, level, max_attempts, wrong_key, test_reset)


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
