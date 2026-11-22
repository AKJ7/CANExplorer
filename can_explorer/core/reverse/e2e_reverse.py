"""AUTOSAR E2E (End-to-End) reverse engineering: detect, classify and recover E2E protection parameters from
captured CAN traffic without an ARXML.

The module works on lists of ``bytes`` payloads from one identifier. All analysis is **passive** (reads captured
frames only). The main entry point is :func:`detect_e2e`, which runs the full detection pipeline: alive counter
detection, profile detection with Data ID recovery, and bypass checking.

Supported profiles: 2, 4, 5, 6, 7 and 22.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from can_explorer.core.e2e import (
    Profile2,
    Profile4,
    Profile5,
    Profile6,
    Profile7,
    Profile22,
    crc8,
    crc8h2f,
    crc16,
    crc32p4,
    crc64,
)

MIN_FRAMES = 20


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AliveCounterResult:
    """A detected alive counter in a CAN payload."""

    position: int  # Byte index (nibble: byte_index * 2 + nibble_index for the nibble within the byte)
    width: int  # Bits: 4 or 8
    modulus: int  # 16 for 4-bit, 256 for 8-bit
    nibble_high: bool  # True when the counter is the high nibble (only meaningful when width == 4)
    increment_rate: float  # Share of frame transitions where the counter incremented by exactly 1
    confidence: float  # 0 to 1

    @property
    def description(self) -> str:
        if self.width == 4:
            pos = f'byte {self.position} {"high" if self.nibble_high else "low"} nibble'
        else:
            pos = f'byte {self.position}'
        return f'{self.width}-bit counter at {pos}, mod {self.modulus}, rate {self.increment_rate:.0%}'


@dataclass(frozen=True)
class DataIdRecovery:
    """Recovered E2E Data IDs."""

    data_ids: object  # List[int] for P2/P22 (16 entries), int for P4/P5/P6/P7
    verification_rate: float  # Fraction of frames that matched

    @property
    def description(self) -> str:
        if isinstance(self.data_ids, list):
            shown = ', '.join(f'0x{d:02X}' for d in self.data_ids[:4])
            suffix = f' ... ({len(self.data_ids)} entries)' if len(self.data_ids) > 4 else ''
            return f'Data IDs: [{shown}{suffix}], {self.verification_rate:.0%} match'
        return f'Data ID: 0x{self.data_ids:X}, {self.verification_rate:.0%} match'


@dataclass(frozen=True)
class E2eBypassFinding:
    """A detected E2E bypass or enforcement weakness."""

    bypass_type: str  # 'static_crc', 'counter_freeze', 'counter_backwards', 'crc_zero_or_ff'
    description: str
    severity: str  # 'critical', 'warning', 'info'


@dataclass
class E2eProfileResult:
    """The result of detecting an E2E profile on one identifier's payloads."""

    profile: int  # 2, 4, 5, 6, 7, 22
    crc_position: int  # Byte index of the CRC (first byte for multi-byte CRCs)
    crc_width: int  # CRC width in bytes
    counter: Optional[AliveCounterResult]
    data_ids: Optional[DataIdRecovery]
    confidence: float  # 0 to 1
    confidence_label: str  # 'confirmed', 'likely', 'possible'
    findings: List[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        label = self.confidence_label
        crc = f'CRC at byte {self.crc_position} ({self.crc_width * 8} bits)'
        parts = [f'Profile {self.profile} ({label})', crc]
        if self.counter:
            parts.append(self.counter.description)
        if self.data_ids:
            parts.append(self.data_ids.description)
        return '; '.join(parts)


@dataclass
class E2eDetectionResult:
    """The full E2E detection result for one identifier."""

    profiles: List[E2eProfileResult]  # Best match first
    counter: Optional[AliveCounterResult]
    bypasses: List[E2eBypassFinding]
    findings: List[str] = field(default_factory=list)

    @property
    def best(self) -> Optional[E2eProfileResult]:
        return self.profiles[0] if self.profiles else None


# ---------------------------------------------------------------------------
# 1. Alive counter detection
# ---------------------------------------------------------------------------


def detect_alive_counter(payloads: Sequence[bytes], min_frames: int = MIN_FRAMES) -> Optional[AliveCounterResult]:
    """Find the byte or nibble that behaves as an E2E alive counter.

    Scans each byte for 8-bit counters (mod 256) and each nibble for 4-bit counters (mod 16). The best
    candidate is the one with the highest share of single-step increments and the most values seen.
    """
    if len(payloads) < min_frames:
        return None
    length = len(payloads[0])
    if length < 2:
        return None

    best: Optional[Tuple[float, AliveCounterResult]] = None

    for byte_idx in range(length):
        # Try 8-bit counter first (prefer wider counters)
        values = [p[byte_idx] for p in payloads]
        result = _score_counter(values, 256, byte_idx, 8, False)
        if result is not None:
            # Bonus for wider counters: an 8-bit counter at the same byte should beat its own low nibble
            score = result.increment_rate * 0.7 + result.confidence * 0.3 + 0.05
            if best is None or score > best[0]:
                best = (score, result)

        # Try 4-bit counters (low and high nibble)
        for high in (False, True):
            values = [((p[byte_idx] >> 4) & 0x0F if high else p[byte_idx] & 0x0F) for p in payloads]
            result = _score_counter(values, 16, byte_idx, 4, high)
            if result is not None:
                score = result.increment_rate * 0.7 + result.confidence * 0.3
                if best is None or score > best[0]:
                    best = (score, result)

    return best[1] if best is not None else None


def _score_counter(
    values: List[int], modulus: int, position: int, width: int, nibble_high: bool
) -> Optional[AliveCounterResult]:
    """Score a sequence of values as a counter with the given modulus."""
    n = len(values)
    if n < MIN_FRAMES:
        return None

    distinct = len(set(values))
    # A counter should use a reasonable share of its range
    min_distinct = max(4, modulus // 4) if modulus <= 16 else max(8, modulus // 8)
    if distinct < min_distinct:
        return None

    # Count monotonic increments (mod modulus), allowing gaps from missed frames
    inc_by_1 = 0
    monotonic = 0
    for i in range(1, n):
        step = (values[i] - values[i - 1]) % modulus
        if step == 1:
            inc_by_1 += 1
            monotonic += 1
        elif 1 < step <= modulus // 4:
            # Allow gaps (missed frames) but they count less
            monotonic += 1

    transitions = n - 1
    if transitions == 0:
        return None
    inc_rate = inc_by_1 / transitions
    mono_rate = monotonic / transitions

    # Require at least 60% of transitions to be monotonic
    if mono_rate < 0.60:
        return None

    # Confidence based on increment rate and distinct values
    value_coverage = distinct / modulus
    confidence = min(1.0, inc_rate * 0.6 + mono_rate * 0.2 + value_coverage * 0.2)

    if confidence < 0.3:
        return None

    return AliveCounterResult(
        position=position,
        width=width,
        modulus=modulus,
        nibble_high=nibble_high,
        increment_rate=round(inc_rate, 3),
        confidence=round(confidence, 3),
    )


# ---------------------------------------------------------------------------
# 2. Profile detection helpers
# ---------------------------------------------------------------------------


def _extract_counter(payload: bytes, counter: AliveCounterResult) -> int:
    """Extract the counter value from a payload given the counter result."""
    byte_val = payload[counter.position]
    if counter.width == 4:
        return (byte_val >> 4) & 0x0F if counter.nibble_high else byte_val & 0x0F
    return byte_val


def _try_profile2(payloads: List[bytes], counter: AliveCounterResult, crc_byte: int) -> Optional[E2eProfileResult]:
    """Try Profile 2: CRC-8 SAE J1850 (polynomial 0x1D), 1-byte CRC, 4-bit counter."""
    if counter.width != 4 or crc_byte >= len(payloads[0]):
        return None
    return _try_profile2_or_22(payloads, counter, crc_byte, crc8, 2)


def _try_profile22(payloads: List[bytes], counter: AliveCounterResult, crc_byte: int) -> Optional[E2eProfileResult]:
    """Try Profile 22: CRC-8H2F (polynomial 0x2F), 1-byte CRC, 4-bit counter."""
    if counter.width != 4 or crc_byte >= len(payloads[0]):
        return None
    return _try_profile2_or_22(payloads, counter, crc_byte, crc8h2f, 22)


def _try_profile2_or_22(payloads, counter, crc_byte, crc_fn, profile_num):
    """Common logic for Profile 2 and 22: brute-force the 16 Data IDs (one per counter value 0-15)."""
    # Group payloads by counter value
    by_counter: Dict[int, List[bytes]] = {}
    for p in payloads:
        cv = _extract_counter(p, counter)
        by_counter.setdefault(cv, []).append(p)

    # Need frames for at least 4 counter values to be meaningful
    if len(by_counter) < 4:
        return None

    data_ids = [0] * 16
    total_matched = 0
    total_tested = 0

    for cv in range(16):
        group = by_counter.get(cv, [])
        if not group:
            continue

        best_did = None
        best_count = 0

        for candidate_did in range(256):
            matched = 0
            for p in group:
                # Build the CRC input: data after the CRC byte, then the Data ID
                at = crc_byte
                if at > 0:
                    crc_input = p[:at] + p[at + 1 :]
                else:
                    crc_input = p[1:]
                expected = crc_fn(crc_input)
                expected = crc_fn(bytes([candidate_did]), start=expected, first_call=False)
                if p[crc_byte] == expected:
                    matched += 1

            if matched > best_count:
                best_count = matched
                best_did = candidate_did

        if best_did is not None:
            data_ids[cv] = best_did
            total_matched += best_count
            total_tested += len(group)

    if total_tested == 0:
        return None

    match_rate = total_matched / total_tested
    if match_rate < 0.5:
        return None

    confidence_label = 'confirmed' if match_rate >= 0.9 else 'likely' if match_rate >= 0.7 else 'possible'

    return E2eProfileResult(
        profile=profile_num,
        crc_position=crc_byte,
        crc_width=1,
        counter=counter,
        data_ids=DataIdRecovery(data_ids, round(match_rate, 3)),
        confidence=round(match_rate, 3),
        confidence_label=confidence_label,
        findings=[f'Profile {profile_num}: CRC at byte {crc_byte}, {match_rate:.0%} frames match'],
    )


def _solve_crc16_data_id(payload: bytes, crc_start: int, crc_width: int, implicit: bool) -> Optional[int]:
    """Solve for the 16-bit Data ID that makes the CRC-16 match for one frame.

    For Profile 5 the CRC covers bytes after the CRC field, then Data ID low byte, then Data ID high byte.
    For Profile 6 the CRC covers bytes after the CRC field, then Data ID high byte first (big endian).

    Since CRC-16 is linear in its continuation bytes, we can try all 65536 candidates on a single frame
    quickly by precomputing the CRC of the data part once.
    """
    if implicit:
        # Profile 6: CRC-16 BE at bytes 0-1, data after CRC, then Data ID big-endian
        received = int.from_bytes(payload[0:2], 'big')
        data_crc = crc16(payload[2:])
        for candidate in range(65536):
            crc_val = crc16(candidate.to_bytes(2, 'big'), start=data_crc)
            if crc_val == received:
                return candidate
    else:
        # Profile 5: CRC-16 LE at bytes 0-1, data after CRC bytes, then Data ID little-endian
        received = payload[0] | (payload[1] << 8)
        data_crc = crc16(payload[2:])
        for candidate in range(65536):
            crc_val = crc16(bytes([candidate & 0xFF]), start=data_crc)
            crc_val = crc16(bytes([(candidate >> 8) & 0xFF]), start=crc_val)
            if crc_val == received:
                return candidate
    return None


def _verify_crc16_data_id(payloads: List[bytes], data_id: int, implicit: bool) -> float:
    """Verify a candidate Data ID against all payloads and return the match rate."""
    matched = 0
    for p in payloads:
        if implicit:
            received = int.from_bytes(p[0:2], 'big')
            covered = p[2:] + data_id.to_bytes(2, 'big')
            expected = crc16(covered)
        else:
            received = p[0] | (p[1] << 8)
            data_crc = crc16(p[2:])
            expected = crc16(bytes([data_id & 0xFF]), start=data_crc)
            expected = crc16(bytes([(data_id >> 8) & 0xFF]), start=expected)
        if received == expected:
            matched += 1
    return matched / len(payloads) if payloads else 0.0


def _try_profile5(payloads: List[bytes]) -> Optional[E2eProfileResult]:
    """Try Profile 5: CRC-16 LE in bytes 0-1, 8-bit counter in byte 2, implicit 16-bit Data ID."""
    if len(payloads[0]) < 3:
        return None

    # Solve for the Data ID from the first frame, then verify on all
    candidate = _solve_crc16_data_id(payloads[0], 0, 2, implicit=False)
    if candidate is None:
        return None

    match_rate = _verify_crc16_data_id(payloads, candidate, implicit=False)
    if match_rate < 0.5:
        # Try solving from a few more frames in case the first was corrupted
        for p in payloads[1:5]:
            alt = _solve_crc16_data_id(p, 0, 2, implicit=False)
            if alt is not None and alt != candidate:
                alt_rate = _verify_crc16_data_id(payloads, alt, implicit=False)
                if alt_rate > match_rate:
                    candidate = alt
                    match_rate = alt_rate
        if match_rate < 0.5:
            return None

    # Detect the 8-bit counter
    counter_values = [p[2] for p in payloads]
    counter_result = _score_counter(counter_values, 256, 2, 8, False)

    confidence_label = 'confirmed' if match_rate >= 0.9 else 'likely' if match_rate >= 0.7 else 'possible'

    return E2eProfileResult(
        profile=5,
        crc_position=0,
        crc_width=2,
        counter=counter_result,
        data_ids=DataIdRecovery(candidate, round(match_rate, 3)),
        confidence=round(match_rate, 3),
        confidence_label=confidence_label,
        findings=[f'Profile 5: CRC-16 LE at bytes 0-1, Data ID 0x{candidate:04X}, {match_rate:.0%} match'],
    )


def _try_profile4(payloads: List[bytes]) -> Optional[E2eProfileResult]:
    """Try Profile 4: Length(16) Counter(16) DataID(32) CRC-32P4(32) at offset 0, big endian. 12-byte header."""
    if len(payloads[0]) < 12:
        return None

    len(payloads[0])
    matched = 0
    data_id = None

    for p in payloads:
        length = int.from_bytes(p[0:2], 'big')
        if length != len(p):
            continue
        # Read the Data ID from bytes 4-7
        did = int.from_bytes(p[4:8], 'big')
        if data_id is None:
            data_id = did
        elif did != data_id:
            # Data ID should be constant for one identifier
            continue
        # Verify CRC: CRC-32P4 over all data except bytes 8-11 (the CRC field)
        covered = p[:8] + p[12:]
        expected_crc = crc32p4(covered)
        received_crc = int.from_bytes(p[8:12], 'big')
        if received_crc == expected_crc:
            matched += 1

    if matched == 0 or data_id is None:
        return None

    match_rate = matched / len(payloads)
    if match_rate < 0.5:
        return None

    # Detect counter in bytes 2-3
    counter_values = [int.from_bytes(p[2:4], 'big') for p in payloads]
    counter_result = _score_counter(counter_values, 65536, 2, 16, False)

    confidence_label = 'confirmed' if match_rate >= 0.9 else 'likely' if match_rate >= 0.7 else 'possible'

    return E2eProfileResult(
        profile=4,
        crc_position=8,
        crc_width=4,
        counter=counter_result,
        data_ids=DataIdRecovery(data_id, round(match_rate, 3)),
        confidence=round(match_rate, 3),
        confidence_label=confidence_label,
        findings=[f'Profile 4: Data ID 0x{data_id:08X}, length field matches, {match_rate:.0%} CRC match'],
    )


def _try_profile6(payloads: List[bytes]) -> Optional[E2eProfileResult]:
    """Try Profile 6: CRC-16(16) Length(16) Counter(8) at offset 0, implicit 16-bit Data ID. 5-byte header."""
    if len(payloads[0]) < 5:
        return None

    # Check that the Length field matches
    length_ok = 0
    for p in payloads:
        length = int.from_bytes(p[2:4], 'big')
        if length == len(p):
            length_ok += 1
    if length_ok < len(payloads) * 0.8:
        return None

    # Solve for the Data ID from the first frame, then verify on all
    candidate = _solve_crc16_data_id(payloads[0], 0, 2, implicit=True)
    if candidate is None:
        return None

    match_rate = _verify_crc16_data_id(payloads, candidate, implicit=True)
    if match_rate < 0.5:
        for p in payloads[1:5]:
            alt = _solve_crc16_data_id(p, 0, 2, implicit=True)
            if alt is not None and alt != candidate:
                alt_rate = _verify_crc16_data_id(payloads, alt, implicit=True)
                if alt_rate > match_rate:
                    candidate = alt
                    match_rate = alt_rate
        if match_rate < 0.5:
            return None

    # Detect the 8-bit counter at byte 4
    counter_values = [p[4] for p in payloads]
    counter_result = _score_counter(counter_values, 256, 4, 8, False)

    confidence_label = 'confirmed' if match_rate >= 0.9 else 'likely' if match_rate >= 0.7 else 'possible'

    return E2eProfileResult(
        profile=6,
        crc_position=0,
        crc_width=2,
        counter=counter_result,
        data_ids=DataIdRecovery(candidate, round(match_rate, 3)),
        confidence=round(match_rate, 3),
        confidence_label=confidence_label,
        findings=[f'Profile 6: implicit Data ID 0x{candidate:04X}, length field matches, {match_rate:.0%} CRC match'],
    )


def _try_profile7(payloads: List[bytes]) -> Optional[E2eProfileResult]:
    """Try Profile 7: CRC-64(64) Length(32) Counter(32) DataID(32) at offset 0, big endian. 20-byte header."""
    if len(payloads[0]) < 20:
        return None

    len(payloads[0])
    matched = 0
    data_id = None

    for p in payloads:
        length = int.from_bytes(p[8:12], 'big')
        if length != len(p):
            continue
        did = int.from_bytes(p[16:20], 'big')
        if data_id is None:
            data_id = did
        elif did != data_id:
            continue
        # CRC-64 over all data except bytes 0-7 (the CRC field)
        covered = p[8:]
        expected_crc = crc64(covered)
        received_crc = int.from_bytes(p[0:8], 'big')
        if received_crc == expected_crc:
            matched += 1

    if matched == 0 or data_id is None:
        return None

    match_rate = matched / len(payloads)
    if match_rate < 0.5:
        return None

    # Detect 32-bit counter at bytes 12-15
    counter_values = [int.from_bytes(p[12:16], 'big') for p in payloads]
    counter_result = _score_counter(counter_values, 2**32, 12, 32, False)

    confidence_label = 'confirmed' if match_rate >= 0.9 else 'likely' if match_rate >= 0.7 else 'possible'

    return E2eProfileResult(
        profile=7,
        crc_position=0,
        crc_width=8,
        counter=counter_result,
        data_ids=DataIdRecovery(data_id, round(match_rate, 3)),
        confidence=round(match_rate, 3),
        confidence_label=confidence_label,
        findings=[f'Profile 7: Data ID 0x{data_id:08X}, length field matches, {match_rate:.0%} CRC match'],
    )


# ---------------------------------------------------------------------------
# 3. Profile detection entry point
# ---------------------------------------------------------------------------


def detect_e2e_profile(payloads: Sequence[bytes], min_frames: int = MIN_FRAMES) -> List[E2eProfileResult]:
    """Try each known E2E profile against the payloads and return the matches, best first."""
    payloads = list(payloads)
    if len(payloads) < min_frames or not payloads[0]:
        return []

    length = len(payloads[0])
    # Filter to consistent length
    payloads = [p for p in payloads if len(p) == length]
    if len(payloads) < min_frames:
        return []

    results: List[E2eProfileResult] = []

    # Try the length-field profiles first (they have strong structural markers)
    for try_fn in (_try_profile4, _try_profile7, _try_profile6):
        result = try_fn(payloads)
        if result is not None:
            results.append(result)

    # Try Profile 5 (CRC-16 LE, brute-force Data ID)
    result = _try_profile5(payloads)
    if result is not None:
        results.append(result)

    # Try Profile 2 and 22 (need an alive counter first)
    counter = detect_alive_counter(payloads, min_frames)
    if counter is not None and counter.width == 4:
        # The CRC byte is typically adjacent to the counter byte
        # Profile 2/22: CRC at the counter byte's offset or at byte 0
        crc_candidates = set()
        crc_candidates.add(counter.position)
        crc_candidates.add(0)
        if counter.position > 0:
            crc_candidates.add(counter.position - 1)
        if counter.position < length - 1:
            crc_candidates.add(counter.position + 1)

        for crc_byte in sorted(crc_candidates):
            if crc_byte >= length:
                continue
            # Skip trying CRC at the same byte as the counter when the counter is a nibble of that byte
            # unless the CRC is at that byte (Profile 2/22 layout: CRC at offset, counter nibble at offset+1)
            for try_fn in (_try_profile2, _try_profile22):
                result = try_fn(payloads, counter, crc_byte)
                if result is not None:
                    results.append(result)

    # Sort by confidence (best first)
    results.sort(key=lambda r: -r.confidence)
    return results


# ---------------------------------------------------------------------------
# 4. Data ID recovery
# ---------------------------------------------------------------------------


def recover_data_ids(
    payloads: Sequence[bytes],
    profile: int,
    crc_position: int,
    counter: Optional[AliveCounterResult] = None,
) -> Optional[DataIdRecovery]:
    """Given the profile, CRC position and counter position, recover the Data IDs.

    For profiles that transmit the Data ID (4, 7), reads it from the header. For profiles with an implicit
    Data ID (5, 6), brute-forces the 16-bit value. For profile 2/22, recovers the per-counter-value Data IDs.
    """
    payloads = list(payloads)
    if not payloads:
        return None

    if profile in (4, 7):
        return _recover_data_ids_from_header(payloads, profile)
    if profile == 5:
        result = _try_profile5(payloads)
        return result.data_ids if result is not None else None
    if profile == 6:
        result = _try_profile6(payloads)
        return result.data_ids if result is not None else None
    if profile in (2, 22) and counter is not None:
        crc_fn = crc8 if profile == 2 else crc8h2f
        result = _try_profile2_or_22(payloads, counter, crc_position, crc_fn, profile)
        return result.data_ids if result is not None else None
    return None


def _recover_data_ids_from_header(payloads: List[bytes], profile: int) -> Optional[DataIdRecovery]:
    """Read the Data ID from the header for profiles 4 and 7."""
    data_ids: Dict[int, int] = {}
    for p in payloads:
        if profile == 4 and len(p) >= 8:
            did = int.from_bytes(p[4:8], 'big')
            data_ids[did] = data_ids.get(did, 0) + 1
        elif profile == 7 and len(p) >= 20:
            did = int.from_bytes(p[16:20], 'big')
            data_ids[did] = data_ids.get(did, 0) + 1

    if not data_ids:
        return None
    # The most common Data ID
    best_did = max(data_ids, key=lambda k: data_ids[k])
    rate = data_ids[best_did] / len(payloads)
    return DataIdRecovery(best_did, round(rate, 3))


# ---------------------------------------------------------------------------
# 5. E2E bypass detection
# ---------------------------------------------------------------------------


def detect_e2e_bypass(
    payloads: Sequence[bytes],
    crc_position: Optional[int] = None,
    crc_width: int = 1,
    counter: Optional[AliveCounterResult] = None,
) -> List[E2eBypassFinding]:
    """Detect if E2E protection is not properly validated by the receiver."""
    payloads = list(payloads)
    findings: List[E2eBypassFinding] = []
    if len(payloads) < MIN_FRAMES:
        return findings

    length = len(payloads[0])

    # Static CRC: the CRC byte(s) never change even when the payload changes
    if crc_position is not None and crc_position + crc_width <= length:
        crc_values = set()
        payload_values = set()
        for p in payloads:
            crc_values.add(p[crc_position : crc_position + crc_width])
            rest = p[:crc_position] + p[crc_position + crc_width :]
            payload_values.add(rest)

        if len(crc_values) == 1 and len(payload_values) > 1:
            findings.append(
                E2eBypassFinding(
                    'static_crc',
                    f'CRC bytes [{crc_position}:{crc_position + crc_width}] never change despite '
                    f'{len(payload_values)} different payloads: the receiver probably does not check the CRC',
                    'critical',
                )
            )

        # CRC zero or 0xFF
        crc_val = next(iter(crc_values)) if len(crc_values) == 1 else None
        if crc_val is not None:
            if all(b == 0 for b in crc_val):
                findings.append(
                    E2eBypassFinding(
                        'crc_zero_or_ff',
                        'CRC is always 0x00: E2E may be configured but the sender does not compute it',
                        'warning',
                    )
                )
            elif all(b == 0xFF for b in crc_val):
                findings.append(
                    E2eBypassFinding(
                        'crc_zero_or_ff',
                        'CRC is always 0xFF: E2E may be configured but the sender does not compute it',
                        'warning',
                    )
                )

    # Counter freeze
    if counter is not None:
        counter_values = [_extract_counter(p, counter) for p in payloads]
        if len(set(counter_values)) == 1:
            findings.append(
                E2eBypassFinding(
                    'counter_freeze',
                    f'Alive counter at byte {counter.position} is always {counter_values[0]}: no replay protection',
                    'critical',
                )
            )
        else:
            # Check for backward jumps (counter accepted out of order)
            backwards = 0
            for i in range(1, len(counter_values)):
                step = (counter_values[i] - counter_values[i - 1]) % counter.modulus
                if step > counter.modulus // 2:  # Backward jump
                    backwards += 1
            if backwards > len(counter_values) * 0.1:
                findings.append(
                    E2eBypassFinding(
                        'counter_backwards',
                        f'Alive counter jumps backwards in {backwards}/{len(counter_values)} transitions: '
                        f'replay protection may be weak',
                        'warning',
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# 6. E2E header computation
# ---------------------------------------------------------------------------


def compute_e2e_header(
    profile: int,
    data: bytes,
    counter: int,
    data_ids: object,
    offset: int = 0,
) -> bytes:
    """Compute a valid E2E header for arbitrary payload data using recovered parameters.

    Wraps the existing Profile*.protect() methods from e2e.py with the recovered parameters.

    *data_ids*: list of 16 ints for Profile 2/22, single int for Profile 4/5/6/7.
    *offset*: the byte offset of the E2E header in the data (only Profile 22 uses a non-zero offset).
    """
    if profile == 2:
        if not isinstance(data_ids, list) or len(data_ids) != 16:
            raise ValueError('Profile 2 needs a list of 16 Data IDs')
        p = Profile2(data_ids)
        return p.protect(data, counter)

    if profile == 22:
        if not isinstance(data_ids, list) or len(data_ids) != 16:
            raise ValueError('Profile 22 needs a list of 16 Data IDs')
        p22 = Profile22(data_ids, offset=offset)
        return p22.protect(data, counter)

    if profile == 5:
        if not isinstance(data_ids, int):
            raise ValueError('Profile 5 needs a single 16-bit Data ID')
        p5 = Profile5(data_ids)
        return p5.protect(data, counter)

    if profile == 4:
        if not isinstance(data_ids, int):
            raise ValueError('Profile 4 needs a single 32-bit Data ID')
        p4 = Profile4(data_ids, offset=offset)
        return p4.protect(data, counter)

    if profile == 6:
        if not isinstance(data_ids, int):
            raise ValueError('Profile 6 needs a single 16-bit Data ID')
        p6 = Profile6(data_ids, offset=offset)
        return p6.protect(data, counter)

    if profile == 7:
        if not isinstance(data_ids, int):
            raise ValueError('Profile 7 needs a single 32-bit Data ID')
        p7 = Profile7(data_ids, offset=offset)
        return p7.protect(data, counter)

    raise ValueError(f'Unknown E2E profile {profile}')


# ---------------------------------------------------------------------------
# 7. Full detection pipeline
# ---------------------------------------------------------------------------


def detect_e2e(payloads: Sequence[bytes], min_frames: int = MIN_FRAMES) -> E2eDetectionResult:
    """Run the full E2E detection pipeline on one identifier's payloads.

    1. Detect the alive counter
    2. Try all known profiles (2, 4, 5, 6, 7, 22) and recover Data IDs
    3. Check for E2E bypasses on the best match
    """
    payloads = list(payloads)
    if len(payloads) < min_frames or not payloads[0]:
        return E2eDetectionResult([], None, [], findings=['Too few frames for E2E detection'])

    length = len(payloads[0])
    payloads = [p for p in payloads if len(p) == length]
    if len(payloads) < min_frames:
        return E2eDetectionResult([], None, [], findings=['Too few frames after filtering to consistent length'])

    counter = detect_alive_counter(payloads, min_frames)
    profiles = detect_e2e_profile(payloads, min_frames)

    bypasses: List[E2eBypassFinding] = []
    if profiles:
        best = profiles[0]
        bypasses = detect_e2e_bypass(payloads, best.crc_position, best.crc_width, best.counter or counter)

    findings: List[str] = []
    if counter:
        findings.append(counter.description)
    if profiles:
        findings.append(profiles[0].description)
    if bypasses:
        findings.extend(b.description for b in bypasses)
    if not profiles:
        findings.append('No E2E profile detected')

    return E2eDetectionResult(
        profiles=profiles,
        counter=counter,
        bypasses=bypasses,
        findings=findings,
    )
