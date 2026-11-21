"""Passive security analysis of CAN gateway behaviour from captured UDS traffic.

A security gateway is a central ECU that authenticates or filters diagnostic requests before forwarding them to the
internal bus. This module analyses captured traffic to characterise the gateway's behaviour:

- **Authentication pattern detection**: which services are locked behind 0x27 or 0x29, at what level, in what
  session type, and what services return securityAccessDenied (NRC 0x33) or authenticationRequired (NRC 0x34).
- **Gateway bypass detection**: diagnostic request IDs that the gateway does not filter, session fallback
  behaviour, timing windows, and functional addressing bypass.
- **Cross-ECU seed/key correlation**: whether different ECUs behind the same gateway share the same algorithm or
  master key.
- **Gateway mapping**: automated topology inference from captured UDS traffic.
- **Access matrix**: per-ECU, per-service, per-session security classification.
- **Unlock sequence extraction**: the exact authentication recipe an observed tester used.
- **NRC fingerprinting**: ECU identification from NRC patterns and response timing.
- **Auth timing analysis**: timing of each step of authentication flows.
- **DID/routine security classification**: security posture of individual data identifiers and routines.
- **Probing planner**: suggest which services to try next to maximize attack-surface coverage.

All analysis is passive: it reads captured frames only and never sends anything on the bus.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import can


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NRC_SECURITY_ACCESS_DENIED = 0x33
NRC_AUTHENTICATION_REQUIRED = 0x34
NRC_CONDITIONS_NOT_CORRECT = 0x22
NRC_SERVICE_NOT_SUPPORTED_IN_SESSION = 0x7F
NRC_GENERAL_REJECT = 0x10
NRC_SUB_FUNCTION_NOT_SUPPORTED = 0x12
NRC_REQUEST_OUT_OF_RANGE = 0x31
NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_INCORRECT_MESSAGE_LENGTH = 0x13
NRC_BUSY_REPEAT_REQUEST = 0x21
NRC_UPLOAD_DOWNLOAD_NOT_ACCEPTED = 0x70
NRC_EXCEEDED_NUMBER_OF_ATTEMPTS = 0x36
NRC_REQUIRED_TIME_DELAY = 0x37

# UDS service IDs
SID_DIAGNOSTIC_SESSION = 0x10
SID_ECU_RESET = 0x11
SID_SECURITY_ACCESS = 0x27
SID_AUTHENTICATION = 0x29
SID_READ_DID = 0x22
SID_READ_MEMORY = 0x23
SID_WRITE_DID = 0x2E
SID_IO_CONTROL = 0x2F
SID_ROUTINE_CONTROL = 0x31
SID_REQUEST_DOWNLOAD = 0x34
SID_REQUEST_UPLOAD = 0x35
SID_TRANSFER_DATA = 0x36
SID_REQUEST_TRANSFER_EXIT = 0x37
SID_TESTER_PRESENT = 0x3E

# Standard diagnostic sessions
SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02
SESSION_EXTENDED = 0x03

# Service names for human-readable output
_SID_NAMES: Dict[int, str] = {
    0x10: 'DiagnosticSessionControl',
    0x11: 'ECUReset',
    0x14: 'ClearDTC',
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
    0x3D: 'WriteMemoryByAddress',
    0x3E: 'TesterPresent',
    0x85: 'ControlDTCSetting',
    0x86: 'ResponseOnEvent',
    0x87: 'LinkControl',
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class GatewayAuthPattern:
    """An observed authentication pattern of a gateway."""

    request_id: int
    response_id: int
    auth_type: str  # 'none', 'seed_key_0x27', 'challenge_response_0x29', 'certificate_0x29', 'unknown'
    session_type: int  # The diagnostic session (0x01, 0x02, 0x03, etc.)
    security_level: Optional[int]  # 0x27 level, if applicable
    locked_services: List[int]  # Services that returned securityAccessDenied (NRC 0x33)
    unlocked_services: List[int]  # Services that worked after authentication
    details: str


@dataclass
class GatewayBypassFinding:
    """A potential bypass path found in gateway filtering."""

    finding_type: str  # 'unfiltered_id', 'session_fallback', 'timing_window', 'functional_addressing'
    description: str
    evidence: str
    severity: str  # 'info', 'warning', 'finding'


@dataclass
class CrossEcuCorrelation:
    """Correlation between seed/key algorithms of different ECUs behind the same gateway."""

    ecu_pairs: List[Tuple[int, int]]  # Request ID pairs
    finding: str  # 'same_algorithm', 'shared_key', 'independent'
    details: str


# --- Item 1: Gateway mapping ---


@dataclass
class EcuNode:
    """One ECU discovered in the captured traffic."""

    request_id: int
    response_id: int
    supported_sessions: List[int]
    auth_type: str  # from GatewayAuthPattern
    services_seen: List[int]  # SIDs that responded (positive or negative)
    mean_response_ms: float  # Average response time in milliseconds


@dataclass
class GatewayMap:
    """Topology of ECUs inferred from captured UDS traffic."""

    ecus: List[EcuNode]
    segments: Dict[str, List[int]]  # Timing-cluster label -> list of request IDs
    diagnostic_ids: List[int]  # All request IDs with diagnostic traffic
    functional_ids_seen: List[int]  # Functional addressing IDs used (0x7DF, 0x18DB33F1)


# --- Item 2: Access matrix ---


@dataclass
class AccessRow:
    """Security posture of one service on one ECU in one session."""

    ecu_id: int
    service: int
    service_name: str
    session: int
    auth_required: str  # 'none', '0x27', '0x29', 'unknown'
    security_level: Optional[int]
    status: str  # 'allowed', 'denied', 'conditional', 'not_seen'


@dataclass
class AccessMatrix:
    """Per-ECU, per-service, per-session security classification."""

    rows: List[AccessRow]
    ecus: List[int]
    sessions: List[int]
    services: List[int]


# --- Item 3: Unlock sequence ---


@dataclass
class UnlockStep:
    """One step of an authentication sequence."""

    action: str  # 'session_change', 'security_access_seed', 'security_access_key', 'authentication', 'tester_present'
    sid: int
    sub_function: Optional[int]
    request_data: str  # Hex
    response_data: str  # Hex
    timestamp: float
    timing_constraint_ms: Optional[float]  # Max delay before next step (None if last)


@dataclass
class UnlockSequence:
    """The exact authentication recipe observed for one ECU."""

    ecu_id: int
    response_id: int
    steps: List[UnlockStep]
    total_time_ms: float
    auth_type: str


# --- Item 4: Consolidated report ---


@dataclass
class GatewayReport:
    """Combined output of all gateway analyses."""

    gateway_map: GatewayMap
    access_matrix: AccessMatrix
    auth_patterns: List[GatewayAuthPattern]
    bypass_findings: List[GatewayBypassFinding]
    correlations: List[CrossEcuCorrelation]
    unlock_sequences: Dict[int, UnlockSequence]  # ecu_id -> sequence
    severity_summary: Dict[str, int]  # 'finding' / 'warning' / 'info' -> count
    overall_severity: str = 'info'  # 'info', 'warning', 'finding', 'critical'
    summary: List[str] = field(default_factory=list)  # Prioritised summary lines


# --- Item 5: NRC fingerprinting ---


@dataclass
class EcuFingerprint:
    """NRC pattern and timing fingerprint of an ECU."""

    request_id: int
    response_id: int
    nrc_set: List[int]  # NRCs observed
    nrc_counts: Dict[int, int]  # NRC -> count
    supported_sessions: List[int]
    timing_profile: Dict[str, float]  # 'mean_ms', 'std_ms', 'min_ms', 'max_ms'
    vendor_hints: List[str]


# --- Item 6: Probing planner ---


@dataclass
class ProbeStep:
    """A suggested next probing action."""

    ecu_id: int
    service: int
    service_name: str
    session: int
    auth_level: Optional[int]
    rationale: str
    priority: str  # 'high', 'medium', 'low'


# --- Item 7: DID/routine security classification ---


@dataclass
class DidSecurityInfo:
    """Security posture of one DID or routine."""

    ecu_id: int
    identifier: int  # DID or routine ID
    service: int  # 0x22, 0x2E, 0x31
    service_name: str
    readable: bool
    writable: bool
    auth_required: str  # 'none', '0x27', '0x29', 'unknown'
    nrc: Optional[int]  # Last NRC received, if any


# --- Item 8: Auth timing analysis ---


@dataclass
class AuthTimingResult:
    """Timing analysis of one authentication flow."""

    ecu_id: int
    auth_type: str
    request_to_seed_ms: Optional[float]
    seed_to_key_ms: Optional[float]
    key_to_response_ms: Optional[float]
    total_ms: float
    jitter_ms: Optional[float]
    findings: List[str] = field(default_factory=list)


# --- Item 11: Bus segmentation detection ---


@dataclass
class BusSegment:
    """A cluster of CAN IDs inferred to share a physical bus segment."""

    name: str
    ids: List[int]
    id_range: Tuple[int, int]  # (min, max) of the IDs in this segment
    mean_rate_hz: float  # Average message rate across IDs
    periodic_ratio: float  # Fraction of IDs that are periodic (< 20 % jitter)
    event_driven_ids: List[int]  # IDs classified as event-driven


# --- Item 12: Response-after-reset analysis ---


@dataclass
class ResetBehaviourFinding:
    """A service that succeeded shortly after an ECU reset."""

    ecu_id: int
    service: int
    service_name: str
    session: int
    success_time_after_reset_ms: float
    finding: str
    severity: str  # 'info', 'warning', 'finding'


# ---------------------------------------------------------------------------
# OEM-specific NRC decoder (item 13)
# ---------------------------------------------------------------------------

# Vendor-specific NRC codes in the 0x70-0x7E range.  Only codes from public sources
# (AUTOSAR SWS documents, ISO 14229 Annex A, community-documented behaviour) are
# included.  Where no public specification exists the NRC is omitted rather than
# guessed -- see CLAUDE.md honesty policy.
OEM_NRCS: Dict[int, str] = {
    0x70: 'uploadDownloadNotAccepted',
    0x71: 'transferDataSuspended',
    0x72: 'generalProgrammingFailure',
    0x73: 'wrongBlockSequenceCounter',
    0x78: 'requestCorrectlyReceivedResponsePending',
    0x7E: 'subFunctionNotSupportedInActiveSession',
    0x7F: 'serviceNotSupportedInActiveSession',
}

# Standard NRC names (ISO 14229, public subset)
_STANDARD_NRCS: Dict[int, str] = {
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
    0x34: 'authenticationRequired',
    0x35: 'invalidKey',
    0x36: 'exceededNumberOfAttempts',
    0x37: 'requiredTimeDelayNotExpired',
    0x70: 'uploadDownloadNotAccepted',
    0x71: 'transferDataSuspended',
    0x72: 'generalProgrammingFailure',
    0x73: 'wrongBlockSequenceCounter',
    0x78: 'requestCorrectlyReceivedResponsePending',
    0x7E: 'subFunctionNotSupportedInActiveSession',
    0x7F: 'serviceNotSupportedInActiveSession',
}


def decode_nrc(nrc: int) -> str:
    """Return a human-readable name for a UDS NRC code.

    Covers standard NRCs and the OEM range (0x70-0x7E) from public sources.
    Returns 'vendor-specific 0xNN' for unrecognised codes in the OEM range.
    """
    name = _STANDARD_NRCS.get(nrc) or OEM_NRCS.get(nrc)
    if name is not None:
        return name
    if 0x70 <= nrc <= 0x7E:
        return f'vendor-specific 0x{nrc:02X}'
    return f'unknown NRC 0x{nrc:02X}'


# ---------------------------------------------------------------------------
# UDS message parsing
# ---------------------------------------------------------------------------


def _is_negative_response(data: bytes) -> Optional[Tuple[int, int]]:
    """If the data is a UDS negative response (7F SID NRC), return (rejected_sid, nrc)."""
    if len(data) >= 3 and data[0] == 0x7F:
        return data[1], data[2]
    return None


def _is_positive_response(data: bytes) -> Optional[int]:
    """If the data is a positive UDS response (SID + 0x40), return the request SID.

    Valid positive response SIDs are 0x41..0x7E (request SIDs 0x01..0x3E). Values at 0x7F and above are
    either negative responses (0x7F) or not valid UDS response SIDs.
    """
    if len(data) >= 1 and 0x41 <= data[0] <= 0x7E:
        return data[0] - 0x40
    return None


def _extract_uds_pairs(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> Dict[Tuple[int, int], List[Tuple[bytes, bytes]]]:
    """Group messages into request/response pairs by (request_id, response_id).

    *response_offset* is added to the request ID to compute the expected response ID. The standard UDS
    convention is +8 for standard 11-bit IDs; some OEMs use other offsets (e.g. +0x600, +0x10).

    For functional addressing IDs (0x7DF, 0x18DB33F1), responses come on the ECU's physical response ID,
    so the pairing accepts any response whose first byte matches the expected positive or negative response
    SID within the time window.
    """
    pairs: Dict[Tuple[int, int], List[Tuple[bytes, bytes]]] = defaultdict(list)
    functional_ids = {0x7DF, 0x18DB33F1}

    # Build a timeline of messages
    by_time = sorted(messages, key=lambda m: m.timestamp)

    # Try to pair each message with its response within a small time window
    used = set()
    for i, msg in enumerate(by_time):
        if i in used or msg.is_error_frame or msg.is_remote_frame:
            continue
        data = bytes(msg.data)
        if len(data) < 1:
            continue
        sid = data[0]
        if sid >= 0x40 or sid == 0x7F:
            continue  # This is a response, not a request
        is_functional = msg.arbitration_id in functional_ids
        expected_response_id = msg.arbitration_id + response_offset
        for j in range(i + 1, min(i + 200, len(by_time))):
            if j in used:
                continue
            resp = by_time[j]
            if resp.timestamp - msg.timestamp > 5.0:
                break
            if resp.is_error_frame:
                continue
            resp_data = bytes(resp.data)
            if len(resp_data) < 1:
                continue
            if is_functional:
                # Functional: accept any response with the right SID match
                is_pos = resp_data[0] == sid + 0x40
                is_neg = len(resp_data) >= 3 and resp_data[0] == 0x7F and resp_data[1] == sid
                if is_pos or is_neg:
                    pairs[(msg.arbitration_id, resp.arbitration_id)].append((data, resp_data))
                    used.add(j)
                    break
            elif resp.arbitration_id == expected_response_id:
                pairs[(msg.arbitration_id, resp.arbitration_id)].append((data, resp_data))
                used.add(j)
                break
    return dict(pairs)


# ---------------------------------------------------------------------------
# Gateway authentication pattern detection
# ---------------------------------------------------------------------------


def analyse_gateway_auth(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> List[GatewayAuthPattern]:
    """Analyse captured UDS traffic to detect authentication patterns.

    Passively observes UDS request/response pairs and detects:
    - Which services return NRC 0x33 (securityAccessDenied) or 0x34 (authenticationRequired)
    - Which 0x27 levels and 0x29 flows are used before those services succeed
    - The session type used
    - Whether the gateway uses a single security level or different levels per service

    *response_offset*: added to the request ID to find the expected response ID (default 8).
    """
    uds_pairs = _extract_uds_pairs(messages, response_offset=response_offset)
    patterns: List[GatewayAuthPattern] = []

    for (req_id, resp_id), exchanges in uds_pairs.items():
        locked_services: Set[int] = set()
        unlocked_services: Set[int] = set()
        security_levels: Set[int] = set()
        session_types: Set[int] = set()
        vendor_nrcs: Set[int] = set()
        has_0x27 = False
        has_0x29 = False
        has_0x29_cert = False

        for request, response in exchanges:
            if len(request) < 1:
                continue

            sid = request[0]

            # Track session changes
            if sid == SID_DIAGNOSTIC_SESSION and len(request) >= 2:
                neg = _is_negative_response(response)
                if neg is None:
                    session_types.add(request[1])

            # Track security access
            if sid == SID_SECURITY_ACCESS and len(request) >= 2:
                has_0x27 = True
                sub = request[1]
                if sub % 2 == 1:  # Request seed (odd sub-function)
                    security_levels.add((sub + 1) // 2)

            # Track authentication
            if sid == SID_AUTHENTICATION:
                has_0x29 = True
                if len(request) >= 2 and request[1] in (0x01, 0x02):  # deAuthenticate, verifyCertificateUnidirectional
                    has_0x29_cert = True

            # Track negative responses (including OEM-specific NRC decoding, item 13)
            neg = _is_negative_response(response)
            if neg is not None:
                rejected_sid, nrc = neg
                if nrc in (NRC_SECURITY_ACCESS_DENIED, NRC_AUTHENTICATION_REQUIRED):
                    locked_services.add(rejected_sid)
                # Collect vendor-specific NRCs for the details line
                if 0x70 <= nrc <= 0x7E:
                    vendor_nrcs.add(nrc)

            # Track positive responses (services that worked)
            pos = _is_positive_response(response)
            if pos is not None:
                unlocked_services.add(pos)

        # Remove services from unlocked that were also locked (they may have worked after auth)
        # but keep them in both lists to show the progression
        if not locked_services and not has_0x27 and not has_0x29:
            auth_type = 'none'
        elif has_0x29_cert:
            auth_type = 'certificate_0x29'
        elif has_0x29:
            auth_type = 'challenge_response_0x29'
        elif has_0x27:
            auth_type = 'seed_key_0x27'
        else:
            auth_type = 'unknown'

        session = min(session_types) if session_types else SESSION_DEFAULT
        level = min(security_levels) if security_levels else None

        details_parts = []
        if locked_services:
            sids = ', '.join(f'0x{s:02X}' for s in sorted(locked_services))
            details_parts.append(f'locked services: {sids}')
        if unlocked_services - locked_services:
            sids = ', '.join(f'0x{s:02X}' for s in sorted(unlocked_services - locked_services))
            details_parts.append(f'open services: {sids}')
        if security_levels:
            details_parts.append(f'security levels: {sorted(security_levels)}')
        if session_types:
            details_parts.append(f'sessions: {sorted(session_types)}')
        if vendor_nrcs:
            decoded = ', '.join(f'0x{n:02X} ({decode_nrc(n)})' for n in sorted(vendor_nrcs))
            details_parts.append(f'vendor NRCs: {decoded}')

        patterns.append(
            GatewayAuthPattern(
                request_id=req_id,
                response_id=resp_id,
                auth_type=auth_type,
                session_type=session,
                security_level=level,
                locked_services=sorted(locked_services),
                unlocked_services=sorted(unlocked_services),
                details='; '.join(details_parts) if details_parts else 'no UDS exchanges observed',
            )
        )

    return patterns


# ---------------------------------------------------------------------------
# Gateway bypass detection
# ---------------------------------------------------------------------------


def analyse_gateway_filtering(
    messages: Sequence[can.Message],
    gateway_ids: Optional[Set[int]] = None,
    response_offset: int = 8,
) -> List[GatewayBypassFinding]:
    """Passive analysis of gateway filtering from captured traffic.

    - **Unfiltered IDs**: diagnostic request IDs that never got a securityAccessDenied
    - **Session fallback**: if switching back to default session removes the security requirement
    - **Timing window**: if there is a window after session change where services work before filtering
    - **Functional addressing**: if functional (broadcast) requests bypass while physical ones do not

    *response_offset*: added to the request ID to find the expected response ID (default 8).
    """
    findings: List[GatewayBypassFinding] = []
    uds_pairs = _extract_uds_pairs(messages, response_offset=response_offset)

    all_request_ids = set()
    locked_ids: Set[int] = set()
    functional_bypassed: Set[int] = set()
    physical_locked: Set[int] = set()

    # Standard functional addressing IDs
    functional_ids = {0x7DF, 0x18DB33F1}

    for (req_id, _resp_id), exchanges in uds_pairs.items():
        all_request_ids.add(req_id)
        this_id_locked = False

        for request, response in exchanges:
            if len(request) < 1:
                continue
            sid = request[0]

            # Skip tester present and session control — they are not "diagnostic services" for filtering
            if sid in (SID_TESTER_PRESENT, SID_DIAGNOSTIC_SESSION, SID_SECURITY_ACCESS, SID_AUTHENTICATION):
                continue

            neg = _is_negative_response(response)
            if neg is not None:
                _rejected_sid, nrc = neg
                if nrc in (NRC_SECURITY_ACCESS_DENIED, NRC_AUTHENTICATION_REQUIRED):
                    this_id_locked = True
                    if req_id in functional_ids:
                        # Functional addressing was also locked
                        pass
                    else:
                        physical_locked.add(req_id)

            pos = _is_positive_response(response)
            if pos is not None:
                if req_id in functional_ids:
                    if pos in {0x22, 0x23, 0x2E, 0x31, 0x34, 0x35, 0x36, 0x37}:
                        functional_bypassed.add(pos)

        if this_id_locked:
            locked_ids.add(req_id)

    # 1. Unfiltered IDs: diagnostic IDs that responded positively but were never locked
    if gateway_ids is None:
        gateway_ids = locked_ids

    for (req_id, _resp_id), exchanges in uds_pairs.items():
        if req_id in gateway_ids or req_id in functional_ids:
            continue
        # Did this ID respond positively to any diagnostic service?
        services_answered = set()
        for request, response in exchanges:
            if len(request) < 1:
                continue
            sid = request[0]
            if sid in (SID_TESTER_PRESENT, SID_DIAGNOSTIC_SESSION):
                continue
            pos = _is_positive_response(response)
            if pos is not None:
                services_answered.add(pos)

        if services_answered:
            sids = ', '.join(f'0x{s:02X}' for s in sorted(services_answered))
            findings.append(
                GatewayBypassFinding(
                    finding_type='unfiltered_id',
                    description=f'ID 0x{req_id:03X} answered diagnostic services without security: {sids}',
                    evidence=f'{len(services_answered)} service(s) answered positively, ID never returned NRC 0x33/0x34',
                    severity='finding' if any(s in {0x2E, 0x31, 0x34, 0x37} for s in services_answered) else 'info',
                )
            )

    # 2. Session fallback: check if services worked in default session for IDs that locked in extended
    for (req_id, _resp_id), exchanges in uds_pairs.items():
        in_default = False
        default_ok: Set[int] = set()
        extended_locked: Set[int] = set()

        for request, response in exchanges:
            if len(request) < 1:
                continue
            sid = request[0]

            if sid == SID_DIAGNOSTIC_SESSION and len(request) >= 2:
                in_default = request[1] == SESSION_DEFAULT

            neg = _is_negative_response(response)
            if neg is not None and neg[1] in (NRC_SECURITY_ACCESS_DENIED, NRC_AUTHENTICATION_REQUIRED):
                if not in_default:
                    extended_locked.add(neg[0])

            pos = _is_positive_response(response)
            if pos is not None and in_default and pos in extended_locked:
                default_ok.add(pos)

        if default_ok:
            sids = ', '.join(f'0x{s:02X}' for s in sorted(default_ok))
            findings.append(
                GatewayBypassFinding(
                    finding_type='session_fallback',
                    description=(
                        f'ID 0x{req_id:03X}: services locked in extended session work in default session: {sids}'
                    ),
                    evidence='Same services returned NRC 0x33 in extended but succeeded in default session',
                    severity='finding',
                )
            )

    # 3. Timing window: look for services that succeeded shortly after a session change
    messages_sorted = sorted(messages, key=lambda m: m.timestamp)
    session_change_times: Dict[int, List[float]] = defaultdict(list)

    # Pre-index: map (arb_id, response_data_bytes) -> list of timestamps for O(1) lookup
    response_timestamps: Dict[Tuple[int, bytes], List[float]] = defaultdict(list)
    for msg in messages_sorted:
        data = bytes(msg.data)
        if len(data) >= 2 and data[0] == SID_DIAGNOSTIC_SESSION + 0x40:
            session_change_times[msg.arbitration_id - response_offset].append(msg.timestamp)
        if not msg.is_error_frame and len(data) >= 1:
            response_timestamps[(msg.arbitration_id, data)].append(msg.timestamp)

    for (req_id, _resp_id), exchanges in uds_pairs.items():
        changes = session_change_times.get(req_id, [])
        if not changes:
            continue

        early_successes: List[Tuple[int, float]] = []
        for request, response in exchanges:
            if len(request) < 1:
                continue
            pos = _is_positive_response(response)
            if pos is None or pos in (SID_DIAGNOSTIC_SESSION, SID_TESTER_PRESENT):
                continue
            # Find if this service also got locked for this ID
            was_locked = any(
                _is_negative_response(r) is not None
                and _is_negative_response(r)[1] in (NRC_SECURITY_ACCESS_DENIED, NRC_AUTHENTICATION_REQUIRED)
                and _is_negative_response(r)[0] == pos
                for _, r in exchanges
            )
            if not was_locked:
                continue
            # Look up the timestamps of this response using the pre-built index
            resp_key = (req_id + response_offset, response)
            resp_times = response_timestamps.get(resp_key, [])
            for resp_time in resp_times:
                for change_time in changes:
                    delta = resp_time - change_time
                    if 0 < delta < 2.0:
                        early_successes.append((pos, delta))
                break  # Only need the first matching timestamp

        if early_successes:
            sids = ', '.join(f'0x{s:02X} ({d:.1f}s)' for s, d in early_successes[:5])
            findings.append(
                GatewayBypassFinding(
                    finding_type='timing_window',
                    description=f'ID 0x{req_id:03X}: locked services succeeded within 2s of session change: {sids}',
                    evidence=f'{len(early_successes)} service(s) succeeded in a timing window after session change',
                    severity='warning',
                )
            )

    # 4. Functional addressing: services that worked via functional but were locked via physical
    if physical_locked and functional_bypassed:
        overlap = {
            s for s in functional_bypassed if any(s in _exchanges_sids(uds_pairs, rid) for rid in physical_locked)
        }
        if overlap:
            sids = ', '.join(f'0x{s:02X}' for s in sorted(overlap))
            findings.append(
                GatewayBypassFinding(
                    finding_type='functional_addressing',
                    description=f'Services answered via functional addressing (0x7DF) but locked via physical: {sids}',
                    evidence='Functional requests bypassed gateway filtering for these services',
                    severity='finding',
                )
            )

    return findings


def _exchanges_sids(uds_pairs: Dict[Tuple[int, int], List[Tuple[bytes, bytes]]], req_id: int) -> Set[int]:
    """Services that were locked for a request ID."""
    sids: Set[int] = set()
    for (rid, _), exchanges in uds_pairs.items():
        if rid != req_id:
            continue
        for _request, response in exchanges:
            neg = _is_negative_response(response)
            if neg is not None and neg[1] in (NRC_SECURITY_ACCESS_DENIED, NRC_AUTHENTICATION_REQUIRED):
                sids.add(neg[0])
    return sids


# ---------------------------------------------------------------------------
# Cross-ECU seed/key correlation
# ---------------------------------------------------------------------------


def correlate_ecu_seeds(
    seed_key_pairs: Dict[int, List[Tuple[bytes, bytes]]],
) -> List[CrossEcuCorrelation]:
    """Check whether different ECUs share the same seed/key algorithm or master key.

    *seed_key_pairs*: mapping of request ID to captured (seed, key) pairs.
    """
    from can_explorer.core.seedkey_crack import COMMON_KEYS, crack

    correlations: List[CrossEcuCorrelation] = []
    ecu_ids = sorted(seed_key_pairs.keys())

    if len(ecu_ids) < 2:
        return correlations

    # Run crack on each ECU
    ecu_reports = {}
    for ecu_id in ecu_ids:
        pairs = seed_key_pairs[ecu_id]
        if len(pairs) >= 3:
            ecu_reports[ecu_id] = crack(pairs, candidate_keys=list(COMMON_KEYS))

    # Compare algorithms pairwise
    same_algo_groups: Dict[str, List[int]] = defaultdict(list)
    shared_key_pairs: List[Tuple[int, int]] = []

    for ecu_id, report in ecu_reports.items():
        for match in report.matches:
            algo_key = f'{match.algorithm}:{match.description}'
            same_algo_groups[algo_key].append(ecu_id)

            # Check for shared candidate keys
            candidate = match.parameters.get('candidate_key')
            if candidate:
                for other_id, other_report in ecu_reports.items():
                    if other_id <= ecu_id:
                        continue
                    for other_match in other_report.matches:
                        other_candidate = other_match.parameters.get('candidate_key')
                        if other_candidate and candidate == other_candidate:
                            shared_key_pairs.append((ecu_id, other_id))

    # Report same-algorithm groups
    for algo_key, ecus in same_algo_groups.items():
        if len(ecus) >= 2:
            pairs_list = [(ecus[i], ecus[j]) for i in range(len(ecus)) for j in range(i + 1, len(ecus))]
            correlations.append(
                CrossEcuCorrelation(
                    ecu_pairs=pairs_list,
                    finding='same_algorithm',
                    details=f'ECUs {[f"0x{e:03X}" for e in ecus]} use the same algorithm: {algo_key.split(":")[0]}',
                )
            )

    # Report shared keys
    if shared_key_pairs:
        correlations.append(
            CrossEcuCorrelation(
                ecu_pairs=list(set(shared_key_pairs)),
                finding='shared_key',
                details=(
                    f'{len(set(shared_key_pairs))} ECU pair(s) share the same master key '
                    '(a compromise of one ECU exposes the others)'
                ),
            )
        )

    # Report independent ECUs (ones that had matches but shared nothing)
    reported_ecus = set()
    for corr in correlations:
        for a, b in corr.ecu_pairs:
            reported_ecus.add(a)
            reported_ecus.add(b)

    independent = [eid for eid in ecu_reports if eid not in reported_ecus and ecu_reports[eid].matches]
    if independent and len(ecu_reports) > 1:
        for eid in independent:
            others = [o for o in ecu_reports if o != eid]
            correlations.append(
                CrossEcuCorrelation(
                    ecu_pairs=[(eid, others[0])],
                    finding='independent',
                    details=f'ECU 0x{eid:03X} uses a different algorithm from the others',
                )
            )

    return correlations


# ---------------------------------------------------------------------------
# Item 1: Automated gateway mapping
# ---------------------------------------------------------------------------


def map_gateway(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> GatewayMap:
    """Build a topology map of ECUs from captured UDS traffic.

    Groups ECUs by response-time clusters (suggesting different internal buses) and reports all
    diagnostic request IDs, functional addressing usage, and per-ECU summaries.
    """
    uds_pairs = _extract_uds_pairs(messages, response_offset=response_offset)
    functional_ids = {0x7DF, 0x18DB33F1}
    by_time = sorted(messages, key=lambda m: m.timestamp)

    ecus: List[EcuNode] = []
    response_times: Dict[int, List[float]] = defaultdict(list)

    # Compute per-ECU response times from the timeline
    for i, msg in enumerate(by_time):
        data = bytes(msg.data)
        if len(data) < 1 or data[0] >= 0x40 or data[0] == 0x7F:
            continue
        if msg.arbitration_id in functional_ids:
            continue
        req_id = msg.arbitration_id
        resp_id = req_id + response_offset
        for j in range(i + 1, min(i + 100, len(by_time))):
            resp = by_time[j]
            if resp.timestamp - msg.timestamp > 5.0:
                break
            if resp.arbitration_id == resp_id and not resp.is_error_frame:
                delta_ms = (resp.timestamp - msg.timestamp) * 1000.0
                response_times[req_id].append(delta_ms)
                break

    seen_functional: List[int] = []
    for (req_id, _resp_id), _exchanges in uds_pairs.items():
        if req_id in functional_ids and req_id not in seen_functional:
            seen_functional.append(req_id)

    for (req_id, resp_id), exchanges in uds_pairs.items():
        if req_id in functional_ids:
            continue
        sessions: Set[int] = set()
        services: Set[int] = set()
        has_0x27 = False
        has_0x29 = False
        has_0x29_cert = False

        for request, response in exchanges:
            if len(request) < 1:
                continue
            sid = request[0]
            if sid == SID_DIAGNOSTIC_SESSION and len(request) >= 2:
                neg = _is_negative_response(response)
                if neg is None:
                    sessions.add(request[1])
            if sid == SID_SECURITY_ACCESS:
                has_0x27 = True
            if sid == SID_AUTHENTICATION:
                has_0x29 = True
                if len(request) >= 2 and request[1] in (0x01, 0x02):
                    has_0x29_cert = True
            pos = _is_positive_response(response)
            neg = _is_negative_response(response)
            if pos is not None:
                services.add(pos)
            if neg is not None:
                services.add(neg[0])

        if has_0x29_cert:
            auth_type = 'certificate_0x29'
        elif has_0x29:
            auth_type = 'challenge_response_0x29'
        elif has_0x27:
            auth_type = 'seed_key_0x27'
        else:
            auth_type = 'none'

        times = response_times.get(req_id, [])
        mean_ms = statistics.mean(times) if times else 0.0

        ecus.append(
            EcuNode(
                request_id=req_id,
                response_id=resp_id,
                supported_sessions=sorted(sessions),
                auth_type=auth_type,
                services_seen=sorted(services),
                mean_response_ms=round(mean_ms, 2),
            )
        )

    # Cluster ECUs by response time into segments
    segments: Dict[str, List[int]] = {}
    if ecus:
        sorted_ecus = sorted(ecus, key=lambda e: e.mean_response_ms)
        cluster_label = 0
        current_cluster: List[int] = [sorted_ecus[0].request_id]
        prev_ms = sorted_ecus[0].mean_response_ms
        for ecu in sorted_ecus[1:]:
            # A gap > 2x or > 10ms absolute suggests a different bus segment
            if ecu.mean_response_ms > prev_ms * 2.0 + 5.0:
                cluster_label += 1
                segments[f'segment_{cluster_label - 1}'] = current_cluster
                current_cluster = []
            current_cluster.append(ecu.request_id)
            prev_ms = ecu.mean_response_ms
        segments[f'segment_{cluster_label}'] = current_cluster

    diagnostic_ids = sorted({e.request_id for e in ecus})

    return GatewayMap(
        ecus=ecus,
        segments=segments,
        diagnostic_ids=diagnostic_ids,
        functional_ids_seen=sorted(seen_functional),
    )


# ---------------------------------------------------------------------------
# Item 2: UDS service access matrix
# ---------------------------------------------------------------------------


def build_access_matrix(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> AccessMatrix:
    """Build a per-ECU, per-service, per-session security classification from captured traffic."""
    uds_pairs = _extract_uds_pairs(messages, response_offset=response_offset)
    functional_ids = {0x7DF, 0x18DB33F1}
    rows: List[AccessRow] = []
    all_ecus: Set[int] = set()
    all_sessions: Set[int] = set()
    all_services: Set[int] = set()

    for (req_id, _resp_id), exchanges in uds_pairs.items():
        if req_id in functional_ids:
            continue
        all_ecus.add(req_id)

        # Track current session for this ECU
        current_session = SESSION_DEFAULT
        has_0x27 = False

        # Per-service, per-session tracking
        service_session_status: Dict[Tuple[int, int], str] = {}  # (service, session) -> status
        service_session_auth: Dict[Tuple[int, int], str] = {}  # (service, session) -> auth_required
        service_session_level: Dict[Tuple[int, int], Optional[int]] = {}

        for request, response in exchanges:
            if len(request) < 1:
                continue
            sid = request[0]

            if sid == SID_DIAGNOSTIC_SESSION and len(request) >= 2:
                neg = _is_negative_response(response)
                if neg is None:
                    current_session = request[1]
                    all_sessions.add(current_session)
                continue

            if sid == SID_SECURITY_ACCESS and len(request) >= 2:
                has_0x27 = True
                continue

            if sid == SID_AUTHENTICATION:
                continue

            if sid in (SID_TESTER_PRESENT,):
                continue

            all_services.add(sid)
            key = (sid, current_session)

            neg = _is_negative_response(response)
            pos = _is_positive_response(response)

            if neg is not None:
                _rejected_sid, nrc = neg
                if nrc in (NRC_SECURITY_ACCESS_DENIED, NRC_AUTHENTICATION_REQUIRED):
                    service_session_status[key] = 'denied'
                    if nrc == NRC_AUTHENTICATION_REQUIRED:
                        service_session_auth[key] = '0x29'
                    else:
                        service_session_auth[key] = '0x27' if has_0x27 else 'unknown'
                elif nrc == NRC_CONDITIONS_NOT_CORRECT:
                    if key not in service_session_status:
                        service_session_status[key] = 'conditional'
                        service_session_auth[key] = 'unknown'
                elif key not in service_session_status:
                    service_session_status[key] = 'denied'
                    service_session_auth[key] = 'none'
            elif pos is not None:
                # Positive trumps a previous 'denied' (the service was unlocked after auth)
                prev = service_session_status.get(key)
                if prev == 'denied':
                    service_session_status[key] = 'allowed'
                    # Auth was required but succeeded
                elif prev != 'allowed':
                    service_session_status[key] = 'allowed'
                    service_session_auth[key] = 'none'

        for (sid, session), status in service_session_status.items():
            auth = service_session_auth.get((sid, session), 'none')
            rows.append(
                AccessRow(
                    ecu_id=req_id,
                    service=sid,
                    service_name=_SID_NAMES.get(sid, f'0x{sid:02X}'),
                    session=session,
                    auth_required=auth,
                    security_level=service_session_level.get((sid, session)),
                    status=status,
                )
            )

    if not all_sessions:
        all_sessions.add(SESSION_DEFAULT)

    return AccessMatrix(
        rows=sorted(rows, key=lambda r: (r.ecu_id, r.session, r.service)),
        ecus=sorted(all_ecus),
        sessions=sorted(all_sessions),
        services=sorted(all_services),
    )


# ---------------------------------------------------------------------------
# Item 3: Unlock sequence extraction
# ---------------------------------------------------------------------------


def extract_unlock_sequence(
    messages: Sequence[can.Message],
    ecu_id: int,
    response_offset: int = 8,
) -> Optional[UnlockSequence]:
    """Extract the exact authentication sequence used for one ECU from captured traffic.

    Returns None if no authentication flow was observed for *ecu_id*.
    """
    resp_id = ecu_id + response_offset
    by_time = sorted(messages, key=lambda m: m.timestamp)
    functional_ids = {0x7DF, 0x18DB33F1}

    # Find all request/response exchanges for this ECU
    auth_sids = {SID_DIAGNOSTIC_SESSION, SID_SECURITY_ACCESS, SID_AUTHENTICATION, SID_TESTER_PRESENT}
    steps: List[UnlockStep] = []
    auth_type = 'none'

    i = 0
    while i < len(by_time):
        msg = by_time[i]
        if msg.is_error_frame or msg.is_remote_frame:
            i += 1
            continue
        data = bytes(msg.data)
        if len(data) < 1:
            i += 1
            continue

        # Match requests to this ECU or functional
        is_this_ecu = msg.arbitration_id == ecu_id
        is_functional = msg.arbitration_id in functional_ids
        if not is_this_ecu and not is_functional:
            i += 1
            continue

        sid = data[0]
        if sid >= 0x40 or sid == 0x7F:
            i += 1
            continue

        if sid not in auth_sids:
            i += 1
            continue

        # Find the response
        resp_data_hex = ''
        for j in range(i + 1, min(i + 100, len(by_time))):
            resp = by_time[j]
            if resp.timestamp - msg.timestamp > 5.0:
                break
            if resp.arbitration_id == resp_id and not resp.is_error_frame:
                resp_data_hex = bytes(resp.data).hex()
                break

        # Classify the step
        sub = data[1] if len(data) >= 2 else None
        if sid == SID_DIAGNOSTIC_SESSION:
            action = 'session_change'
        elif sid == SID_SECURITY_ACCESS:
            if sub is not None and sub % 2 == 1:
                action = 'security_access_seed'
                auth_type = 'seed_key_0x27'
            else:
                action = 'security_access_key'
                auth_type = 'seed_key_0x27'
        elif sid == SID_AUTHENTICATION:
            action = 'authentication'
            auth_type = 'challenge_response_0x29'
        elif sid == SID_TESTER_PRESENT:
            action = 'tester_present'
        else:
            action = 'unknown'

        steps.append(
            UnlockStep(
                action=action,
                sid=sid,
                sub_function=sub,
                request_data=data.hex(),
                response_data=resp_data_hex,
                timestamp=msg.timestamp,
                timing_constraint_ms=None,  # Filled below
            )
        )
        i += 1

    if not steps or auth_type == 'none':
        return None

    # Compute timing constraints between consecutive steps
    for idx in range(len(steps) - 1):
        delta_ms = (steps[idx + 1].timestamp - steps[idx].timestamp) * 1000.0
        steps[idx] = UnlockStep(
            action=steps[idx].action,
            sid=steps[idx].sid,
            sub_function=steps[idx].sub_function,
            request_data=steps[idx].request_data,
            response_data=steps[idx].response_data,
            timestamp=steps[idx].timestamp,
            timing_constraint_ms=round(delta_ms, 1),
        )

    total_ms = (steps[-1].timestamp - steps[0].timestamp) * 1000.0 if len(steps) > 1 else 0.0

    return UnlockSequence(
        ecu_id=ecu_id,
        response_id=resp_id,
        steps=steps,
        total_time_ms=round(total_ms, 1),
        auth_type=auth_type,
    )


# ---------------------------------------------------------------------------
# Item 5: NRC fingerprinting and ECU identification
# ---------------------------------------------------------------------------


# Known vendor NRC patterns (from public documentation and community knowledge)
_VENDOR_NRC_HINTS: Dict[frozenset, str] = {
    frozenset({0x72}): 'General programming failure (common in Bosch ECUs)',
    frozenset({0x73}): 'Wrong block sequence counter (common in Continental ECUs)',
}


def fingerprint_ecu(
    messages: Sequence[can.Message],
    ecu_id: int,
    response_offset: int = 8,
) -> EcuFingerprint:
    """Build an NRC pattern and timing fingerprint for one ECU.

    The fingerprint includes the set of NRCs the ECU returns, response timing statistics, and
    vendor hints where the NRC pattern matches a known ECU vendor stack.
    """
    resp_id = ecu_id + response_offset
    by_time = sorted(messages, key=lambda m: m.timestamp)

    nrc_counts: Dict[int, int] = defaultdict(int)
    sessions: Set[int] = set()
    response_times_ms: List[float] = []

    for i, msg in enumerate(by_time):
        data = bytes(msg.data)
        if len(data) < 1 or msg.arbitration_id != ecu_id:
            continue
        sid = data[0]
        if sid >= 0x40 or sid == 0x7F:
            continue
        # Find the response
        for j in range(i + 1, min(i + 100, len(by_time))):
            resp = by_time[j]
            if resp.timestamp - msg.timestamp > 5.0:
                break
            if resp.arbitration_id == resp_id and not resp.is_error_frame:
                resp_data = bytes(resp.data)
                delta_ms = (resp.timestamp - msg.timestamp) * 1000.0
                response_times_ms.append(delta_ms)

                neg = _is_negative_response(resp_data)
                if neg is not None:
                    nrc_counts[neg[1]] += 1

                # Track session
                if sid == SID_DIAGNOSTIC_SESSION and len(data) >= 2:
                    pos = _is_positive_response(resp_data)
                    if pos is not None:
                        sessions.add(data[1])
                break

    # Build timing profile
    if response_times_ms:
        timing = {
            'mean_ms': round(statistics.mean(response_times_ms), 2),
            'std_ms': round(statistics.stdev(response_times_ms), 2) if len(response_times_ms) >= 2 else 0.0,
            'min_ms': round(min(response_times_ms), 2),
            'max_ms': round(max(response_times_ms), 2),
        }
    else:
        timing = {'mean_ms': 0.0, 'std_ms': 0.0, 'min_ms': 0.0, 'max_ms': 0.0}

    # Vendor hints
    vendor_hints: List[str] = []
    nrc_set_frozen = frozenset(nrc_counts.keys())
    for pattern, hint in _VENDOR_NRC_HINTS.items():
        if pattern.issubset(nrc_set_frozen):
            vendor_hints.append(hint)

    # Heuristic hints from NRC combinations
    if NRC_EXCEEDED_NUMBER_OF_ATTEMPTS in nrc_counts or NRC_REQUIRED_TIME_DELAY in nrc_counts:
        vendor_hints.append('Rate limiting detected: the ECU enforces attempt limits or time delays')
    if nrc_counts.get(NRC_SERVICE_NOT_SUPPORTED, 0) > 5:
        vendor_hints.append('Many unsupported services: the ECU has a narrow service set')

    return EcuFingerprint(
        request_id=ecu_id,
        response_id=resp_id,
        nrc_set=sorted(nrc_counts.keys()),
        nrc_counts=dict(nrc_counts),
        supported_sessions=sorted(sessions),
        timing_profile=timing,
        vendor_hints=vendor_hints,
    )


# ---------------------------------------------------------------------------
# Item 6: Multi-session probing planner
# ---------------------------------------------------------------------------


def plan_probing(
    access_matrix: AccessMatrix,
    auth_patterns: Optional[List[GatewayAuthPattern]] = None,
    priority: str = 'coverage',
) -> List[ProbeStep]:
    """Suggest which services to try next to maximize attack-surface coverage.

    Prioritises:
    - Services that returned conditionsNotCorrect (may work in another session)
    - Services that worked via functional but not physical addressing
    - Write/control services at a lower security level than expected
    - Services not yet tried in all available sessions
    """
    steps: List[ProbeStep] = []

    # Build lookup of what was observed
    seen: Dict[Tuple[int, int, int], AccessRow] = {}
    for row in access_matrix.rows:
        seen[(row.ecu_id, row.service, row.session)] = row

    # 1. Services with conditionsNotCorrect -- try in other sessions
    for row in access_matrix.rows:
        if row.status == 'conditional':
            for session in access_matrix.sessions:
                if session != row.session and (row.ecu_id, row.service, session) not in seen:
                    steps.append(
                        ProbeStep(
                            ecu_id=row.ecu_id,
                            service=row.service,
                            service_name=row.service_name,
                            session=session,
                            auth_level=None,
                            rationale=(
                                f'{row.service_name} returned conditionsNotCorrect in session {row.session}; '
                                f'try session {session}'
                            ),
                            priority='high',
                        )
                    )

    # 2. Denied services -- try after lower security levels
    for row in access_matrix.rows:
        if row.status == 'denied' and row.auth_required == '0x27':
            # Suggest trying with different security levels
            steps.append(
                ProbeStep(
                    ecu_id=row.ecu_id,
                    service=row.service,
                    service_name=row.service_name,
                    session=row.session,
                    auth_level=1,
                    rationale=f'{row.service_name} requires 0x27; try unlocking at level 1 first',
                    priority='medium',
                )
            )

    # 3. Services not tried in all sessions
    write_sids = {SID_WRITE_DID, SID_IO_CONTROL, SID_ROUTINE_CONTROL, SID_REQUEST_DOWNLOAD, 0x3D}
    for ecu_id in access_matrix.ecus:
        for service in access_matrix.services:
            for session in access_matrix.sessions:
                if (ecu_id, service, session) not in seen:
                    prio = 'medium' if service in write_sids else 'low'
                    steps.append(
                        ProbeStep(
                            ecu_id=ecu_id,
                            service=service,
                            service_name=_SID_NAMES.get(service, f'0x{service:02X}'),
                            session=session,
                            auth_level=None,
                            rationale=f'Service 0x{service:02X} not yet observed in session {session}',
                            priority=prio,
                        )
                    )

    # Sort by priority
    prio_order = {'high': 0, 'medium': 1, 'low': 2}
    steps.sort(key=lambda s: (prio_order.get(s.priority, 3), s.ecu_id, s.service))
    return steps


# ---------------------------------------------------------------------------
# Item 7: DID/routine security classification
# ---------------------------------------------------------------------------


def classify_did_security(
    messages: Sequence[can.Message],
    ecu_id: int,
    response_offset: int = 8,
) -> List[DidSecurityInfo]:
    """Classify the security posture of individual DIDs and routines for one ECU.

    From captured traffic, determine which DIDs are readable/writable and which require authentication.
    """
    resp_id = ecu_id + response_offset
    by_time = sorted(messages, key=lambda m: m.timestamp)

    # Track per-DID/routine: (service, identifier) -> info
    did_info: Dict[Tuple[int, int], Dict[str, Any]] = {}
    has_0x27 = False

    for i, msg in enumerate(by_time):
        data = bytes(msg.data)
        if len(data) < 1 or msg.arbitration_id != ecu_id:
            continue
        sid = data[0]
        if sid == SID_SECURITY_ACCESS:
            has_0x27 = True
            continue
        if sid == SID_AUTHENTICATION:
            continue

        # Extract DID or routine ID
        did = None
        if sid in (SID_READ_DID, SID_WRITE_DID) and len(data) >= 3:
            did = (data[1] << 8) | data[2]
        elif sid == SID_ROUTINE_CONTROL and len(data) >= 4:
            did = (data[2] << 8) | data[3]
        else:
            continue

        key = (sid, did)

        # Find the response
        for j in range(i + 1, min(i + 100, len(by_time))):
            resp = by_time[j]
            if resp.timestamp - msg.timestamp > 5.0:
                break
            if resp.arbitration_id == resp_id and not resp.is_error_frame:
                resp_data = bytes(resp.data)
                neg = _is_negative_response(resp_data)
                pos = _is_positive_response(resp_data)

                if key not in did_info:
                    did_info[key] = {
                        'readable': False,
                        'writable': False,
                        'auth_required': 'none',
                        'nrc': None,
                    }

                if neg is not None:
                    did_info[key]['nrc'] = neg[1]
                    if neg[1] in (NRC_SECURITY_ACCESS_DENIED, NRC_AUTHENTICATION_REQUIRED):
                        if neg[1] == NRC_AUTHENTICATION_REQUIRED:
                            did_info[key]['auth_required'] = '0x29'
                        elif has_0x27:
                            did_info[key]['auth_required'] = '0x27'
                        else:
                            did_info[key]['auth_required'] = 'unknown'
                elif pos is not None:
                    if sid == SID_READ_DID:
                        did_info[key]['readable'] = True
                    elif sid == SID_WRITE_DID:
                        did_info[key]['writable'] = True
                    elif sid == SID_ROUTINE_CONTROL:
                        did_info[key]['readable'] = True
                    # If it succeeded, auth is not required (or was already done)
                    if did_info[key]['auth_required'] != 'none':
                        # It was locked before but now works -- auth was done
                        pass
                    else:
                        did_info[key]['auth_required'] = 'none'
                break

    result: List[DidSecurityInfo] = []
    for (sid, did), info in sorted(did_info.items()):
        result.append(
            DidSecurityInfo(
                ecu_id=ecu_id,
                identifier=did,
                service=sid,
                service_name=_SID_NAMES.get(sid, f'0x{sid:02X}'),
                readable=info['readable'],
                writable=info['writable'],
                auth_required=info['auth_required'],
                nrc=info['nrc'],
            )
        )
    return result


# ---------------------------------------------------------------------------
# Item 8: Auth flow timing analysis
# ---------------------------------------------------------------------------


def analyse_auth_timing(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> List[AuthTimingResult]:
    """Measure the timing of each step of authentication flows for all ECUs.

    Reports request-to-seed, seed-to-key and key-to-response intervals. Unusually fast or slow
    responses can indicate weak seed generation, trivial key derivation or rate limiting.
    """
    by_time = sorted(messages, key=lambda m: m.timestamp)
    functional_ids = {0x7DF, 0x18DB33F1}
    results: List[AuthTimingResult] = []

    # Group 0x27 flows per ECU
    ecu_flows: Dict[int, List[Tuple[str, float]]] = defaultdict(list)

    for _i, msg in enumerate(by_time):
        data = bytes(msg.data)
        if len(data) < 2 or msg.is_error_frame:
            continue

        arb = msg.arbitration_id
        if arb in functional_ids:
            continue

        # Request: 0x27 sub
        if data[0] == SID_SECURITY_ACCESS:
            sub = data[1]
            if sub % 2 == 1:
                ecu_flows[arb].append(('seed_request', msg.timestamp))
            else:
                ecu_flows[arb].append(('key_send', msg.timestamp))

        # Response: 0x67 sub
        if data[0] == 0x67:
            req_id = arb - response_offset
            sub = data[1]
            if sub % 2 == 1:
                ecu_flows[req_id].append(('seed_response', msg.timestamp))
            else:
                ecu_flows[req_id].append(('key_accepted', msg.timestamp))

        # Negative response to 0x27
        neg = _is_negative_response(data)
        if neg is not None and neg[0] == SID_SECURITY_ACCESS:
            req_id = arb - response_offset
            ecu_flows[req_id].append(('key_rejected', msg.timestamp))

    for ecu_id, flow in ecu_flows.items():
        # Parse sequential steps
        request_to_seed_times: List[float] = []
        seed_to_key_times: List[float] = []
        key_to_response_times: List[float] = []
        total_times: List[float] = []

        last_seed_req = None
        last_seed_resp = None
        last_key_send = None
        flow_start = None

        for event, ts in flow:
            if event == 'seed_request':
                last_seed_req = ts
                flow_start = ts
            elif event == 'seed_response':
                if last_seed_req is not None:
                    request_to_seed_times.append((ts - last_seed_req) * 1000.0)
                last_seed_resp = ts
            elif event == 'key_send':
                if last_seed_resp is not None:
                    seed_to_key_times.append((ts - last_seed_resp) * 1000.0)
                last_key_send = ts
            elif event in ('key_accepted', 'key_rejected'):
                if last_key_send is not None:
                    key_to_response_times.append((ts - last_key_send) * 1000.0)
                if flow_start is not None:
                    total_times.append((ts - flow_start) * 1000.0)
                # Reset for next flow
                last_seed_req = None
                last_seed_resp = None
                last_key_send = None
                flow_start = None

        if not total_times:
            continue

        findings: List[str] = []

        mean_req_seed = statistics.mean(request_to_seed_times) if request_to_seed_times else None
        mean_seed_key = statistics.mean(seed_to_key_times) if seed_to_key_times else None
        mean_key_resp = statistics.mean(key_to_response_times) if key_to_response_times else None
        mean_total = statistics.mean(total_times)
        jitter = statistics.stdev(total_times) if len(total_times) >= 2 else None

        # Heuristic findings
        if mean_req_seed is not None and mean_req_seed < 1.0:
            findings.append(
                f'Seed generated in {mean_req_seed:.1f} ms: very fast, may indicate weak PRNG or fixed seed'
            )
        if mean_key_resp is not None and mean_key_resp < 0.5:
            findings.append(f'Key verified in {mean_key_resp:.1f} ms: very fast, may indicate trivial key derivation')
        if jitter is not None and jitter > 50.0:
            findings.append(f'High timing jitter ({jitter:.1f} ms): may indicate rate limiting or load variation')

        # Check for increasing delays (rate limiting)
        if len(key_to_response_times) >= 3:
            diffs = [
                key_to_response_times[i + 1] - key_to_response_times[i] for i in range(len(key_to_response_times) - 1)
            ]
            if all(d > 0 for d in diffs):
                findings.append('Response times are strictly increasing: possible rate limiting')

        results.append(
            AuthTimingResult(
                ecu_id=ecu_id,
                auth_type='seed_key_0x27',
                request_to_seed_ms=round(mean_req_seed, 2) if mean_req_seed is not None else None,
                seed_to_key_ms=round(mean_seed_key, 2) if mean_seed_key is not None else None,
                key_to_response_ms=round(mean_key_resp, 2) if mean_key_resp is not None else None,
                total_ms=round(mean_total, 2),
                jitter_ms=round(jitter, 2) if jitter is not None else None,
                findings=findings,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Item 4: Consolidated gateway report
# ---------------------------------------------------------------------------


def gateway_report(
    messages: Sequence[can.Message],
    response_offset: int = 8,
    seed_key_pairs: Optional[Dict[int, List[Tuple[bytes, bytes]]]] = None,
) -> GatewayReport:
    """Run all gateway analyses and produce a combined report.

    *seed_key_pairs*: optional pre-extracted seed/key pairs per ECU request ID; when given,
    cross-ECU correlation is included.
    """
    auth_patterns = analyse_gateway_auth(messages, response_offset=response_offset)
    bypass_findings = analyse_gateway_filtering(messages, response_offset=response_offset)
    gw_map = map_gateway(messages, response_offset=response_offset)
    access = build_access_matrix(messages, response_offset=response_offset)

    correlations: List[CrossEcuCorrelation] = []
    if seed_key_pairs and len(seed_key_pairs) >= 2:
        try:
            correlations = correlate_ecu_seeds(seed_key_pairs)
        except Exception:
            pass

    # Extract unlock sequences for each ECU
    unlock_sequences: Dict[int, UnlockSequence] = {}
    for ecu in gw_map.ecus:
        seq = extract_unlock_sequence(messages, ecu.request_id, response_offset=response_offset)
        if seq is not None:
            unlock_sequences[ecu.request_id] = seq

    # Severity summary
    severity_summary: Dict[str, int] = {'finding': 0, 'warning': 0, 'info': 0}
    for f in bypass_findings:
        severity_summary[f.severity] = severity_summary.get(f.severity, 0) + 1
    for c in correlations:
        if c.finding == 'shared_key':
            severity_summary['finding'] += 1
        elif c.finding == 'same_algorithm':
            severity_summary['warning'] += 1
        else:
            severity_summary['info'] += 1

    # Overall severity
    if severity_summary.get('finding', 0) >= 3:
        overall_severity = 'critical'
    elif severity_summary.get('finding', 0) >= 1:
        overall_severity = 'finding'
    elif severity_summary.get('warning', 0) >= 1:
        overall_severity = 'warning'
    else:
        overall_severity = 'info'

    # Prioritised summary lines
    summary: List[str] = []
    for f in bypass_findings:
        if f.severity == 'finding':
            summary.append(f'{f.finding_type}: {f.description}')
    for c in correlations:
        if c.finding == 'shared_key':
            summary.append(f'shared key: {c.details}')
    for f in bypass_findings:
        if f.severity == 'warning':
            summary.append(f'{f.finding_type}: {f.description}')
    if not summary:
        summary.append(f'{len(auth_patterns)} ECU(s) analysed, no critical findings')

    return GatewayReport(
        gateway_map=gw_map,
        access_matrix=access,
        auth_patterns=auth_patterns,
        bypass_findings=bypass_findings,
        correlations=correlations,
        unlock_sequences=unlock_sequences,
        severity_summary=severity_summary,
        overall_severity=overall_severity,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Item 11: Passive bus segmentation detection
# ---------------------------------------------------------------------------


def detect_bus_segments(
    messages: Sequence[can.Message],
    max_jitter_ratio: float = 0.20,
) -> List[BusSegment]:
    """Cluster CAN IDs by timing characteristics and ID range to infer physical bus segments.

    IDs with periodic traffic (jitter < *max_jitter_ratio* of the mean interval) are classified
    as periodic; the rest as event-driven.  IDs are then grouped into contiguous ranges (gaps
    larger than 0x100 start a new segment).

    This is a heuristic -- physical segmentation cannot be reliably determined from timing alone,
    but it gives a useful first approximation.
    """
    if not messages:
        return []

    # Collect timestamps per ID
    ts_by_id: Dict[int, List[float]] = defaultdict(list)
    for msg in messages:
        if msg.is_error_frame or msg.is_remote_frame:
            continue
        ts_by_id[msg.arbitration_id].append(msg.timestamp)

    if not ts_by_id:
        return []

    # Compute rate and periodicity per ID
    id_stats: Dict[int, Tuple[float, bool]] = {}  # id -> (rate_hz, is_periodic)
    for arb_id, timestamps in ts_by_id.items():
        timestamps.sort()
        if len(timestamps) < 2:
            id_stats[arb_id] = (0.0, False)
            continue
        span = timestamps[-1] - timestamps[0]
        if span <= 0:
            id_stats[arb_id] = (0.0, False)
            continue
        rate = (len(timestamps) - 1) / span
        intervals = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
        mean_interval = statistics.mean(intervals)
        if mean_interval <= 0:
            id_stats[arb_id] = (rate, False)
            continue
        std_interval = statistics.pstdev(intervals)
        jitter = std_interval / mean_interval
        id_stats[arb_id] = (rate, jitter < max_jitter_ratio)

    # Sort IDs and cluster by contiguous ranges (gap > 0x100 starts new segment)
    sorted_ids = sorted(id_stats.keys())
    clusters: List[List[int]] = []
    current: List[int] = [sorted_ids[0]]
    for i in range(1, len(sorted_ids)):
        if sorted_ids[i] - sorted_ids[i - 1] > 0x100:
            clusters.append(current)
            current = []
        current.append(sorted_ids[i])
    clusters.append(current)

    segments: List[BusSegment] = []
    for idx, cluster in enumerate(clusters):
        rates = [id_stats[arb_id][0] for arb_id in cluster]
        periodic_ids = [arb_id for arb_id in cluster if id_stats[arb_id][1]]
        event_ids = [arb_id for arb_id in cluster if not id_stats[arb_id][1]]
        mean_rate = statistics.mean(rates) if rates else 0.0
        periodic_ratio = len(periodic_ids) / len(cluster) if cluster else 0.0
        segments.append(
            BusSegment(
                name=f'segment_{idx}',
                ids=cluster,
                id_range=(min(cluster), max(cluster)),
                mean_rate_hz=round(mean_rate, 2),
                periodic_ratio=round(periodic_ratio, 3),
                event_driven_ids=event_ids,
            )
        )

    return segments


# ---------------------------------------------------------------------------
# Item 12: Response-after-reset analysis
# ---------------------------------------------------------------------------


def analyse_reset_behaviour(
    messages: Sequence[can.Message],
    response_offset: int = 8,
    window_ms: float = 2000.0,
) -> List[ResetBehaviourFinding]:
    """Detect services that succeed shortly after an ECU reset (0x11).

    A service that works within *window_ms* of a reset positive response may indicate a boot-up
    window where security is not yet enforced.  This is common on ECUs that initialise their
    security context after the application starts.
    """
    if not messages:
        return []

    by_time = sorted(messages, key=lambda m: m.timestamp)
    findings: List[ResetBehaviourFinding] = []

    # Find ECU reset responses
    reset_times: Dict[int, List[float]] = defaultdict(list)  # request_id -> reset timestamps
    for msg in by_time:
        data = bytes(msg.data)
        # Positive response to ECUReset: 0x51 sub
        if len(data) >= 2 and data[0] == 0x51:
            req_id = msg.arbitration_id - response_offset
            reset_times[req_id].append(msg.timestamp)

    if not reset_times:
        return []

    # For each reset, check which services succeed within the window
    for msg in by_time:
        data = bytes(msg.data)
        pos_sid = _is_positive_response(data)
        if pos_sid is None:
            continue
        # Skip reset responses themselves and tester present
        if pos_sid in (SID_ECU_RESET, SID_TESTER_PRESENT, SID_DIAGNOSTIC_SESSION):
            continue
        req_id = msg.arbitration_id - response_offset
        if req_id not in reset_times:
            continue
        for reset_ts in reset_times[req_id]:
            delta_ms = (msg.timestamp - reset_ts) * 1000.0
            if 0 < delta_ms <= window_ms:
                # Determine session from surrounding traffic
                session = SESSION_DEFAULT
                service_name = _SID_NAMES.get(pos_sid, f'0x{pos_sid:02X}')
                is_write = pos_sid in (SID_WRITE_DID, SID_IO_CONTROL, SID_ROUTINE_CONTROL, SID_REQUEST_DOWNLOAD)
                severity = 'finding' if is_write else 'warning'
                findings.append(
                    ResetBehaviourFinding(
                        ecu_id=req_id,
                        service=pos_sid,
                        service_name=service_name,
                        session=session,
                        success_time_after_reset_ms=round(delta_ms, 1),
                        finding=(
                            f'{service_name} succeeded {delta_ms:.0f} ms after reset: '
                            f'possible boot-up security bypass window'
                        ),
                        severity=severity,
                    )
                )
                break  # Only report once per service per reset

    return findings


# ---------------------------------------------------------------------------
# Auth state machine recovery (roadmap 1.1)
# ---------------------------------------------------------------------------


@dataclass
class AuthState:
    """One state in the recovered authentication state machine."""

    name: str  # e.g. 'default_session', 'extended_session', 'unlocked_level_1'
    session: int
    security_level: Optional[int]
    services_available: List[int]


@dataclass
class AuthTransition:
    """A transition between authentication states."""

    from_state: str
    to_state: str
    action: str  # e.g. 'session_change(0x03)', 'security_access(level=1)', 'tester_present'
    sid: int
    sub_function: Optional[int]
    timing_constraint_ms: Optional[float]  # Must complete within this time
    mandatory: bool  # Whether skipping this step causes failure


@dataclass
class AuthStateMachine:
    """The recovered authentication state machine for one ECU."""

    ecu_id: int
    response_id: int
    states: List[AuthState]
    transitions: List[AuthTransition]
    initial_state: str
    tester_present_required: bool
    tester_present_interval_ms: Optional[float]
    total_unlock_time_ms: Optional[float]
    findings: List[str] = field(default_factory=list)


def recover_auth_state_machine(
    messages: Sequence[can.Message],
    ecu_id: int,
    response_offset: int = 8,
) -> Optional[AuthStateMachine]:
    """Reconstruct the full authentication state machine for one ECU from captured UDS traffic.

    Builds on ``extract_unlock_sequence`` to model which steps are mandatory, order-dependent
    and timing-constrained.
    """
    seq = extract_unlock_sequence(messages, ecu_id, response_offset)
    if seq is None:
        return None

    resp_id = ecu_id + response_offset
    by_time = sorted(messages, key=lambda m: m.timestamp)

    states: List[AuthState] = []
    transitions: List[AuthTransition] = []
    findings: List[str] = []

    # Build states from the unlock sequence
    current_session = SESSION_DEFAULT
    current_level: Optional[int] = None
    state_name = 'default_session'
    states.append(AuthState(name=state_name, session=current_session, security_level=None, services_available=[]))

    tester_present_seen = False
    tester_present_intervals: List[float] = []
    last_tester_present_ts: Optional[float] = None

    # Track tester present messages
    for msg in by_time:
        data = bytes(msg.data)
        if msg.arbitration_id == ecu_id and len(data) >= 1 and data[0] == SID_TESTER_PRESENT:
            tester_present_seen = True
            if last_tester_present_ts is not None:
                tester_present_intervals.append((msg.timestamp - last_tester_present_ts) * 1000.0)
            last_tester_present_ts = msg.timestamp

    tester_present_interval_ms: Optional[float] = None
    if tester_present_intervals:
        tester_present_interval_ms = round(statistics.mean(tester_present_intervals), 1)

    for step in seq.steps:
        prev_state = state_name

        if step.action == 'session_change' and step.sub_function is not None:
            current_session = step.sub_function
            state_name = f'session_{current_session:#x}'
            states.append(
                AuthState(
                    name=state_name,
                    session=current_session,
                    security_level=current_level,
                    services_available=[],
                )
            )
            transitions.append(
                AuthTransition(
                    from_state=prev_state,
                    to_state=state_name,
                    action=f'session_change({current_session:#x})',
                    sid=SID_DIAGNOSTIC_SESSION,
                    sub_function=step.sub_function,
                    timing_constraint_ms=step.timing_constraint_ms,
                    mandatory=True,
                )
            )

        elif step.action == 'security_access_seed':
            pass  # Part of the two-step SA flow

        elif step.action == 'security_access_key':
            level = step.sub_function
            if level is not None:
                current_level = (level - 1) if level % 2 == 0 else level
            state_name = f'unlocked_level_{current_level}'
            states.append(
                AuthState(
                    name=state_name,
                    session=current_session,
                    security_level=current_level,
                    services_available=[],
                )
            )
            transitions.append(
                AuthTransition(
                    from_state=prev_state,
                    to_state=state_name,
                    action=f'security_access(level={current_level})',
                    sid=SID_SECURITY_ACCESS,
                    sub_function=step.sub_function,
                    timing_constraint_ms=step.timing_constraint_ms,
                    mandatory=True,
                )
            )

        elif step.action == 'tester_present':
            transitions.append(
                AuthTransition(
                    from_state=state_name,
                    to_state=state_name,
                    action='tester_present',
                    sid=SID_TESTER_PRESENT,
                    sub_function=step.sub_function,
                    timing_constraint_ms=step.timing_constraint_ms,
                    mandatory=tester_present_seen,
                )
            )

    # Determine which services are available in each state by checking what succeeded
    # after the ECU reached that state
    for msg in by_time:
        data = bytes(msg.data)
        if msg.arbitration_id != resp_id or len(data) < 2:
            continue
        pos = _is_positive_response(data)
        if pos is not None and states:
            states[-1].services_available.append(pos)

    # Deduplicate services_available
    for state in states:
        state.services_available = sorted(set(state.services_available))

    # Findings
    if len(states) > 3:
        findings.append(f'Multi-step authentication: {len(states)} states, {len(transitions)} transitions')
    if tester_present_seen:
        findings.append(
            f'TesterPresent keep-alive required (interval ~{tester_present_interval_ms:.0f} ms)'
            if tester_present_interval_ms
            else 'TesterPresent keep-alive observed'
        )

    # Check for timing constraints
    tight_constraints = [t for t in transitions if t.timing_constraint_ms is not None and t.timing_constraint_ms < 500]
    if tight_constraints:
        findings.append(f'{len(tight_constraints)} transition(s) have tight timing constraints (<500 ms)')

    return AuthStateMachine(
        ecu_id=ecu_id,
        response_id=resp_id,
        states=states,
        transitions=transitions,
        initial_state='default_session',
        tester_present_required=tester_present_seen,
        tester_present_interval_ms=tester_present_interval_ms,
        total_unlock_time_ms=seq.total_time_ms,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Multi-capture differential analysis (roadmap 1.3)
# ---------------------------------------------------------------------------


@dataclass
class DifferentialChange:
    """One change detected between two captures."""

    identifier: int
    change_type: str  # 'appeared', 'disappeared', 'payload_delta', 'rate_changed', 'service_delta'
    description: str
    auth_correlated: bool  # True if the change correlates with an authentication event
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DifferentialResult:
    """Result of comparing two or more captures."""

    capture_count: int
    changes: List[DifferentialChange]
    summary: str
    auth_events_detected: int


def _capture_profile(messages: Sequence[can.Message]) -> Dict[str, Any]:
    """Build a profile of a capture: IDs, rates, payloads, services."""
    ids_seen: Dict[int, List[can.Message]] = defaultdict(list)
    for m in messages:
        ids_seen[m.arbitration_id].append(m)

    profile: Dict[str, Any] = {
        'ids': set(ids_seen.keys()),
        'rates': {},
        'payloads': {},
        'services': {},
    }

    for arb_id, msgs in ids_seen.items():
        if len(msgs) >= 2:
            duration = msgs[-1].timestamp - msgs[0].timestamp
            profile['rates'][arb_id] = len(msgs) / duration if duration > 0 else 0.0
        else:
            profile['rates'][arb_id] = 0.0

        # Store unique payload set
        profile['payloads'][arb_id] = set(bytes(m.data) for m in msgs)

        # Track UDS services
        for m in msgs:
            data = bytes(m.data)
            if len(data) >= 1:
                sid = data[0]
                if sid not in profile['services'].get(arb_id, set()):
                    profile['services'].setdefault(arb_id, set()).add(sid)

    return profile


def differential_analysis(
    captures: List[Sequence[can.Message]],
    labels: Optional[List[str]] = None,
) -> DifferentialResult:
    """Compare two or more captures to find changes in IDs, payloads and services.

    Automatically detects authentication events and correlates changes with them.
    """
    if len(captures) < 2:
        return DifferentialResult(
            capture_count=len(captures),
            changes=[],
            summary='At least two captures are required',
            auth_events_detected=0,
        )

    if labels is None:
        labels = [f'capture_{i}' for i in range(len(captures))]

    profiles = [_capture_profile(cap) for cap in captures]
    changes: List[DifferentialChange] = []
    auth_events = 0

    # Detect auth events in each capture
    for cap in captures:
        for m in cap:
            data = bytes(m.data)
            if len(data) >= 2 and data[0] == SID_SECURITY_ACCESS:
                auth_events += 1
                break

    # Compare adjacent captures
    for i in range(len(profiles) - 1):
        p1 = profiles[i]
        p2 = profiles[i + 1]
        label_a = labels[i]
        label_b = labels[i + 1]

        ids_a = p1['ids']
        ids_b = p2['ids']

        # Appeared IDs
        for arb_id in sorted(ids_b - ids_a):
            is_diag = 0x700 <= arb_id <= 0x7FF
            changes.append(
                DifferentialChange(
                    identifier=arb_id,
                    change_type='appeared',
                    description=f'ID 0x{arb_id:03X} appeared in {label_b} (not in {label_a})',
                    auth_correlated=is_diag and auth_events > 0,
                )
            )

        # Disappeared IDs
        for arb_id in sorted(ids_a - ids_b):
            changes.append(
                DifferentialChange(
                    identifier=arb_id,
                    change_type='disappeared',
                    description=f'ID 0x{arb_id:03X} disappeared in {label_b} (was in {label_a})',
                    auth_correlated=False,
                )
            )

        # Payload deltas for shared IDs
        for arb_id in sorted(ids_a & ids_b):
            payloads_a = p1['payloads'].get(arb_id, set())
            payloads_b = p2['payloads'].get(arb_id, set())

            new_payloads = payloads_b - payloads_a
            if new_payloads and len(new_payloads) > len(payloads_b) * 0.3:
                services_a = p1['services'].get(arb_id, set())
                services_b = p2['services'].get(arb_id, set())
                new_services = services_b - services_a

                is_auth_related = bool(new_services & {0x67, 0x69, 0x50})  # SA/Auth positive responses
                changes.append(
                    DifferentialChange(
                        identifier=arb_id,
                        change_type='payload_delta',
                        description=(f'ID 0x{arb_id:03X}: {len(new_payloads)} new payload(s) in {label_b}'),
                        auth_correlated=is_auth_related,
                        details={
                            'new_payload_count': len(new_payloads),
                            'new_services': [f'0x{s:02X}' for s in sorted(new_services)] if new_services else [],
                        },
                    )
                )

            # Rate changes
            rate_a = p1['rates'].get(arb_id, 0.0)
            rate_b = p2['rates'].get(arb_id, 0.0)
            if rate_a > 0 and rate_b > 0:
                ratio = rate_b / rate_a
                if ratio > 2.0 or ratio < 0.5:
                    changes.append(
                        DifferentialChange(
                            identifier=arb_id,
                            change_type='rate_changed',
                            description=(f'ID 0x{arb_id:03X}: rate changed from {rate_a:.1f} Hz to {rate_b:.1f} Hz'),
                            auth_correlated=False,
                            details={'rate_before': round(rate_a, 1), 'rate_after': round(rate_b, 1)},
                        )
                    )

    auth_correlated_count = sum(1 for c in changes if c.auth_correlated)
    summary = (
        f'{len(changes)} change(s) across {len(captures)} captures; '
        f'{auth_correlated_count} correlate with authentication events'
    )

    return DifferentialResult(
        capture_count=len(captures),
        changes=changes,
        summary=summary,
        auth_events_detected=auth_events,
    )


# ---------------------------------------------------------------------------
# Gateway filter rule inference (roadmap 3.1)
# ---------------------------------------------------------------------------


@dataclass
class FilterRule:
    """An inferred gateway filter rule."""

    ecu_id: int
    service: int
    service_name: str
    session: int
    action: str  # 'allow', 'deny', 'rate_limit'
    granularity: str  # 'service', 'sub_function', 'did', 'payload_length'
    sub_function: Optional[int]
    did: Optional[int]
    max_length: Optional[int]
    max_rate_hz: Optional[float]
    evidence: str


@dataclass
class FilterRuleSet:
    """The inferred gateway filter ruleset."""

    rules: List[FilterRule]
    ecus: List[int]
    coverage: float  # Fraction of observed service/session combos explained by rules
    findings: List[str] = field(default_factory=list)


def infer_filter_rules(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> FilterRuleSet:
    """Infer the gateway's filter rules from captured UDS traffic.

    Builds allow/deny rules per service, per ECU, per session. Also detects DID-level filtering,
    payload-length filtering and rate limiting.
    """
    if not messages:
        return FilterRuleSet(rules=[], ecus=[], coverage=0.0, findings=['No messages to analyse'])

    by_time = sorted(messages, key=lambda m: m.timestamp)
    functional_ids = {0x7DF, 0x18DB33F1}
    rules: List[FilterRule] = []
    findings: List[str] = []

    # Build request -> response pairs
    ecu_requests: Dict[int, List[Tuple[bytes, bytes, float, float]]] = defaultdict(list)

    for i, msg in enumerate(by_time):
        data = bytes(msg.data)
        if len(data) < 1 or msg.arbitration_id in functional_ids or msg.is_error_frame:
            continue
        sid = data[0]
        if sid >= 0x40 or sid == 0x7F:
            continue

        resp_id = msg.arbitration_id + response_offset
        for j in range(i + 1, min(i + 100, len(by_time))):
            resp = by_time[j]
            if resp.timestamp - msg.timestamp > 5.0:
                break
            if resp.arbitration_id == resp_id and not resp.is_error_frame:
                resp_data = bytes(resp.data)
                ecu_requests[msg.arbitration_id].append((data, resp_data, msg.timestamp, resp.timestamp))
                break

    ecus = sorted(ecu_requests.keys())

    # Determine current session for each request (track session changes)
    for ecu_id, pairs in ecu_requests.items():
        session_at_time: List[Tuple[float, int]] = [(0.0, SESSION_DEFAULT)]

        for req, resp, req_ts, _resp_ts in pairs:
            if req[0] == SID_DIAGNOSTIC_SESSION and len(req) >= 2:
                pos = _is_positive_response(resp)
                if pos is not None:
                    session_at_time.append((req_ts, req[1]))

        def _session_at(ts: float, _sat: List[Tuple[float, int]] = session_at_time) -> int:
            s = SESSION_DEFAULT
            for t, sess in _sat:
                if t <= ts:
                    s = sess
            return s

        # Service-level rules
        service_results: Dict[Tuple[int, int], List[str]] = defaultdict(list)  # (service, session) -> outcomes
        did_results: Dict[Tuple[int, int, int], List[str]] = defaultdict(list)

        # Rate tracking
        service_times: Dict[int, List[float]] = defaultdict(list)

        for req, resp, req_ts, _resp_ts in pairs:
            sid = req[0]
            session = _session_at(req_ts)
            service_times[sid].append(req_ts)

            neg = _is_negative_response(resp)
            pos = _is_positive_response(resp)

            outcome = 'allowed' if pos is not None else ('denied' if neg else 'unknown')
            service_results[(sid, session)].append(outcome)

            # DID-level tracking
            did = None
            if sid in (SID_READ_DID, SID_WRITE_DID) and len(req) >= 3:
                did = (req[1] << 8) | req[2]
            elif sid == SID_ROUTINE_CONTROL and len(req) >= 4:
                did = (req[2] << 8) | req[3]

            if did is not None:
                did_results[(sid, session, did)].append(outcome)

        # Generate service-level rules
        for (sid, session), outcomes in service_results.items():
            allow_count = outcomes.count('allowed')
            deny_count = outcomes.count('denied')
            total = len(outcomes)

            if deny_count == total:
                rules.append(
                    FilterRule(
                        ecu_id=ecu_id,
                        service=sid,
                        service_name=_SID_NAMES.get(sid, f'0x{sid:02X}'),
                        session=session,
                        action='deny',
                        granularity='service',
                        sub_function=None,
                        did=None,
                        max_length=None,
                        max_rate_hz=None,
                        evidence=f'{deny_count}/{total} requests denied',
                    )
                )
            elif allow_count == total:
                rules.append(
                    FilterRule(
                        ecu_id=ecu_id,
                        service=sid,
                        service_name=_SID_NAMES.get(sid, f'0x{sid:02X}'),
                        session=session,
                        action='allow',
                        granularity='service',
                        sub_function=None,
                        did=None,
                        max_length=None,
                        max_rate_hz=None,
                        evidence=f'{allow_count}/{total} requests allowed',
                    )
                )
            elif allow_count > 0 and deny_count > 0:
                # Mixed results: check if DID-level filtering explains it
                for (dsid, dsess, did), doutcomes in did_results.items():
                    if dsid != sid or dsess != session:
                        continue
                    d_allow = doutcomes.count('allowed')
                    d_deny = doutcomes.count('denied')
                    if d_deny == len(doutcomes):
                        rules.append(
                            FilterRule(
                                ecu_id=ecu_id,
                                service=sid,
                                service_name=_SID_NAMES.get(sid, f'0x{sid:02X}'),
                                session=session,
                                action='deny',
                                granularity='did',
                                sub_function=None,
                                did=did,
                                max_length=None,
                                max_rate_hz=None,
                                evidence=f'DID 0x{did:04X}: {d_deny}/{len(doutcomes)} denied',
                            )
                        )
                    elif d_allow == len(doutcomes):
                        rules.append(
                            FilterRule(
                                ecu_id=ecu_id,
                                service=sid,
                                service_name=_SID_NAMES.get(sid, f'0x{sid:02X}'),
                                session=session,
                                action='allow',
                                granularity='did',
                                sub_function=None,
                                did=did,
                                max_length=None,
                                max_rate_hz=None,
                                evidence=f'DID 0x{did:04X}: {d_allow}/{len(doutcomes)} allowed',
                            )
                        )

        # Rate limiting detection
        for sid, times in service_times.items():
            if len(times) >= 5:
                intervals = [times[i + 1] - times[i] for i in range(len(times) - 1)]
                min_interval = min(intervals)
                if min_interval > 0:
                    max_rate = 1.0 / min_interval
                    # Check if there is evidence of rate limiting (NRC 0x37 or increasing delays)
                    nrc_37_count = sum(
                        1
                        for req, resp, _, _ in pairs
                        if req[0] == sid
                        and _is_negative_response(resp) is not None
                        and _is_negative_response(resp)[1] == NRC_REQUIRED_TIME_DELAY
                    )
                    if nrc_37_count > 0:
                        rules.append(
                            FilterRule(
                                ecu_id=ecu_id,
                                service=sid,
                                service_name=_SID_NAMES.get(sid, f'0x{sid:02X}'),
                                session=SESSION_DEFAULT,
                                action='rate_limit',
                                granularity='service',
                                sub_function=None,
                                did=None,
                                max_length=None,
                                max_rate_hz=round(max_rate, 1),
                                evidence=f'NRC 0x37 (requiredTimeDelayNotExpired) seen {nrc_37_count} times',
                            )
                        )

    # Coverage: how many observed combos are explained by a rule
    total_combos = 0
    explained = 0
    for ecu_id, pairs in ecu_requests.items():
        sids_seen = set()
        for req, _, _, _ in pairs:
            sids_seen.add(req[0])
        total_combos += len(sids_seen)
        for sid in sids_seen:
            if any(r.ecu_id == ecu_id and r.service == sid for r in rules):
                explained += 1

    coverage = explained / total_combos if total_combos > 0 else 0.0
    findings.append(f'{len(rules)} filter rules inferred across {len(ecus)} ECU(s)')
    findings.append(f'Rule coverage: {coverage:.0%} of observed service/ECU combinations')

    return FilterRuleSet(
        rules=rules,
        ecus=ecus,
        coverage=round(coverage, 2),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Attack surface report (roadmap 1.7)
# ---------------------------------------------------------------------------


@dataclass
class AttackSurfaceEntry:
    """One finding in the attack surface report."""

    ecu_id: int
    category: str  # 'auth_weakness', 'bypass', 'seed_quality', 'timing', 'nrc_fingerprint'
    finding: str
    severity: str  # 'critical', 'high', 'medium', 'low', 'info'
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AttackSurfaceReport:
    """Prioritised attack surface report consolidating all security analyses."""

    entries: List[AttackSurfaceEntry]
    ecu_scores: Dict[int, float]  # ecu_id -> severity score (0-10)
    overall_score: float
    summary: List[str]
    ecu_count: int


def attack_surface_report(
    messages: Sequence[can.Message],
    response_offset: int = 8,
    seed_key_pairs: Optional[Dict[int, List[Tuple[bytes, bytes]]]] = None,
) -> AttackSurfaceReport:
    """Combine all gateway analyses into a prioritised attack surface report.

    Runs gateway_report, fingerprint_ecu, analyse_auth_timing and assess_seed_quality and
    consolidates the findings into a single report with severity scores.
    """
    report = gateway_report(messages, response_offset, seed_key_pairs)
    entries: List[AttackSurfaceEntry] = []
    ecu_scores: Dict[int, float] = defaultdict(float)
    summary: List[str] = []

    severity_weights = {
        'critical': 10.0,
        'finding': 7.0,
        'high': 7.0,
        'warning': 4.0,
        'medium': 4.0,
        'low': 2.0,
        'info': 0.5,
    }

    # Bypass findings
    for f in report.bypass_findings:
        entries.append(
            AttackSurfaceEntry(
                ecu_id=0,
                category='bypass',
                finding=f.description,
                severity=f.severity,
                details={'finding_type': f.finding_type, 'evidence': f.evidence},
            )
        )

    # Auth patterns
    for p in report.auth_patterns:
        if p.auth_type == 'none':
            for ecu in report.gateway_map.ecus:
                if ecu.request_id == p.request_id:
                    entries.append(
                        AttackSurfaceEntry(
                            ecu_id=p.request_id,
                            category='auth_weakness',
                            finding=f'ECU 0x{p.request_id:03X} has no authentication',
                            severity='high',
                        )
                    )
                    ecu_scores[p.request_id] += severity_weights['high']

    # Auth timing
    timing_results = analyse_auth_timing(messages, response_offset)
    for t in timing_results:
        for finding in t.findings:
            sev = 'medium'
            if 'weak PRNG' in finding or 'trivial' in finding:
                sev = 'high'
            elif 'rate limiting' in finding:
                sev = 'low'
            entries.append(
                AttackSurfaceEntry(
                    ecu_id=t.ecu_id,
                    category='timing',
                    finding=finding,
                    severity=sev,
                    details={'total_ms': t.total_ms},
                )
            )
            ecu_scores[t.ecu_id] += severity_weights.get(sev, 1.0)

    # NRC fingerprints
    for ecu in report.gateway_map.ecus:
        fp = fingerprint_ecu(messages, ecu.request_id, response_offset)
        for hint in fp.vendor_hints:
            entries.append(
                AttackSurfaceEntry(
                    ecu_id=ecu.request_id,
                    category='nrc_fingerprint',
                    finding=hint,
                    severity='info',
                    details={'nrc_set': fp.nrc_set, 'timing': fp.timing_profile},
                )
            )
            ecu_scores[ecu.request_id] += severity_weights['info']

    # Seed quality (from correlations)
    for c in report.correlations:
        if c.finding == 'shared_key':
            for pair in c.ecu_pairs:
                for eid in pair:
                    entries.append(
                        AttackSurfaceEntry(
                            ecu_id=eid,
                            category='seed_quality',
                            finding=c.details,
                            severity='high',
                        )
                    )
                    ecu_scores[eid] += severity_weights['high']

    # Sort by severity
    sev_order = {'critical': 0, 'high': 1, 'finding': 1, 'medium': 2, 'warning': 2, 'low': 3, 'info': 4}
    entries.sort(key=lambda e: (sev_order.get(e.severity, 5), e.ecu_id))

    # Overall score
    overall = max(ecu_scores.values()) if ecu_scores else 0.0
    ecu_count = len(report.gateway_map.ecus)

    # Summary
    critical_count = sum(1 for e in entries if e.severity in ('critical', 'high', 'finding'))
    summary.append(f'{ecu_count} ECU(s) analysed, {len(entries)} finding(s)')
    if critical_count > 0:
        summary.append(f'{critical_count} critical/high severity finding(s)')
    summary.append(f'Overall risk score: {overall:.1f}/10')

    return AttackSurfaceReport(
        entries=entries,
        ecu_scores=dict(ecu_scores),
        overall_score=round(min(10.0, overall), 1),
        summary=summary,
        ecu_count=ecu_count,
    )


# ---------------------------------------------------------------------------
# Item 14: Export gateway findings to JSON/CSV
# ---------------------------------------------------------------------------


def export_report(report: GatewayReport, fmt: str = 'json') -> str:
    """Export a *GatewayReport* as structured JSON or CSV.

    *fmt*: ``'json'`` (default) or ``'csv'``.

    JSON: a dict with keys ``ecus``, ``auth_patterns``, ``bypass_findings``, ``correlations``,
    ``severity_summary``, ``overall_severity``, ``summary``.

    CSV: one row per finding, with columns ``category``, ``id``, ``finding``, ``severity``,
    ``details``.
    """
    if fmt == 'json':
        data: Dict[str, Any] = {
            'overall_severity': report.overall_severity,
            'summary': report.summary,
            'severity_summary': report.severity_summary,
            'ecus': [
                {
                    'request_id': f'0x{e.request_id:03X}',
                    'response_id': f'0x{e.response_id:03X}',
                    'auth_type': e.auth_type,
                    'services_seen': [f'0x{s:02X}' for s in e.services_seen],
                    'supported_sessions': e.supported_sessions,
                    'mean_response_ms': e.mean_response_ms,
                }
                for e in report.gateway_map.ecus
            ],
            'auth_patterns': [
                {
                    'request_id': f'0x{p.request_id:03X}',
                    'auth_type': p.auth_type,
                    'session_type': p.session_type,
                    'security_level': p.security_level,
                    'locked_services': [f'0x{s:02X}' for s in p.locked_services],
                    'unlocked_services': [f'0x{s:02X}' for s in p.unlocked_services],
                    'details': p.details,
                }
                for p in report.auth_patterns
            ],
            'bypass_findings': [
                {
                    'finding_type': f.finding_type,
                    'description': f.description,
                    'severity': f.severity,
                    'evidence': f.evidence,
                }
                for f in report.bypass_findings
            ],
            'correlations': [
                {
                    'ecu_pairs': [f'0x{a:03X}-0x{b:03X}' for a, b in c.ecu_pairs],
                    'finding': c.finding,
                    'details': c.details,
                }
                for c in report.correlations
            ],
            'access_matrix': [
                {
                    'ecu_id': f'0x{r.ecu_id:03X}',
                    'service': f'0x{r.service:02X}',
                    'service_name': r.service_name,
                    'session': r.session,
                    'auth_required': r.auth_required,
                    'status': r.status,
                }
                for r in report.access_matrix.rows
            ],
        }
        return json.dumps(data, indent=2)

    if fmt == 'csv':
        rows: List[Dict[str, str]] = []
        for ecu in report.gateway_map.ecus:
            rows.append(
                {
                    'category': 'Topology',
                    'id': f'0x{ecu.request_id:03X}',
                    'finding': ecu.auth_type,
                    'severity': 'info',
                    'details': f'{len(ecu.services_seen)} services, {ecu.mean_response_ms:.1f} ms',
                }
            )
        for p in report.auth_patterns:
            rows.append(
                {
                    'category': 'Authentication',
                    'id': f'0x{p.request_id:03X}',
                    'finding': p.auth_type,
                    'severity': 'info',
                    'details': p.details,
                }
            )
        for f in report.bypass_findings:
            rows.append(
                {
                    'category': 'Bypass',
                    'id': '',
                    'finding': f.finding_type,
                    'severity': f.severity,
                    'details': f.description,
                }
            )
        for c in report.correlations:
            pairs_text = ', '.join(f'0x{a:03X}-0x{b:03X}' for a, b in c.ecu_pairs[:5])
            rows.append(
                {
                    'category': 'Cross-ECU',
                    'id': pairs_text,
                    'finding': c.finding,
                    'severity': 'warning' if c.finding != 'independent' else 'info',
                    'details': c.details,
                }
            )
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=['category', 'id', 'finding', 'severity', 'details'])
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue()

    raise ValueError(f'Unsupported export format: {fmt!r} (use "json" or "csv")')


# ---------------------------------------------------------------------------
# 1.4  UDS service enumeration planner
# ---------------------------------------------------------------------------


_STANDARD_READ_DIDS = [0xF186, 0xF187, 0xF188, 0xF189, 0xF190, 0xF191, 0xF193, 0xF195]

_ENUMERATE_SERVICES = [
    SID_READ_DID,
    SID_WRITE_DID,
    SID_IO_CONTROL,
    SID_ROUTINE_CONTROL,
    SID_READ_MEMORY,
    SID_REQUEST_DOWNLOAD,
    SID_REQUEST_UPLOAD,
]


@dataclass
class EnumerationStep:
    """One step in a UDS service enumeration plan."""

    ecu_id: int
    description: str
    uds_request_hex: str
    expected_response: str
    prerequisites: List[str]
    session: int
    security_level: Optional[int]


@dataclass
class EnumerationPlan:
    """A minimal UDS enumeration plan covering all reachable services per ECU."""

    steps: List[EnumerationStep]
    ecu_count: int
    estimated_time_seconds: float
    findings: List[str]


def enumerate_plan(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> EnumerationPlan:
    """Generate a minimal UDS enumeration plan from captured traffic analysis.

    Uses the access matrix and probing plan to produce concrete UDS request bytes for each step.
    """
    matrix = build_access_matrix(messages, response_offset)
    probe_steps = plan_probing(matrix)
    gw = map_gateway(messages, response_offset)

    steps: List[EnumerationStep] = []
    findings: List[str] = []
    step_index = 0

    for ecu in gw.ecus:
        eid = ecu.request_id
        sessions_to_try = ecu.supported_sessions or [SESSION_DEFAULT]

        for session in sessions_to_try:
            prereqs: List[str] = []
            if session != SESSION_DEFAULT:
                step_index += 1
                session_hex = f'10 {session:02X}'
                steps.append(
                    EnumerationStep(
                        ecu_id=eid,
                        description=f'Switch to session 0x{session:02X}',
                        uds_request_hex=session_hex,
                        expected_response=f'50 {session:02X}',
                        prerequisites=[],
                        session=session,
                        security_level=None,
                    )
                )
                prereqs = [f'step_{step_index}']

            # Check if security access was needed in this session
            needs_auth = any(
                r.ecu_id == eid and r.session == session and r.auth_required == '0x27' for r in matrix.rows
            )
            if needs_auth:
                step_index += 1
                steps.append(
                    EnumerationStep(
                        ecu_id=eid,
                        description=f'Request seed (level 1) on 0x{eid:03X}',
                        uds_request_hex='27 01',
                        expected_response='67 01 <seed>',
                        prerequisites=list(prereqs),
                        session=session,
                        security_level=1,
                    )
                )
                seed_step = f'step_{step_index}'
                step_index += 1
                steps.append(
                    EnumerationStep(
                        ecu_id=eid,
                        description=f'Send key (level 2) on 0x{eid:03X}',
                        uds_request_hex='27 02 <key>',
                        expected_response='67 02',
                        prerequisites=[seed_step],
                        session=session,
                        security_level=1,
                    )
                )
                prereqs = [f'step_{step_index}']

            # Read standard DIDs
            for did in _STANDARD_READ_DIDS:
                step_index += 1
                did_hi, did_lo = (did >> 8) & 0xFF, did & 0xFF
                steps.append(
                    EnumerationStep(
                        ecu_id=eid,
                        description=f'Read DID 0x{did:04X} on 0x{eid:03X}',
                        uds_request_hex=f'22 {did_hi:02X} {did_lo:02X}',
                        expected_response=f'62 {did_hi:02X} {did_lo:02X} <data>',
                        prerequisites=list(prereqs),
                        session=session,
                        security_level=None,
                    )
                )

            # Enumerate other services that haven't been tried yet
            for sid in _ENUMERATE_SERVICES:
                already_tried = any(r.ecu_id == eid and r.service == sid and r.session == session for r in matrix.rows)
                if not already_tried:
                    step_index += 1
                    steps.append(
                        EnumerationStep(
                            ecu_id=eid,
                            description=f'Probe {_SID_NAMES.get(sid, f"0x{sid:02X}")} on 0x{eid:03X}',
                            uds_request_hex=f'{sid:02X}',
                            expected_response=f'{sid + 0x40:02X} <data> or 7F {sid:02X} <NRC>',
                            prerequisites=list(prereqs),
                            session=session,
                            security_level=None,
                        )
                    )

    # Add high-priority probing steps that the planner suggested
    for ps in probe_steps:
        if ps.priority == 'high':
            step_index += 1
            steps.append(
                EnumerationStep(
                    ecu_id=ps.ecu_id,
                    description=ps.rationale,
                    uds_request_hex=f'{ps.service:02X}',
                    expected_response=f'{ps.service + 0x40:02X} <data> or 7F {ps.service:02X} <NRC>',
                    prerequisites=[],
                    session=ps.session,
                    security_level=ps.auth_level,
                )
            )

    ecu_count = len(gw.ecus)
    estimated_time = len(steps) * 0.15  # ~150ms per request on average
    findings.append(f'{len(steps)} enumeration steps across {ecu_count} ECU(s)')
    if any(s.security_level is not None for s in steps):
        findings.append('Some steps require security access; key computation is not included')

    return EnumerationPlan(
        steps=steps,
        ecu_count=ecu_count,
        estimated_time_seconds=round(estimated_time, 1),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 1.5  ECU firmware version fingerprinting
# ---------------------------------------------------------------------------

_IDENTITY_DIDS: Dict[int, str] = {
    0xF186: 'active_session',
    0xF187: 'spare_part',
    0xF188: 'diagnosis_name',
    0xF189: 'ecu_serial',
    0xF190: 'vin',
    0xF191: 'hardware_version',
    0xF193: 'system_supplier',
    0xF195: 'software_version',
}


@dataclass
class EcuIdentity:
    """Full identity card for one ECU built from captured traffic."""

    request_id: int
    response_id: int
    nrc_fingerprint: Optional[EcuFingerprint]
    did_values: Dict[int, str]
    vendor_hints: List[str]
    hardware_version: Optional[str]
    software_version: Optional[str]
    ecu_serial: Optional[str]
    spare_part: Optional[str]
    system_supplier: Optional[str]
    findings: List[str]


def identify_ecu(
    messages: Sequence[can.Message],
    ecu_request_id: int,
    response_offset: int = 8,
) -> EcuIdentity:
    """Build a full ECU identity card from captured traffic.

    Passively scans the capture for ReadDataByIdentifier (0x22) responses to standard identity DIDs
    and correlates with the NRC fingerprint.
    """
    resp_id = ecu_request_id + response_offset
    did_values: Dict[int, str] = {}
    by_time = sorted(messages, key=lambda m: m.timestamp)

    # Scan for positive 0x62 responses (ReadDataByIdentifier)
    for msg in by_time:
        if msg.arbitration_id != resp_id or msg.is_error_frame:
            continue
        data = bytes(msg.data)
        if len(data) >= 4 and data[0] == 0x62:
            did = (data[1] << 8) | data[2]
            if did in _IDENTITY_DIDS:
                value_bytes = data[3:]
                # Try ASCII decode for human-readable values
                try:
                    text = value_bytes.decode('ascii', errors='replace').rstrip('\x00').strip()
                    if all(32 <= c < 127 for c in value_bytes if c != 0):
                        did_values[did] = text
                    else:
                        did_values[did] = value_bytes.hex(' ').upper()
                except Exception:
                    did_values[did] = value_bytes.hex(' ').upper()

    nrc_fp = fingerprint_ecu(messages, ecu_request_id, response_offset)

    # Extract named fields
    hardware_version = did_values.get(0xF191)
    software_version = did_values.get(0xF195)
    ecu_serial = did_values.get(0xF189)
    spare_part = did_values.get(0xF187)
    system_supplier = did_values.get(0xF193)

    vendor_hints = list(nrc_fp.vendor_hints)
    findings: List[str] = []

    if did_values:
        findings.append(f'Read {len(did_values)} identity DID(s) from captured traffic')
    else:
        findings.append('No identity DID responses found in the capture; try reading DIDs F186-F195')

    if system_supplier:
        vendor_hints.append(f'System supplier: {system_supplier}')
    if software_version and hardware_version:
        findings.append(f'HW: {hardware_version}, SW: {software_version}')

    return EcuIdentity(
        request_id=ecu_request_id,
        response_id=resp_id,
        nrc_fingerprint=nrc_fp,
        did_values=did_values,
        vendor_hints=vendor_hints,
        hardware_version=hardware_version,
        software_version=software_version,
        ecu_serial=ecu_serial,
        spare_part=spare_part,
        system_supplier=system_supplier,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 1.8  Session / security-level brute-force planner
# ---------------------------------------------------------------------------


@dataclass
class BruteForceStep:
    """One attempt in a brute-force plan."""

    ecu_id: int
    session: int
    security_level: int
    description: str
    uds_request_hex: str


@dataclass
class BruteForcePlan:
    """A plan to systematically try OEM sessions and non-standard security levels."""

    steps: List[BruteForceStep]
    ecu_count: int
    oem_sessions: List[int]
    non_standard_levels: List[int]
    estimated_attempts: int
    findings: List[str]


def brute_plan(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> BruteForcePlan:
    """Generate a brute-force plan for OEM-specific sessions and non-standard security levels.

    Analyses the access matrix to find known sessions and levels, then plans to probe the unknowns.
    OEM sessions are 0x40-0x5F; standard security-access levels are odd 0x01-0x41.
    """
    matrix = build_access_matrix(messages, response_offset)
    gw = map_gateway(messages, response_offset)

    known_sessions: Set[int] = set(matrix.sessions)
    known_levels: Set[int] = set()
    for row in matrix.rows:
        if row.security_level is not None:
            known_levels.add(row.security_level)

    # OEM session range: 0x40 to 0x5F
    oem_sessions = [s for s in range(0x40, 0x60) if s not in known_sessions]
    # Non-standard security levels: odd numbers 1-63 that weren't observed
    standard_levels = {1, 3, 5, 7, 9, 11}
    non_standard_levels = [lev for lev in range(1, 64, 2) if lev not in known_levels and lev not in standard_levels]

    steps: List[BruteForceStep] = []
    findings: List[str] = []

    for ecu in gw.ecus:
        eid = ecu.request_id

        # Try each OEM session
        for session in oem_sessions:
            steps.append(
                BruteForceStep(
                    ecu_id=eid,
                    session=session,
                    security_level=0,
                    description=f'Try OEM session 0x{session:02X} on 0x{eid:03X}',
                    uds_request_hex=f'10 {session:02X}',
                )
            )

        # For each known session, try non-standard security levels
        for session in sorted(known_sessions | {SESSION_EXTENDED, SESSION_PROGRAMMING}):
            for level in non_standard_levels:
                steps.append(
                    BruteForceStep(
                        ecu_id=eid,
                        session=session,
                        security_level=level,
                        description=(f'Try security level 0x{level:02X} in session 0x{session:02X} on 0x{eid:03X}'),
                        uds_request_hex=f'27 {level:02X}',
                    )
                )

    ecu_count = len(gw.ecus)
    findings.append(f'{len(oem_sessions)} OEM session(s) to probe, {len(non_standard_levels)} non-standard level(s)')
    findings.append(f'{len(steps)} total attempts across {ecu_count} ECU(s)')
    if known_sessions:
        findings.append(f'Known sessions: {", ".join(f"0x{s:02X}" for s in sorted(known_sessions))}')

    return BruteForcePlan(
        steps=steps,
        ecu_count=ecu_count,
        oem_sessions=oem_sessions,
        non_standard_levels=non_standard_levels,
        estimated_attempts=len(steps),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 1.9  Timing side-channel analysis
# ---------------------------------------------------------------------------


@dataclass
class TimingSideChannelResult:
    """Result of timing side-channel analysis on key verification."""

    ecu_id: int
    constant_time: bool
    max_timing_delta_ms: float
    byte_correlations: Dict[int, float]
    sample_count: int
    findings: List[str]


def detect_timing_side_channel(
    messages: Sequence[can.Message],
    ecu_request_id: int,
    response_offset: int = 8,
) -> Optional[TimingSideChannelResult]:
    """Detect timing side channels in key verification from captured auth attempts.

    Correlates individual key bytes with the verification response time to find non-constant-time
    comparisons. Requires many captured seed/key attempts (at least 10).
    """
    resp_id = ecu_request_id + response_offset
    by_time = sorted(messages, key=lambda m: m.timestamp)

    # Extract (key_bytes, verification_time_ms) pairs
    key_timing_pairs: List[Tuple[bytes, float]] = []
    last_key_send: Optional[Tuple[bytes, float]] = None

    for msg in by_time:
        data = bytes(msg.data)
        if msg.is_error_frame or len(data) < 2:
            continue

        if msg.arbitration_id == ecu_request_id and data[0] == SID_SECURITY_ACCESS and data[1] % 2 == 0:
            # Key send (even sub-function)
            last_key_send = (data[2:], msg.timestamp)

        elif msg.arbitration_id == resp_id and last_key_send is not None:
            # Response to key
            is_accepted = data[0] == 0x67
            neg = _is_negative_response(data)
            is_rejected = neg is not None and neg[0] == SID_SECURITY_ACCESS
            if is_accepted or is_rejected:
                key_bytes, send_ts = last_key_send
                delta_ms = (msg.timestamp - send_ts) * 1000.0
                if len(key_bytes) >= 1 and 0.0 < delta_ms < 10000.0:
                    key_timing_pairs.append((key_bytes, delta_ms))
                last_key_send = None

    if len(key_timing_pairs) < 10:
        return None

    # Compute per-byte correlation with timing
    key_len = min(len(kp[0]) for kp in key_timing_pairs)
    if key_len == 0:
        return None

    timings = [t for _, t in key_timing_pairs]
    mean_time = statistics.mean(timings)
    std_time = statistics.stdev(timings) if len(timings) >= 2 else 0.001

    byte_correlations: Dict[int, float] = {}
    for byte_idx in range(key_len):
        byte_vals = [float(kp[0][byte_idx]) for kp in key_timing_pairs]
        mean_byte = statistics.mean(byte_vals)
        std_byte = statistics.stdev(byte_vals) if len(byte_vals) >= 2 else 0.001
        if std_byte < 0.001 or std_time < 0.001:
            byte_correlations[byte_idx] = 0.0
            continue
        n = len(key_timing_pairs)
        cov = sum((byte_vals[i] - mean_byte) * (timings[i] - mean_time) for i in range(n)) / n
        corr = cov / (std_byte * std_time)
        byte_correlations[byte_idx] = round(corr, 4)

    max_abs_corr = max(abs(c) for c in byte_correlations.values()) if byte_correlations else 0.0
    max_delta = max(timings) - min(timings)

    # A correlation > 0.3 or a timing delta > 5ms between fastest and slowest suggests non-constant-time
    constant_time = max_abs_corr < 0.3 and max_delta < 5.0

    findings: List[str] = []
    if not constant_time:
        worst_byte = max(byte_correlations, key=lambda k: abs(byte_correlations[k]))
        findings.append(
            f'Non-constant-time key verification detected: byte {worst_byte} correlates '
            f'with timing (r={byte_correlations[worst_byte]:.3f})'
        )
        findings.append(f'Timing delta: {max_delta:.1f} ms across {len(key_timing_pairs)} attempts')
    else:
        findings.append(
            f'Key verification appears constant-time (max |r|={max_abs_corr:.3f}, '
            f'delta={max_delta:.1f} ms, {len(key_timing_pairs)} samples)'
        )

    return TimingSideChannelResult(
        ecu_id=ecu_request_id,
        constant_time=constant_time,
        max_timing_delta_ms=round(max_delta, 2),
        byte_correlations=byte_correlations,
        sample_count=len(key_timing_pairs),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 1.10  Diagnostic routing table inference
# ---------------------------------------------------------------------------


@dataclass
class RoutingEntry:
    """One entry in the inferred routing table."""

    request_id: int
    forwarded: bool
    rewritten_id: Optional[int]
    latency_ms: float
    direction: str


@dataclass
class RoutingTable:
    """The inferred gateway routing table from two-segment captures."""

    entries: List[RoutingEntry]
    forwarded_ids: List[int]
    blocked_ids: List[int]
    rewritten_ids: List[Tuple[int, int]]
    findings: List[str]


def infer_routing_table(
    external_messages: Sequence[can.Message],
    internal_messages: Sequence[can.Message],
) -> RoutingTable:
    """Infer the gateway routing table from captures on two bus segments.

    Compares messages on the external bus with the internal bus to determine which request IDs
    are forwarded, blocked, or rewritten (ID changed in transit).
    """
    # Build a time-indexed lookup of internal messages
    internal_by_time = sorted(internal_messages, key=lambda m: m.timestamp)
    external_by_time = sorted(external_messages, key=lambda m: m.timestamp)

    # Collect all unique UDS request IDs on the external bus
    external_ids: Set[int] = set()
    for msg in external_by_time:
        data = bytes(msg.data)
        if len(data) >= 1 and data[0] < 0x40 and data[0] != 0x7F and not msg.is_error_frame:
            external_ids.add(msg.arbitration_id)

    internal_ids: Set[int] = set()
    for msg in internal_by_time:
        data = bytes(msg.data)
        if len(data) >= 1 and data[0] < 0x40 and data[0] != 0x7F and not msg.is_error_frame:
            internal_ids.add(msg.arbitration_id)

    # For each external request, check if a matching message appears on the internal bus
    # within a short time window (gateway forwarding latency is typically < 10ms)
    entries: List[RoutingEntry] = []
    forwarded_ids: List[int] = []
    blocked_ids: List[int] = []
    rewritten_ids: List[Tuple[int, int]] = []

    for ext_id in sorted(external_ids):
        # Find external messages with this ID
        ext_msgs = [m for m in external_by_time if m.arbitration_id == ext_id and not m.is_error_frame]
        if not ext_msgs:
            continue

        # Check for exact ID forwarding
        matched = False
        latencies: List[float] = []
        for em in ext_msgs[:50]:  # Sample up to 50 messages
            ext_data = bytes(em.data)
            for im in internal_by_time:
                dt = im.timestamp - em.timestamp
                if dt < -0.001:
                    continue
                if dt > 0.050:
                    break
                if im.arbitration_id == ext_id and bytes(im.data) == ext_data:
                    matched = True
                    latencies.append(dt * 1000.0)
                    break

        if matched:
            mean_lat = statistics.mean(latencies) if latencies else 0.0
            entries.append(
                RoutingEntry(
                    request_id=ext_id,
                    forwarded=True,
                    rewritten_id=None,
                    latency_ms=round(mean_lat, 2),
                    direction='external_to_internal',
                )
            )
            forwarded_ids.append(ext_id)
            continue

        # Check for ID rewriting: same payload but different ID on the internal bus
        rewrite_target: Optional[int] = None
        rewrite_latencies: List[float] = []
        for em in ext_msgs[:20]:
            ext_data = bytes(em.data)
            for im in internal_by_time:
                dt = im.timestamp - em.timestamp
                if dt < -0.001:
                    continue
                if dt > 0.050:
                    break
                if im.arbitration_id != ext_id and bytes(im.data) == ext_data:
                    rewrite_target = im.arbitration_id
                    rewrite_latencies.append(dt * 1000.0)
                    break

        if rewrite_target is not None:
            mean_lat = statistics.mean(rewrite_latencies) if rewrite_latencies else 0.0
            entries.append(
                RoutingEntry(
                    request_id=ext_id,
                    forwarded=True,
                    rewritten_id=rewrite_target,
                    latency_ms=round(mean_lat, 2),
                    direction='external_to_internal',
                )
            )
            forwarded_ids.append(ext_id)
            rewritten_ids.append((ext_id, rewrite_target))
        else:
            entries.append(
                RoutingEntry(
                    request_id=ext_id,
                    forwarded=False,
                    rewritten_id=None,
                    latency_ms=0.0,
                    direction='blocked',
                )
            )
            blocked_ids.append(ext_id)

    # Also check internal-to-external direction for response forwarding
    internal_only = internal_ids - external_ids
    for int_id in sorted(internal_only):
        int_msgs = [m for m in internal_by_time if m.arbitration_id == int_id and not m.is_error_frame]
        if not int_msgs:
            continue
        found_on_ext = False
        for im in int_msgs[:20]:
            int_data = bytes(im.data)
            for em in external_by_time:
                dt = em.timestamp - im.timestamp
                if dt < -0.001:
                    continue
                if dt > 0.050:
                    break
                if em.arbitration_id == int_id and bytes(em.data) == int_data:
                    found_on_ext = True
                    break
            if found_on_ext:
                break
        if not found_on_ext:
            entries.append(
                RoutingEntry(
                    request_id=int_id,
                    forwarded=False,
                    rewritten_id=None,
                    latency_ms=0.0,
                    direction='internal_only',
                )
            )

    findings: List[str] = []
    findings.append(f'{len(forwarded_ids)} ID(s) forwarded, {len(blocked_ids)} blocked, {len(rewritten_ids)} rewritten')
    if rewritten_ids:
        for orig, rewr in rewritten_ids:
            findings.append(f'ID rewriting: 0x{orig:03X} -> 0x{rewr:03X}')

    return RoutingTable(
        entries=entries,
        forwarded_ids=sorted(forwarded_ids),
        blocked_ids=sorted(blocked_ids),
        rewritten_ids=rewritten_ids,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 3.5  Gateway message rewriting detection
# ---------------------------------------------------------------------------


@dataclass
class RewritingFinding:
    """One rewriting transformation detected through the gateway."""

    external_id: int
    internal_id: int
    rewriting_type: str
    description: str
    evidence: str


@dataclass
class RewritingDetection:
    """Results of gateway rewriting detection from two-interface captures."""

    findings: List[RewritingFinding]
    matched_pairs: int
    unmatched_external: List[int]
    findings_summary: List[str]


def detect_rewriting(
    external_messages: Sequence[can.Message],
    internal_messages: Sequence[can.Message],
) -> RewritingDetection:
    """Detect gateway message rewriting from two-interface captures.

    Compares request payloads on the external bus with forwarded payloads on the internal bus
    and reports ID mapping, payload truncation, byte rewriting and length changes.
    """
    external_by_time = sorted(external_messages, key=lambda m: m.timestamp)
    internal_by_time = sorted(internal_messages, key=lambda m: m.timestamp)

    findings: List[RewritingFinding] = []
    matched_pairs = 0
    unmatched_external: Set[int] = set()

    # Build external request events
    external_requests: List[Tuple[float, int, bytes]] = []
    for msg in external_by_time:
        data = bytes(msg.data)
        if len(data) >= 1 and data[0] < 0x40 and data[0] != 0x7F and not msg.is_error_frame:
            external_requests.append((msg.timestamp, msg.arbitration_id, data))

    for ext_ts, ext_id, ext_data in external_requests:
        # Find the best-matching internal message within the forwarding window
        best_match: Optional[Tuple[int, bytes, float]] = None
        best_score = -1

        for im in internal_by_time:
            dt = im.timestamp - ext_ts
            if dt < -0.002:
                continue
            if dt > 0.100:
                break
            int_data = bytes(im.data)
            if len(int_data) < 1:
                continue

            # Score: how many leading payload bytes match
            match_len = 0
            for k in range(min(len(ext_data), len(int_data))):
                if ext_data[k] == int_data[k]:
                    match_len += 1
                else:
                    break

            if match_len >= 1 and match_len > best_score:
                best_score = match_len
                best_match = (im.arbitration_id, int_data, dt * 1000.0)

        if best_match is None:
            unmatched_external.add(ext_id)
            continue

        int_id, int_data, latency = best_match
        matched_pairs += 1

        # ID mapping
        if int_id != ext_id:
            findings.append(
                RewritingFinding(
                    external_id=ext_id,
                    internal_id=int_id,
                    rewriting_type='id_mapping',
                    description=f'Gateway maps 0x{ext_id:03X} to 0x{int_id:03X}',
                    evidence=f'Latency {latency:.1f} ms',
                )
            )

        # Length change
        if len(int_data) != len(ext_data):
            rtype = 'payload_truncation' if len(int_data) < len(ext_data) else 'length_change'
            findings.append(
                RewritingFinding(
                    external_id=ext_id,
                    internal_id=int_id,
                    rewriting_type=rtype,
                    description=(f'Payload length changed from {len(ext_data)} to {len(int_data)} bytes'),
                    evidence=(f'External: {ext_data.hex(" ").upper()}, internal: {int_data.hex(" ").upper()}'),
                )
            )

        # Byte rewriting (same length, different content beyond the SID)
        if len(int_data) == len(ext_data) and int_data != ext_data:
            diff_indices = [i for i in range(len(ext_data)) if ext_data[i] != int_data[i]]
            if diff_indices:
                findings.append(
                    RewritingFinding(
                        external_id=ext_id,
                        internal_id=int_id,
                        rewriting_type='byte_rewriting',
                        description=f'{len(diff_indices)} byte(s) rewritten at offset(s) {diff_indices}',
                        evidence=(f'External: {ext_data.hex(" ").upper()}, internal: {int_data.hex(" ").upper()}'),
                    )
                )

    findings_summary: List[str] = []
    findings_summary.append(f'{matched_pairs} message pair(s) matched across the gateway')
    if unmatched_external:
        findings_summary.append(f'{len(unmatched_external)} external ID(s) not seen on the internal bus')
    id_maps = [f for f in findings if f.rewriting_type == 'id_mapping']
    if id_maps:
        findings_summary.append(f'{len(id_maps)} ID mapping(s) detected')
    byte_rews = [f for f in findings if f.rewriting_type == 'byte_rewriting']
    if byte_rews:
        findings_summary.append(f'{len(byte_rews)} byte rewriting(s) detected')

    return RewritingDetection(
        findings=findings,
        matched_pairs=matched_pairs,
        unmatched_external=sorted(unmatched_external),
        findings_summary=findings_summary,
    )


# ---------------------------------------------------------------------------
# 3.6  Multi-session unlock chain discovery
# ---------------------------------------------------------------------------


@dataclass
class UnlockChainStep:
    """One step in a multi-session unlock chain."""

    session: int
    security_level: Optional[int]
    service: int
    description: str


@dataclass
class UnlockChain:
    """A multi-session unlock chain for reaching a locked service."""

    ecu_id: int
    steps: List[UnlockChainStep]
    target_service: int
    target_description: str
    findings: List[str]


def discover_unlock_chains(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> List[UnlockChain]:
    """Discover multi-session unlock chains from captured traffic.

    Identifies ECUs where reaching a locked service required changing sessions and security levels
    in a specific order, potentially spanning multiple sessions.
    """
    gw = map_gateway(messages, response_offset)
    chains: List[UnlockChain] = []

    for ecu in gw.ecus:
        eid = ecu.request_id
        sm = recover_auth_state_machine(messages, eid, response_offset)
        if sm is None:
            continue

        # A chain is interesting when it crosses more than one session
        sessions_in_path: Set[int] = set()
        for trans in sm.transitions:
            for state in sm.states:
                if state.name == trans.to_state:
                    sessions_in_path.add(state.session)

        if len(sessions_in_path) < 2:
            continue

        # Build the chain of steps
        steps: List[UnlockChainStep] = []
        for trans in sm.transitions:
            to_state = next((s for s in sm.states if s.name == trans.to_state), None)
            if to_state is None:
                continue

            steps.append(
                UnlockChainStep(
                    session=to_state.session,
                    security_level=to_state.security_level,
                    service=trans.sid,
                    description=trans.action,
                )
            )

        # Determine the target: the last state's available services
        final_state = sm.states[-1] if sm.states else None
        if final_state and final_state.services_available:
            target_svc = final_state.services_available[0]
            target_desc = _SID_NAMES.get(target_svc, f'0x{target_svc:02X}')
        else:
            target_svc = 0
            target_desc = 'authenticated state'

        chain_findings: List[str] = []
        chain_findings.append(f'Multi-session chain: {len(steps)} steps across {len(sessions_in_path)} session(s)')
        if sm.tester_present_required:
            chain_findings.append(f'TesterPresent required (interval ~{sm.tester_present_interval_ms} ms)')

        chains.append(
            UnlockChain(
                ecu_id=eid,
                steps=steps,
                target_service=target_svc,
                target_description=target_desc,
                findings=chain_findings,
            )
        )

    return chains


# ---------------------------------------------------------------------------
# 3.7  Non-standard ID range scanning
# ---------------------------------------------------------------------------


@dataclass
class ScanRange:
    """One contiguous range of CAN IDs to probe for diagnostic responders."""

    start_id: int
    end_id: int
    extended: bool
    rationale: str


@dataclass
class ScanPlan:
    """A plan to scan non-standard diagnostic ID ranges."""

    ranges: List[ScanRange]
    known_responders: List[int]
    estimated_probes: int
    findings: List[str]


# Standard diagnostic ID range per ISO 14229
_STANDARD_DIAG_START = 0x700
_STANDARD_DIAG_END = 0x7FF


def scan_plan_non_standard(
    messages: Sequence[can.Message],
    response_offset: int = 8,
) -> ScanPlan:
    """Generate a plan to scan non-standard diagnostic ID ranges.

    Identifies known responders, determines which ID ranges were active in the capture, and plans
    to probe gaps where hidden diagnostic endpoints may exist.
    """
    gw = map_gateway(messages, response_offset)
    known_responders = sorted(gw.diagnostic_ids)

    # Collect all arbitration IDs seen in the capture
    all_seen_ids: Set[int] = set()
    extended_seen = False
    for msg in messages:
        all_seen_ids.add(msg.arbitration_id)
        if msg.is_extended_id:
            extended_seen = True

    ranges: List[ScanRange] = []
    findings: List[str] = []

    # Non-standard ranges that OEMs commonly use
    oem_ranges = [
        (0x600, 0x6FF, 'OEM range 0x600-0x6FF (common in VAG/BMW)'),
        (0x500, 0x5FF, 'OEM range 0x500-0x5FF (common in PSA/Stellantis)'),
        (0x400, 0x4FF, 'OEM range 0x400-0x4FF (some Asian OEMs)'),
        (0x1A0, 0x1FF, 'Low ID range (some ECUs respond here)'),
    ]

    for start, end, rationale in oem_ranges:
        responders_in_range = [r for r in known_responders if start <= r <= end]
        ids_in_range = [i for i in all_seen_ids if start <= i <= end]
        if ids_in_range and not responders_in_range:
            ranges.append(ScanRange(start_id=start, end_id=end, extended=False, rationale=rationale))

    # Scan the gap below the standard range if we see traffic there
    below_standard = [i for i in all_seen_ids if 0x100 <= i < _STANDARD_DIAG_START]
    if below_standard and not any(r.start_id < _STANDARD_DIAG_START for r in ranges):
        range_min = (min(below_standard) // 0x100) * 0x100
        range_max = (max(below_standard) // 0x100 + 1) * 0x100 - 1
        ranges.append(
            ScanRange(
                start_id=range_min,
                end_id=range_max,
                extended=False,
                rationale=f'Traffic observed in 0x{range_min:03X}-0x{range_max:03X} but no diagnostic responders',
            )
        )

    # If extended IDs were seen, add a scan of the 29-bit range
    if extended_seen:
        extended_diag_seen = [i for i in all_seen_ids if i > 0x7FF]
        if extended_diag_seen:
            # Cluster extended IDs into ranges
            ext_sorted = sorted(extended_diag_seen)
            cluster_start = ext_sorted[0]
            cluster_end = ext_sorted[0]
            for eid in ext_sorted[1:]:
                if eid - cluster_end > 0x100:
                    ranges.append(
                        ScanRange(
                            start_id=cluster_start,
                            end_id=cluster_end + 0x10,
                            extended=True,
                            rationale=f'Extended ID traffic near 0x{cluster_start:08X}',
                        )
                    )
                    cluster_start = eid
                cluster_end = eid
            ranges.append(
                ScanRange(
                    start_id=cluster_start,
                    end_id=cluster_end + 0x10,
                    extended=True,
                    rationale=f'Extended ID traffic near 0x{cluster_start:08X}',
                )
            )

    # Always suggest probing the standard range for completeness
    standard_responders = [r for r in known_responders if _STANDARD_DIAG_START <= r <= _STANDARD_DIAG_END]
    if len(standard_responders) < 8:
        ranges.append(
            ScanRange(
                start_id=_STANDARD_DIAG_START,
                end_id=_STANDARD_DIAG_END,
                extended=False,
                rationale=(f'Standard diagnostic range: {len(standard_responders)} responder(s) found, scan for more'),
            )
        )

    estimated_probes = sum(r.end_id - r.start_id + 1 for r in ranges)
    findings.append(f'{len(known_responders)} known diagnostic responder(s)')
    findings.append(f'{len(ranges)} range(s) to scan, ~{estimated_probes} probe(s)')
    if any(not r.extended for r in ranges):
        findings.append('Use TesterPresent (3E 00) as the probe frame for each candidate ID')

    return ScanPlan(
        ranges=ranges,
        known_responders=known_responders,
        estimated_probes=estimated_probes,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 3.9  DoIP gateway analysis
# ---------------------------------------------------------------------------

# DoIP header type codes (ISO 13400-2)
_DOIP_VEHICLE_IDENTIFICATION_REQ = 0x0001
_DOIP_VEHICLE_IDENTIFICATION_RESP = 0x0004
_DOIP_ROUTING_ACTIVATION_REQ = 0x0005
_DOIP_ROUTING_ACTIVATION_RESP = 0x0006
_DOIP_DIAGNOSTIC_MESSAGE = 0x8001
_DOIP_DIAGNOSTIC_ACK = 0x8002
_DOIP_DIAGNOSTIC_NACK = 0x8003


@dataclass
class DoipGatewayAnalysis:
    """Analysis of a DoIP gateway from captured traffic."""

    activation_lines: List[Dict[str, Any]]
    vehicle_id: Optional[str]
    routing_activations: List[Dict[str, Any]]
    entity_addresses: List[int]
    access_control_findings: List[str]
    findings: List[str]


def analyse_doip_gateway(
    messages: Sequence[can.Message],
) -> DoipGatewayAnalysis:
    """Analyse DoIP gateway properties from captured traffic.

    Looks for DoIP protocol patterns in the captures: vehicle identification responses,
    routing activation exchanges, and diagnostic message routing.
    """
    activation_lines: List[Dict[str, Any]] = []
    routing_activations: List[Dict[str, Any]] = []
    entity_addresses: Set[int] = set()
    vehicle_id: Optional[str] = None
    access_findings: List[str] = []
    findings: List[str] = []

    # DoIP messages may appear as CAN frames with specific patterns, or as metadata
    # in the capture. We look for DoIP header bytes in the payload.
    for msg in messages:
        data = bytes(msg.data)
        if len(data) < 8:
            continue

        # DoIP header: version (1 byte), inverse version (1 byte), type (2 bytes), length (4 bytes)
        version = data[0]
        inv_version = data[1]
        if version ^ inv_version != 0xFF:
            continue
        if version not in (0x02, 0x03):
            continue

        payload_type = (data[2] << 8) | data[3]

        if payload_type == _DOIP_VEHICLE_IDENTIFICATION_RESP and len(data) >= 41:
            # Extract VIN (17 bytes at offset 8)
            vin_bytes = data[8:25]
            try:
                vin = vin_bytes.decode('ascii').rstrip('\x00')
                if len(vin) == 17:
                    vehicle_id = vin
            except (UnicodeDecodeError, ValueError):
                pass
            # Logical address at offset 25-26
            if len(data) >= 27:
                logical_addr = (data[25] << 8) | data[26]
                entity_addresses.add(logical_addr)
                activation_lines.append(
                    {
                        'type': 'vehicle_identification',
                        'logical_address': f'0x{logical_addr:04X}',
                        'vin': vehicle_id,
                        'timestamp': msg.timestamp,
                    }
                )

        elif payload_type == _DOIP_ROUTING_ACTIVATION_REQ and len(data) >= 15:
            source_addr = (data[8] << 8) | data[9]
            activation_type = data[10]
            routing_activations.append(
                {
                    'type': 'request',
                    'source_address': f'0x{source_addr:04X}',
                    'activation_type': activation_type,
                    'timestamp': msg.timestamp,
                }
            )

        elif payload_type == _DOIP_ROUTING_ACTIVATION_RESP and len(data) >= 17:
            tester_addr = (data[8] << 8) | data[9]
            entity_addr = (data[10] << 8) | data[11]
            response_code = data[12]
            entity_addresses.add(entity_addr)
            routing_activations.append(
                {
                    'type': 'response',
                    'tester_address': f'0x{tester_addr:04X}',
                    'entity_address': f'0x{entity_addr:04X}',
                    'response_code': response_code,
                    'timestamp': msg.timestamp,
                }
            )
            if response_code == 0x10:
                access_findings.append(f'Routing activation successful for entity 0x{entity_addr:04X}')
            elif response_code == 0x00:
                access_findings.append(f'Routing activation denied for entity 0x{entity_addr:04X} (unknown source)')

        elif payload_type == _DOIP_DIAGNOSTIC_NACK and len(data) >= 13:
            nack_code = data[12]
            access_findings.append(f'Diagnostic NACK with code 0x{nack_code:02X}')

    if not activation_lines and not routing_activations:
        findings.append('No DoIP protocol traffic detected in the capture')
    else:
        findings.append(
            f'{len(activation_lines)} identification exchange(s), {len(routing_activations)} routing activation(s)'
        )
        findings.append(f'{len(entity_addresses)} DoIP entity address(es) discovered')

    if vehicle_id:
        findings.append(f'Vehicle identified: VIN {vehicle_id}')

    return DoipGatewayAnalysis(
        activation_lines=activation_lines,
        vehicle_id=vehicle_id,
        routing_activations=routing_activations,
        entity_addresses=sorted(entity_addresses),
        access_control_findings=access_findings,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 3.10  Gateway compliance report
# ---------------------------------------------------------------------------

# WP.29 R155 / ISO 21434 requirement categories (simplified identifiers from public annexes)
_WP29_REQUIREMENTS: Dict[str, str] = {
    'R155_7.3.3': 'Authentication and access control for diagnostic interfaces',
    'R155_7.3.4': 'Protection against unauthorised modification of software',
    'R155_7.3.5': 'Secure communication of vehicle data',
    'R155_7.3.6': 'Detection of and response to attacks',
    'R155_7.3.7': 'Monitoring and incident response',
    'ISO_21434_8': 'Threat analysis and risk assessment (TARA)',
    'ISO_21434_9': 'Concept-phase cybersecurity requirements',
    'ISO_21434_10': 'Product development cybersecurity',
}


@dataclass
class ComplianceFinding:
    """One finding mapped to a compliance framework."""

    category: str
    finding: str
    severity: str
    remediation: str
    reference: str


@dataclass
class GatewayComplianceReport:
    """Gateway compliance report with WP.29 R155 / ISO 21434 classifications."""

    findings: List[ComplianceFinding]
    wp29_coverage: Dict[str, str]
    overall_compliance: str
    summary: List[str]


def gateway_compliance_report(
    messages: Sequence[can.Message],
    response_offset: int = 8,
    seed_key_pairs: Optional[Dict[int, List[Tuple[bytes, bytes]]]] = None,
) -> GatewayComplianceReport:
    """Produce a gateway compliance report with WP.29 R155 / ISO 21434 classifications.

    Runs the full gateway analysis and maps each finding to the relevant compliance requirements
    with remediation guidance.
    """
    report = gateway_report(messages, response_offset, seed_key_pairs)
    filter_rules = infer_filter_rules(messages, response_offset)
    surface = attack_surface_report(messages, response_offset, seed_key_pairs)

    comp_findings: List[ComplianceFinding] = []
    wp29_coverage: Dict[str, str] = {k: 'not_assessed' for k in _WP29_REQUIREMENTS}

    # Map bypass findings to compliance categories
    for bf in report.bypass_findings:
        if bf.finding_type in ('unfiltered_id', 'functional_addressing'):
            comp_findings.append(
                ComplianceFinding(
                    category='WP.29_R155',
                    finding=bf.description,
                    severity='high' if bf.severity == 'finding' else bf.severity,
                    remediation='Implement per-service, per-session filtering on the gateway',
                    reference='R155_7.3.3',
                )
            )
            wp29_coverage['R155_7.3.3'] = 'non_compliant'
        elif bf.finding_type == 'timing_window':
            comp_findings.append(
                ComplianceFinding(
                    category='WP.29_R155',
                    finding=bf.description,
                    severity='high',
                    remediation='Eliminate race conditions between session transitions and filter updates',
                    reference='R155_7.3.3',
                )
            )
            wp29_coverage['R155_7.3.3'] = 'non_compliant'
        elif bf.finding_type == 'session_fallback':
            comp_findings.append(
                ComplianceFinding(
                    category='WP.29_R155',
                    finding=bf.description,
                    severity='medium',
                    remediation='Ensure session fallback resets all security grants',
                    reference='R155_7.3.3',
                )
            )
            if wp29_coverage.get('R155_7.3.3') != 'non_compliant':
                wp29_coverage['R155_7.3.3'] = 'partial'

    # Auth patterns
    for p in report.auth_patterns:
        if p.auth_type == 'none':
            comp_findings.append(
                ComplianceFinding(
                    category='WP.29_R155',
                    finding=f'ECU 0x{p.request_id:03X} has no authentication on diagnostic interface',
                    severity='critical',
                    remediation='Require authentication (0x27 or 0x29) before granting access to sensitive services',
                    reference='R155_7.3.3',
                )
            )
            wp29_coverage['R155_7.3.3'] = 'non_compliant'

    # Software protection
    write_allowed_no_auth = [
        r
        for r in filter_rules.rules
        if r.service in (SID_REQUEST_DOWNLOAD, SID_REQUEST_UPLOAD, 0x3D) and r.action == 'allow'
    ]
    if write_allowed_no_auth:
        comp_findings.append(
            ComplianceFinding(
                category='WP.29_R155',
                finding=f'{len(write_allowed_no_auth)} write/download service(s) allowed without evident security',
                severity='critical',
                remediation='Gate download, upload and write-memory services behind security access',
                reference='R155_7.3.4',
            )
        )
        wp29_coverage['R155_7.3.4'] = 'non_compliant'
    else:
        wp29_coverage['R155_7.3.4'] = 'compliant'

    # Timing anomalies from attack surface report
    timing_entries = [e for e in surface.entries if e.category == 'timing']
    if timing_entries:
        for te in timing_entries:
            comp_findings.append(
                ComplianceFinding(
                    category='ISO_21434',
                    finding=te.finding,
                    severity=te.severity,
                    remediation='Review key derivation and verification for timing leaks',
                    reference='ISO_21434_10',
                )
            )
        wp29_coverage['ISO_21434_10'] = 'partial'

    # Seed quality from correlations
    for c in report.correlations:
        if c.finding in ('same_algorithm', 'shared_key'):
            comp_findings.append(
                ComplianceFinding(
                    category='ISO_21434',
                    finding=c.details,
                    severity='high',
                    remediation='Use per-ECU unique keys and diversified seed generation',
                    reference='ISO_21434_9',
                )
            )
            wp29_coverage['ISO_21434_9'] = 'non_compliant'

    # Assess overall compliance status
    if wp29_coverage.get('R155_7.3.3') == 'not_assessed' and report.auth_patterns:
        has_auth = any(p.auth_type != 'none' for p in report.auth_patterns)
        if has_auth:
            wp29_coverage['R155_7.3.3'] = 'compliant'

    statuses = set(wp29_coverage.values())
    if 'non_compliant' in statuses:
        overall = 'non_compliant'
    elif statuses == {'not_assessed'}:
        overall = 'insufficient_data'
    elif 'partial' in statuses:
        overall = 'partial'
    else:
        overall = 'compliant'

    summary: List[str] = []
    critical_count = sum(1 for f in comp_findings if f.severity == 'critical')
    high_count = sum(1 for f in comp_findings if f.severity == 'high')
    summary.append(f'{len(comp_findings)} compliance finding(s): {critical_count} critical, {high_count} high')
    summary.append(f'Overall compliance: {overall}')
    assessed = sum(1 for v in wp29_coverage.values() if v != 'not_assessed')
    summary.append(f'{assessed}/{len(wp29_coverage)} requirement area(s) assessed')

    return GatewayComplianceReport(
        findings=comp_findings,
        wp29_coverage=wp29_coverage,
        overall_compliance=overall,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Responsible-disclosure report
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {
    'critical': 0,
    'high': 1,
    'finding': 2,
    'medium': 3,
    'warning': 4,
    'low': 5,
    'info': 6,
}


@dataclass
class DisclosureItem:
    """One finding prepared for a responsible-disclosure report."""

    title: str
    severity: str
    evidence: str
    remediation: str
    reference: str


@dataclass
class DisclosureReport:
    """A PSIRT-ready bundle of the gateway findings, ordered most-severe first."""

    vehicle: str
    overall_severity: str
    overall_compliance: str
    items: List[DisclosureItem]
    topology: List[str]
    summary: List[str]


def build_disclosure(
    messages: Sequence[can.Message],
    response_offset: int = 8,
    seed_key_pairs: Optional[Dict[int, List[Tuple[bytes, bytes]]]] = None,
    vehicle: str = '',
) -> DisclosureReport:
    """Collect the gateway findings into a disclosure bundle: each bypass finding and weak-auth pattern as a titled
    item with its evidence (reproduction), remediation and WP.29 reference, ordered most-severe first."""
    report = gateway_report(messages, response_offset, seed_key_pairs)
    compliance = gateway_compliance_report(messages, response_offset, seed_key_pairs)

    items: List[DisclosureItem] = []
    evidence_by_text = {bf.description: bf.evidence for bf in report.bypass_findings}
    for finding in compliance.findings:
        items.append(
            DisclosureItem(
                title=finding.finding,
                severity=finding.severity,
                evidence=evidence_by_text.get(finding.finding, 'See the capture analysis for the supporting frames.'),
                remediation=finding.remediation,
                reference=finding.reference,
            )
        )
    items.sort(key=lambda item: _SEVERITY_RANK.get(item.severity, 99))

    topology = [
        f'ECU 0x{node.request_id:X} ↔ 0x{node.response_id:X}' + (f' ({node.name})' if getattr(node, 'name', '') else '')
        for node in report.gateway_map.ecus
    ]
    return DisclosureReport(
        vehicle=vehicle,
        overall_severity=report.overall_severity,
        overall_compliance=compliance.overall_compliance,
        items=items,
        topology=topology,
        summary=report.summary,
    )


def disclosure_report(
    messages: Sequence[can.Message],
    response_offset: int = 8,
    seed_key_pairs: Optional[Dict[int, List[Tuple[bytes, bytes]]]] = None,
    vehicle: str = '',
    researcher: str = '',
    contact: str = '',
    fmt: str = 'markdown',
) -> str:
    """A responsible-disclosure document for an OEM PSIRT, as Markdown (default) or JSON. It states only what the
    capture shows — findings, reproduction evidence, remediation and the WP.29 R155 / ISO 21434 reference — and
    names no exploit. For findings from a vehicle you are authorised to test, shared with the manufacturer."""
    bundle = build_disclosure(messages, response_offset, seed_key_pairs, vehicle)
    if fmt == 'json':
        return json.dumps(
            {
                'vehicle': bundle.vehicle,
                'researcher': researcher,
                'contact': contact,
                'overall_severity': bundle.overall_severity,
                'overall_compliance': bundle.overall_compliance,
                'findings': [
                    {
                        'title': item.title,
                        'severity': item.severity,
                        'evidence': item.evidence,
                        'remediation': item.remediation,
                        'reference': item.reference,
                    }
                    for item in bundle.items
                ],
                'topology': bundle.topology,
                'summary': bundle.summary,
            },
            indent=2,
        )
    if fmt != 'markdown':
        raise ValueError(f"Unknown format {fmt!r}; one of 'markdown', 'json'")

    lines: List[str] = []
    lines.append(f'# Security disclosure: {vehicle or "vehicle gateway"}')
    lines.append('')
    if researcher or contact:
        lines.append(f'Reported by: {researcher or "(researcher)"}' + (f' <{contact}>' if contact else ''))
        lines.append('')
    lines.append(
        f'Overall severity: **{bundle.overall_severity}** · compliance posture: **{bundle.overall_compliance}** · '
        f'{len(bundle.items)} finding(s)'
    )
    lines.append('')
    lines.append(
        'This report states only what a passive capture of an authorised test vehicle shows. It identifies gateway '
        'weaknesses and their remediation; it contains no exploit or bypass procedure.'
    )
    lines.append('')
    if bundle.summary:
        lines.append('## Summary')
        lines.extend(f'- {line}' for line in bundle.summary)
        lines.append('')
    lines.append('## Findings')
    if not bundle.items:
        lines.append('No gateway weaknesses were found in the capture.')
    for index, item in enumerate(bundle.items, 1):
        lines.append('')
        lines.append(f'### {index}. {item.title}')
        lines.append(f'- Severity: **{item.severity}**')
        lines.append(f'- Reference: {item.reference}')
        lines.append(f'- Evidence: {item.evidence}')
        lines.append(f'- Remediation: {item.remediation}')
    lines.append('')
    lines.append('## Topology observed')
    lines.extend(f'- {line}' for line in bundle.topology) if bundle.topology else lines.append('- (none inferred)')
    lines.append('')
    return '\n'.join(lines)
