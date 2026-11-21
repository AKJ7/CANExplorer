"""Cryptanalysis of CAN frame payloads: statistical randomness tests, freshness/replay analysis and keystream
reuse detection.

These tools help assess the security of a frame's trailing bytes (suspected MACs or encrypted data) passively —
they never send anything on the bus. The randomness tests replace the simple half-set/half-flip threshold of the
analysis module with proper statistical tests; the freshness analysis evaluates whether a counter+MAC pair provides
adequate replay protection; and the keystream reuse detector finds repeated counter values whose XOR-of-ciphertexts
reveals structure.
"""

import hashlib
import hmac as _hmac
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from can_explorer.core.reverse.fields import BitField


# ---------------------------------------------------------------------------
# Randomness tests
# ---------------------------------------------------------------------------


@dataclass
class RandomnessResult:
    """The outcome of statistical randomness tests on a byte range of captured payloads."""

    byte_range: Tuple[int, int]  # (first_byte, last_byte) inclusive
    monobit_p: float
    runs_p: float
    chi_square_p: float
    serial_correlation: float
    counter_correlation: Optional[float]  # None if no counter found
    verdict: str  # 'random', 'structured', 'inconclusive'
    confidence: float  # 0 to 1


def _erfc(x: float) -> float:
    """Complementary error function (good to ~1.5e-7), sufficient for p-value estimates."""
    # Abramowitz and Stegun approximation 7.1.26
    t = 1.0 / (1.0 + 0.3275911 * abs(x))
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    result = poly * math.exp(-(x * x))
    return result if x >= 0 else 2.0 - result


def _monobit_p(bits: List[int]) -> float:
    """NIST SP 800-22 Frequency (Monobit) Test p-value: proportion of 1-bits should be ~0.5."""
    n = len(bits)
    if n == 0:
        return 0.0
    s = sum(bits)
    # z-score: (s - n/2) / sqrt(n/4)
    z = abs(2 * s - n) / math.sqrt(n) if n > 0 else 0.0
    return _erfc(z / math.sqrt(2))


def _runs_p(bits: List[int]) -> float:
    """NIST SP 800-22 Runs Test p-value: the number of runs of consecutive identical bits."""
    n = len(bits)
    if n < 2:
        return 0.0
    ones = sum(bits)
    pi = ones / n
    # Pre-test: if the proportion is too far from 0.5, the runs test is not applicable
    if abs(pi - 0.5) >= 2.0 / math.sqrt(n):
        return 0.0
    runs = 1 + sum(1 for i in range(1, n) if bits[i] != bits[i - 1])
    numerator = abs(runs - 2.0 * n * pi * (1 - pi))
    denominator = 2.0 * math.sqrt(2.0 * n) * pi * (1 - pi)
    if denominator == 0:
        return 0.0
    return _erfc(numerator / denominator)


def _chi_square_p(byte_values: List[int], num_bins: int = 256) -> float:
    """Chi-square test p-value for uniform distribution of byte values."""
    n = len(byte_values)
    if n < num_bins:
        return 0.5  # Not enough data; inconclusive
    expected = n / num_bins
    counts = [0] * num_bins
    for v in byte_values:
        counts[v % num_bins] += 1
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    # Approximate p-value using the Wilson-Hilferty normal approximation for the chi-square CDF
    df = num_bins - 1
    z = ((chi2 / df) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / math.sqrt(2.0 / (9.0 * df))
    # p-value: probability that chi-square exceeds chi2 (upper tail)
    p = 0.5 * _erfc(z / math.sqrt(2))
    return max(0.0, min(1.0, p))


def _serial_correlation(byte_values: List[int]) -> float:
    """Lag-1 serial correlation between consecutive frames' byte values (-1 to 1).

    For a random MAC this should be ~0; for a drifting physical value it is high.
    """
    n = len(byte_values)
    if n < 3:
        return 0.0
    mean = sum(byte_values) / n
    var = sum((v - mean) ** 2 for v in byte_values) / n
    if var == 0:
        return 0.0
    cov = sum((byte_values[i] - mean) * (byte_values[i + 1] - mean) for i in range(n - 1)) / (n - 1)
    return max(-1.0, min(1.0, cov / var))


def _counter_correlation(byte_values: List[int], counter_values: Sequence[int]) -> float:
    """Pearson correlation between byte values and counter values."""
    n = min(len(byte_values), len(counter_values))
    if n < 3:
        return 0.0
    mean_b = sum(byte_values[:n]) / n
    mean_c = sum(counter_values[:n]) / n
    var_b = sum((byte_values[i] - mean_b) ** 2 for i in range(n))
    var_c = sum((counter_values[i] - mean_c) ** 2 for i in range(n))
    if var_b == 0 or var_c == 0:
        return 0.0
    cov = sum((byte_values[i] - mean_b) * (counter_values[i] - mean_c) for i in range(n))
    return max(-1.0, min(1.0, cov / math.sqrt(var_b * var_c)))


def test_randomness(
    payloads: Sequence[bytes],
    byte_range: Tuple[int, int],
    counter_values: Optional[Sequence[int]] = None,
) -> RandomnessResult:
    """Run statistical randomness tests on `byte_range` (first_byte, last_byte inclusive) of the payloads.

    Returns a verdict: 'random' (all tests pass, low serial correlation), 'structured' (fails one or more tests or
    high serial correlation) or 'inconclusive' (too few frames to tell).
    """
    first, last = byte_range
    if not payloads or first > last:
        return RandomnessResult(byte_range, 0.0, 0.0, 0.0, 0.0, None, 'inconclusive', 0.0)

    # Extract the bytes in the range from each payload
    byte_values: List[int] = []
    all_bits: List[int] = []
    for payload in payloads:
        for b in range(first, last + 1):
            if b < len(payload):
                val = payload[b]
            else:
                val = 0
            byte_values.append(val)
            for bit_pos in range(8):
                all_bits.append((val >> bit_pos) & 1)

    # Per-frame aggregate (sum of the bytes in range) for serial and counter correlation
    per_frame: List[int] = []
    for i in range(len(payloads)):
        total = 0
        for b in range(first, last + 1):
            if b < len(payloads[i]):
                total += payloads[i][b]
        per_frame.append(total)

    n_frames = len(payloads)
    if n_frames < 20:
        return RandomnessResult(byte_range, 0.0, 0.0, 0.0, 0.0, None, 'inconclusive', 0.0)

    monobit = _monobit_p(all_bits)
    runs = _runs_p(all_bits)
    chi2 = _chi_square_p(byte_values)
    serial = _serial_correlation(per_frame)
    counter_corr = _counter_correlation(per_frame, counter_values) if counter_values is not None else None

    # Verdict
    p_threshold = 0.01
    serial_threshold = 0.3
    tests_passed = sum(
        [
            monobit >= p_threshold,
            runs >= p_threshold,
            chi2 >= p_threshold,
            abs(serial) <= serial_threshold,
        ]
    )

    if n_frames < 100:
        # Not enough frames for a confident verdict, but give a tentative one
        if tests_passed >= 3 and abs(serial) <= serial_threshold:
            verdict = 'random'
            confidence = 0.4 + 0.1 * tests_passed
        elif tests_passed <= 1:
            verdict = 'structured'
            confidence = 0.5
        else:
            verdict = 'inconclusive'
            confidence = 0.3
    else:
        if tests_passed == 4:
            verdict = 'random'
            confidence = 0.7 + 0.1 * min(3, n_frames // 200)
        elif tests_passed <= 1:
            verdict = 'structured'
            confidence = 0.8
        elif tests_passed == 2 and abs(serial) > serial_threshold:
            verdict = 'structured'
            confidence = 0.6
        else:
            verdict = 'inconclusive'
            confidence = 0.4

    # Counter correlation can lower confidence
    if counter_corr is not None and abs(counter_corr) > 0.3:
        if verdict == 'random':
            verdict = 'inconclusive'
            confidence = min(confidence, 0.4)

    return RandomnessResult(
        byte_range=byte_range,
        monobit_p=round(monobit, 6),
        runs_p=round(runs, 6),
        chi_square_p=round(chi2, 6),
        serial_correlation=round(serial, 6),
        counter_correlation=round(counter_corr, 6) if counter_corr is not None else None,
        verdict=verdict,
        confidence=round(min(1.0, max(0.0, confidence)), 2),
    )


# ---------------------------------------------------------------------------
# Freshness and replay analysis
# ---------------------------------------------------------------------------


@dataclass
class FreshnessReport:
    """Assessment of counter+MAC freshness protection of captured traffic."""

    has_freshness: bool  # False if same payload -> same MAC (no freshness value)
    counter_wraps: int  # How many times the counter wrapped
    mac_repeats_on_wrap: bool  # MAC repeats when counter wraps -> too-short freshness window
    mac_bits: int
    forgery_probability: float  # 2^(-mac_bits) per attempt
    frames_per_second: float
    expected_forgery_time: str  # Human-readable, e.g. '4.7 hours' or '194 days'
    replay_possible: bool  # True if no freshness or counter wraps
    findings: List[str] = field(default_factory=list)


def _format_time(seconds: float) -> str:
    """Human-readable time duration."""
    if seconds < 60:
        return f'{seconds:.1f} seconds'
    if seconds < 3600:
        return f'{seconds / 60:.1f} minutes'
    if seconds < 86400:
        return f'{seconds / 3600:.1f} hours'
    if seconds < 365.25 * 86400:
        return f'{seconds / 86400:.1f} days'
    return f'{seconds / (365.25 * 86400):.1f} years'


def analyse_freshness(
    payloads: Sequence[bytes],
    timestamps: Sequence[float],
    counter_field: BitField,
    mac_range: Tuple[int, int],
) -> FreshnessReport:
    """Passively analyse captured traffic for freshness/replay protection quality.

    counter_field: the BitField of the alive counter.
    mac_range: (first_byte, last_byte) inclusive of the MAC bytes.
    """
    n = len(payloads)
    mac_first, mac_last = mac_range
    mac_bits = (mac_last - mac_first + 1) * 8
    forgery_prob = 2.0 ** (-mac_bits) if mac_bits > 0 else 1.0

    # Frame rate
    if n >= 2 and timestamps[-1] > timestamps[0]:
        fps = (n - 1) / (timestamps[-1] - timestamps[0])
    else:
        fps = 0.0

    # Expected time for a forgery at the frame rate
    if fps > 0 and forgery_prob < 1.0:
        expected_attempts = 1.0 / forgery_prob
        expected_seconds = expected_attempts / fps
        expected_time = _format_time(expected_seconds)
    else:
        expected_time = 'N/A'

    findings: List[str] = []

    if n < 2:
        return FreshnessReport(
            has_freshness=False,
            counter_wraps=0,
            mac_repeats_on_wrap=False,
            mac_bits=mac_bits,
            forgery_probability=forgery_prob,
            frames_per_second=round(fps, 1),
            expected_forgery_time=expected_time,
            replay_possible=True,
            findings=['Too few frames to analyse'],
        )

    # Extract counter values
    counter_max = (1 << counter_field.length) - 1
    counters: List[int] = []
    macs: List[bytes] = []
    data_parts: List[bytes] = []
    for payload in payloads:
        # Extract counter using BitField
        mask = (1 << counter_field.length) - 1
        val = int.from_bytes(payload, 'little') if len(payload) > 0 else 0
        counters.append((val >> counter_field.start) & mask)
        mac_bytes = payload[mac_first : mac_last + 1] if mac_last < len(payload) else payload[mac_first:]
        macs.append(mac_bytes)
        # Data part: everything except MAC
        data_parts.append(payload[:mac_first])

    # Check if same data -> same MAC (no freshness)
    data_mac_map: dict = {}
    has_freshness = True
    same_data_same_mac = 0
    same_data_total = 0
    for i in range(n):
        key = data_parts[i]
        if key in data_mac_map:
            same_data_total += 1
            if data_mac_map[key] == macs[i]:
                same_data_same_mac += 1
        else:
            data_mac_map[key] = macs[i]

    if same_data_total > 0 and same_data_same_mac / same_data_total > 0.9:
        has_freshness = False
        findings.append(
            f'Same payload produces the same MAC in {same_data_same_mac}/{same_data_total} cases: '
            'no freshness value detected'
        )

    # Count counter wraps
    wraps = 0
    for i in range(1, n):
        if counters[i] < counters[i - 1] and counters[i - 1] - counters[i] > counter_max // 2:
            wraps += 1

    if wraps > 0:
        findings.append(f'Counter wrapped {wraps} time(s) over {n} frames')

    # Check if MAC repeats when counter wraps back to same value
    counter_to_mac: dict = {}
    mac_repeats_on_wrap = False
    repeat_count = 0
    for i in range(n):
        c = counters[i]
        if c in counter_to_mac:
            if counter_to_mac[c] == macs[i]:
                repeat_count += 1
        else:
            counter_to_mac[c] = macs[i]

    if wraps > 0 and repeat_count > wraps:
        mac_repeats_on_wrap = True
        findings.append(
            f'MAC repeats for the same counter value after wrapping ({repeat_count} repeats): '
            'the freshness window is only the counter range'
        )

    replay_possible = not has_freshness or wraps > 0
    if replay_possible and not findings:
        if not has_freshness:
            findings.append('Replay is possible: no freshness value protects the MAC')
        elif wraps > 0:
            findings.append('Replay of old frames may be possible after counter wraps')

    if mac_bits < 32:
        findings.append(f'Short MAC ({mac_bits} bits): forgery probability {forgery_prob:.2e} per attempt')

    return FreshnessReport(
        has_freshness=has_freshness,
        counter_wraps=wraps,
        mac_repeats_on_wrap=mac_repeats_on_wrap,
        mac_bits=mac_bits,
        forgery_probability=forgery_prob,
        frames_per_second=round(fps, 1),
        expected_forgery_time=expected_time,
        replay_possible=replay_possible,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# MAC pattern detection
# ---------------------------------------------------------------------------


@dataclass
class MacPatternResult:
    """Classification of a suspected MAC by its byte range."""

    byte_range: Tuple[int, int]  # (first_byte, last_byte) inclusive
    mac_bits: int
    likely_algorithm: str  # 'CMAC-AES-128', 'HMAC-SHA-256', 'CRC', 'unknown'
    truncated: bool
    findings: List[str] = field(default_factory=list)


# Common truncated MAC lengths: AUTOSAR SecOC uses 24, 28, 32 or 64-bit truncated CMAC
_MAC_LENGTH_HINTS: dict = {
    8: ('CRC-8 or truncated MAC', True),
    16: ('CRC-16 or truncated MAC', True),
    24: ('CMAC-AES-128 (24-bit truncation, AUTOSAR SecOC)', True),
    28: ('CMAC-AES-128 (28-bit truncation, AUTOSAR SecOC)', True),
    32: ('CMAC-AES-128 (32-bit truncation, AUTOSAR SecOC) or CRC-32', True),
    64: ('CMAC-AES-128 (64-bit truncation) or HMAC-SHA-256 truncated', True),
    128: ('CMAC-AES-128 (full)', False),
    256: ('HMAC-SHA-256 (full)', False),
}


def classify_mac(
    payloads: Sequence[bytes],
    byte_range: Tuple[int, int],
    has_counter: bool = False,
) -> MacPatternResult:
    """Classify a suspected MAC byte range by its length and statistical properties.

    Uses the byte range length to infer the most likely algorithm. For CAN payloads, truncated MACs
    (24-32 bits) are common in AUTOSAR SecOC; full 128-bit or 256-bit MACs only appear in CAN FD frames.

    *has_counter*: set to True if a freshness counter was found in the same frame, which makes a truncated
    CMAC-AES-128 more likely than a CRC.
    """
    first, last = byte_range
    mac_bits = (last - first + 1) * 8

    findings: List[str] = []

    if mac_bits in _MAC_LENGTH_HINTS:
        hint, truncated = _MAC_LENGTH_HINTS[mac_bits]
        likely = hint
    elif mac_bits < 128:
        likely = 'truncated MAC (non-standard length)'
        truncated = True
    else:
        likely = 'unknown'
        truncated = False

    # If a counter is present alongside a 24-32 bit field, it strongly suggests SecOC CMAC, not CRC
    if has_counter and mac_bits in (24, 28, 32):
        likely = f'CMAC-AES-128 ({mac_bits}-bit truncation, AUTOSAR SecOC)'
        findings.append('Freshness counter present: truncated CMAC-AES-128 is the most likely algorithm')
    elif has_counter and mac_bits == 16:
        likely = 'CMAC-AES-128 (16-bit truncation) or CRC-16'
        findings.append('Freshness counter present: could be a very short truncated MAC (weak) or a CRC')
    elif not has_counter and mac_bits in (24, 28, 32):
        findings.append('No freshness counter found: could be CRC rather than a truncated MAC')

    # Statistical check: if all bytes in the range pass randomness, it is more likely a MAC than a CRC
    if len(payloads) >= 50:
        randomness = test_randomness(payloads, byte_range)
        if randomness.verdict == 'random':
            findings.append(f'Bytes pass randomness tests (confidence {randomness.confidence}): consistent with a MAC')
            if 'CRC' in likely:
                likely = likely.replace('CRC', 'MAC or CRC')
        elif randomness.verdict == 'structured':
            findings.append('Bytes show structure: more likely a CRC or checksum than a cryptographic MAC')

    return MacPatternResult(
        byte_range=byte_range,
        mac_bits=mac_bits,
        likely_algorithm=likely,
        truncated=truncated,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Keystream reuse detection
# ---------------------------------------------------------------------------


@dataclass
class KeystreamReuseReport:
    """Results of detecting keystream reuse in encrypted CAN payloads."""

    counter_field: BitField
    reused_counter_values: List[int]  # Counter values that appeared more than once
    structured_xor_pairs: int  # How many XOR-of-same-counter pairs showed structure
    findings: List[str] = field(default_factory=list)


def _byte_entropy(data: bytes) -> float:
    """Shannon entropy of a byte sequence (0 to 8 bits)."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in counts if c > 0)


def _is_structured(xor_result: bytes) -> bool:
    """Heuristic: a XOR of two plaintexts (or two ciphertexts with the same keystream) shows structure —
    low entropy, mostly ASCII-printable bytes or many zero bytes.

    For short sequences (CAN payloads are at most 64 bytes), the thresholds are conservative to avoid false
    positives from random data.
    """
    if len(xor_result) < 2:
        return False
    n = len(xor_result)
    # Many zero bytes (identical plaintexts at that position): needs a strong majority
    zero_count = sum(1 for b in xor_result if b == 0)
    if zero_count >= max(2, n * 0.7):
        return True
    # Mostly ASCII-printable (0x20-0x7E) or null — only for longer sequences where it is unlikely by chance
    printable_count = sum(1 for b in xor_result if 0x20 <= b <= 0x7E or b == 0)
    if n >= 8 and printable_count == n:
        return True
    # Low entropy is unreliable for short sequences; only use for longer ones
    if n >= 8:
        entropy = _byte_entropy(xor_result)
        if entropy < 2.0:
            return True
    return False


def detect_keystream_reuse(
    payloads: Sequence[bytes],
    counter_field: BitField,
    encrypted_range: Tuple[int, int],
) -> KeystreamReuseReport:
    """Detect keystream reuse by finding repeated counter values and checking whether XOR of same-counter payloads
    shows structure (low entropy, ASCII-range bytes, repeated patterns).

    encrypted_range: (first_byte, last_byte) inclusive of the suspected encrypted bytes.
    """
    enc_first, enc_last = encrypted_range

    # Extract counter values and encrypted portions
    counter_to_indices: dict = {}
    for i, payload in enumerate(payloads):
        mask = (1 << counter_field.length) - 1
        val = int.from_bytes(payload, 'little') if len(payload) > 0 else 0
        counter_val = (val >> counter_field.start) & mask
        counter_to_indices.setdefault(counter_val, []).append(i)

    reused = sorted(c for c, indices in counter_to_indices.items() if len(indices) > 1)
    findings: List[str] = []
    structured_pairs = 0

    if not reused:
        findings.append('No counter values repeated: no keystream reuse detected')
        return KeystreamReuseReport(
            counter_field=counter_field,
            reused_counter_values=[],
            structured_xor_pairs=0,
            findings=findings,
        )

    findings.append(f'{len(reused)} counter value(s) appeared more than once')

    for counter_val in reused:
        indices = counter_to_indices[counter_val]
        # Compare all pairs with this counter value
        for a in range(len(indices)):
            for b in range(a + 1, len(indices)):
                pa = payloads[indices[a]]
                pb = payloads[indices[b]]
                # XOR the encrypted ranges
                xor_bytes = bytes(
                    (pa[k] if k < len(pa) else 0) ^ (pb[k] if k < len(pb) else 0)
                    for k in range(enc_first, enc_last + 1)
                )
                if _is_structured(xor_bytes):
                    structured_pairs += 1

    if structured_pairs > 0:
        findings.append(
            f'{structured_pairs} XOR pair(s) of frames with the same counter showed structure: likely keystream reuse'
        )
    else:
        findings.append(
            'Counter values repeated but XOR of same-counter frames showed no structure: '
            'keystream may still be unique (e.g. a wider nonce is used)'
        )

    return KeystreamReuseReport(
        counter_field=counter_field,
        reused_counter_values=reused,
        structured_xor_pairs=structured_pairs,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# SecOC layout detection (without ARXML)
# ---------------------------------------------------------------------------


@dataclass
class SecOcLayout:
    """Detected SecOC trailer layout for one CAN identifier."""

    identifier: int
    payload_end: int  # Last byte index of authentic payload (exclusive)
    freshness_start: int  # First byte index of the freshness value
    freshness_end: int  # Last byte index of freshness value (exclusive)
    freshness_bits: int
    mac_start: int  # First byte index of the MAC
    mac_end: int  # Last byte index of MAC (exclusive)
    mac_bits: int
    confidence: float  # 0 to 1
    findings: List[str] = field(default_factory=list)


def _detect_monotonic_range(
    payloads: Sequence[bytes],
    start: int,
    end: int,
) -> Tuple[float, int]:
    """Score how monotonic a byte range is across consecutive payloads.

    Returns (monotonic_ratio, counter_width_bits).
    """
    if not payloads or start >= end:
        return 0.0, 0

    width = end - start
    values: List[int] = []
    for p in payloads:
        if end <= len(p):
            val = int.from_bytes(p[start:end], 'big')
            values.append(val)

    if len(values) < 10:
        return 0.0, 0

    monotonic = 0
    max_val = (1 << (width * 8)) - 1
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        if diff == 0:
            continue
        if 0 < diff <= max_val // 4:
            monotonic += 1
        elif diff < 0 and values[i] < max_val // 4 and values[i - 1] > max_val * 3 // 4:
            monotonic += 1  # Wrap-around

    non_zero_transitions = sum(1 for i in range(1, len(values)) if values[i] != values[i - 1])
    if non_zero_transitions == 0:
        return 0.0, width * 8

    ratio = monotonic / non_zero_transitions
    return ratio, width * 8


def detect_secoc_layout(
    payloads: Sequence[bytes],
    identifier: int = 0,
    min_frames: int = 50,
) -> Optional[SecOcLayout]:
    """Detect the SecOC trailer layout (freshness + MAC boundary) from statistical properties.

    Looks for a trailing region where the last N bytes pass randomness tests (MAC candidate) and the bytes
    immediately before them are monotonically increasing (freshness counter candidate). The authentic payload
    precedes the freshness value.

    Returns None if no SecOC layout is detected with sufficient confidence.
    """
    if len(payloads) < min_frames:
        return None

    payload_len = len(payloads[0])
    if payload_len < 3:
        return None

    # Ensure all payloads have the same length
    if any(len(p) != payload_len for p in payloads):
        return None

    findings: List[str] = []
    best: Optional[Tuple[float, int, int, int, int, List[str]]] = None

    # Try different MAC lengths (common SecOC truncation lengths)
    candidate_mac_bytes = []
    if payload_len >= 8:
        candidate_mac_bytes = [3, 4, 2, 8]  # 24, 32, 16, 64 bits
    elif payload_len >= 4:
        candidate_mac_bytes = [3, 2]  # 24, 16 bits

    for mac_len in candidate_mac_bytes:
        mac_start = payload_len - mac_len
        if mac_start < 2:
            continue

        # Test if MAC region is random
        randomness = test_randomness(payloads, (mac_start, payload_len - 1))
        if randomness.verdict != 'random':
            continue

        # Try different freshness lengths before the MAC
        for fresh_len in [1, 2, 3, 4]:
            fresh_start = mac_start - fresh_len
            if fresh_start < 0:
                continue

            mono_ratio, counter_bits = _detect_monotonic_range(payloads, fresh_start, mac_start)

            if mono_ratio < 0.6:
                continue

            # Score this candidate
            score = mono_ratio * 0.5 + randomness.confidence * 0.5

            # Prefer standard SecOC lengths
            mac_bits = mac_len * 8
            if mac_bits in (24, 28, 32):
                score += 0.1
            if counter_bits in (4, 8, 16):
                score += 0.05

            cand_findings = [
                f'MAC candidate: bytes [{mac_start}:{payload_len}] ({mac_bits} bits), '
                f'randomness confidence {randomness.confidence}',
                f'Freshness candidate: bytes [{fresh_start}:{mac_start}] ({counter_bits} bits), '
                f'monotonic ratio {mono_ratio:.2f}',
            ]

            if best is None or score > best[0]:
                best = (score, fresh_start, mac_start, mac_len, fresh_len, cand_findings)

    if best is None:
        return None

    score, fresh_start, mac_start, mac_len, fresh_len, cand_findings = best
    mac_bits = mac_len * 8
    fresh_bits = fresh_len * 8

    findings.extend(cand_findings)

    mac_class = classify_mac(payloads, (mac_start, payload_len - 1), has_counter=True)
    findings.append(f'MAC classification: {mac_class.likely_algorithm}')

    confidence = min(1.0, max(0.0, score))
    if confidence < 0.4:
        return None

    return SecOcLayout(
        identifier=identifier,
        payload_end=fresh_start,
        freshness_start=fresh_start,
        freshness_end=mac_start,
        freshness_bits=fresh_bits,
        mac_start=mac_start,
        mac_end=payload_len,
        mac_bits=mac_bits,
        confidence=round(confidence, 2),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Freshness value reconstruction and sync analysis (2.2)
# ---------------------------------------------------------------------------


@dataclass
class FreshnessReconstruction:
    """Reconstructed freshness scheme for one SecOC-protected identifier."""

    identifier: int
    counter_width_bits: int  # Detected width of the truncated counter on the bus
    counter_range: int  # 2^counter_width_bits
    wrap_count: int  # How many times the counter wrapped in the capture
    freshness_type: str  # 'monotonic', 'trip_reset', 'time_based', 'unknown'
    frames_per_wrap: Optional[float]  # Average frames between wraps
    seconds_per_wrap: Optional[float]  # Average time between wraps
    effective_window: str  # Human-readable replay window
    findings: List[str] = field(default_factory=list)


@dataclass
class SyncMessage:
    """A candidate freshness synchronisation message."""

    identifier: int
    sync_target_ids: List[int]  # IDs whose counters reset when this message appears
    sync_evidence: str
    confidence: float


def reconstruct_freshness(
    payloads: Sequence[bytes],
    timestamps: Sequence[float],
    freshness_start: int,
    freshness_end: int,
    identifier: int = 0,
) -> FreshnessReconstruction:
    """Reconstruct the full freshness scheme from the truncated counter on the bus.

    Detects counter width, wrap behaviour, freshness manager type and the effective replay window.
    """
    n = len(payloads)
    width = freshness_end - freshness_start
    counter_bits = width * 8
    counter_range = 1 << counter_bits
    findings: List[str] = []

    if n < 2:
        return FreshnessReconstruction(
            identifier=identifier,
            counter_width_bits=counter_bits,
            counter_range=counter_range,
            wrap_count=0,
            freshness_type='unknown',
            frames_per_wrap=None,
            seconds_per_wrap=None,
            effective_window='N/A',
            findings=['Too few frames to reconstruct freshness'],
        )

    # Extract counter values
    counters: List[int] = []
    for p in payloads:
        if freshness_end <= len(p):
            val = int.from_bytes(p[freshness_start:freshness_end], 'big')
            counters.append(val)
        else:
            counters.append(0)

    # Detect wraps
    wraps: List[int] = []  # Indices where wraps occurred
    for i in range(1, n):
        if counters[i] < counters[i - 1] and counters[i - 1] - counters[i] > counter_range // 2:
            wraps.append(i)

    wrap_count = len(wraps)
    findings.append(f'Counter width: {counter_bits} bits (range 0..{counter_range - 1})')
    findings.append(f'Counter wraps detected: {wrap_count}')

    # Frames and time per wrap
    frames_per_wrap: Optional[float] = None
    seconds_per_wrap: Optional[float] = None

    if wrap_count > 0:
        frames_per_wrap = n / wrap_count
        total_time = timestamps[-1] - timestamps[0] if timestamps[-1] > timestamps[0] else 0.0
        if total_time > 0:
            seconds_per_wrap = total_time / wrap_count

    # Determine freshness type
    # Monotonic: counter increments by 1 (or small constant) each frame
    increments: List[int] = []
    for i in range(1, min(n, 1000)):
        diff = counters[i] - counters[i - 1]
        if diff < 0:
            diff += counter_range
        if diff < counter_range // 2:
            increments.append(diff)

    if not increments:
        freshness_type = 'unknown'
    else:
        mean_inc = sum(increments) / len(increments)
        if 0.8 <= mean_inc <= 1.2:
            freshness_type = 'monotonic'
            findings.append('Counter increments by 1 per frame: monotonic freshness counter')
        elif mean_inc > 100:
            freshness_type = 'time_based'
            findings.append(f'Counter increments by ~{mean_inc:.0f} per frame: likely time-based freshness')
        else:
            # Check for resets (counter drops to near zero without reaching max)
            resets = sum(
                1 for i in range(1, n) if counters[i] < counters[i - 1] and counters[i - 1] < counter_range * 3 // 4
            )
            if resets > 0:
                freshness_type = 'trip_reset'
                findings.append(f'Counter resets detected ({resets} times) without reaching max: trip/reset counter')
            else:
                freshness_type = 'monotonic'
                findings.append(f'Counter increments by ~{mean_inc:.1f} per frame: monotonic counter')

    # Effective replay window
    if seconds_per_wrap is not None:
        effective_window = _format_time(seconds_per_wrap)
        findings.append(f'Effective replay window: {effective_window} (time between counter wraps)')
    elif frames_per_wrap is not None:
        effective_window = f'{frames_per_wrap:.0f} frames'
        findings.append(f'Effective replay window: ~{frames_per_wrap:.0f} frames')
    else:
        total_time = timestamps[-1] - timestamps[0] if n >= 2 and timestamps[-1] > timestamps[0] else 0.0
        if total_time > 0 and wrap_count == 0:
            effective_window = f'>{_format_time(total_time)} (no wrap observed)'
            findings.append(f'No counter wrap observed over {_format_time(total_time)}')
        else:
            effective_window = 'N/A'

    return FreshnessReconstruction(
        identifier=identifier,
        counter_width_bits=counter_bits,
        counter_range=counter_range,
        wrap_count=wrap_count,
        freshness_type=freshness_type,
        frames_per_wrap=round(frames_per_wrap, 1) if frames_per_wrap is not None else None,
        seconds_per_wrap=round(seconds_per_wrap, 3) if seconds_per_wrap is not None else None,
        effective_window=effective_window,
        findings=findings,
    )


def detect_sync_messages(
    all_payloads: Dict[int, List[bytes]],
    all_timestamps: Dict[int, List[float]],
    freshness_ranges: Dict[int, Tuple[int, int]],
) -> List[SyncMessage]:
    """Detect synchronisation messages that reset counters on multiple SecOC-protected identifiers.

    *all_payloads*: mapping of CAN identifier to list of payloads.
    *all_timestamps*: mapping of CAN identifier to list of timestamps.
    *freshness_ranges*: mapping of CAN identifier to (freshness_start, freshness_end) byte indices.

    A sync message is an identifier whose appearance correlates with counter resets on multiple other identifiers.
    """
    if len(freshness_ranges) < 2:
        return []

    # For each monitored ID, find timestamps where the counter resets
    reset_times: Dict[int, List[float]] = {}
    for can_id, frange in freshness_ranges.items():
        payloads = all_payloads.get(can_id, [])
        timestamps = all_timestamps.get(can_id, [])
        fresh_start, fresh_end = frange
        counter_range = 1 << ((fresh_end - fresh_start) * 8)

        resets: List[float] = []
        prev_val = None
        for i, p in enumerate(payloads):
            if fresh_end <= len(p):
                val = int.from_bytes(p[fresh_start:fresh_end], 'big')
            else:
                val = 0
            if prev_val is not None and val < prev_val and prev_val - val > counter_range // 2:
                if i < len(timestamps):
                    resets.append(timestamps[i])
            prev_val = val

        if resets:
            reset_times[can_id] = resets

    if not reset_times:
        return []

    # For each non-monitored ID in all_timestamps, check if its frame times correlate with resets
    monitored = set(freshness_ranges.keys())
    candidates: List[SyncMessage] = []

    for cand_id, cand_ts in all_timestamps.items():
        if cand_id in monitored or not cand_ts:
            continue

        synced_ids: List[int] = []
        for mon_id, resets in reset_times.items():
            # For each reset, check if the candidate sent a frame within 50ms before it
            matches = 0
            for rt in resets:
                for ct in cand_ts:
                    if -0.01 <= rt - ct <= 0.05:
                        matches += 1
                        break
            if len(resets) > 0 and matches / len(resets) >= 0.5:
                synced_ids.append(mon_id)

        if len(synced_ids) >= 2:
            conf = min(1.0, len(synced_ids) / len(reset_times))
            candidates.append(
                SyncMessage(
                    identifier=cand_id,
                    sync_target_ids=sorted(synced_ids),
                    sync_evidence=(
                        f'Identifier 0x{cand_id:03X} appears within 50 ms before counter resets on '
                        f'{len(synced_ids)} monitored identifiers'
                    ),
                    confidence=round(conf, 2),
                )
            )

    candidates.sort(key=lambda s: -s.confidence)
    return candidates


# ---------------------------------------------------------------------------
# MAC truncation strength assessment (2.3)
# ---------------------------------------------------------------------------

# AUTOSAR SecOC recommended minimum MAC truncation lengths by domain
_SECOC_RECOMMENDATIONS: Dict[str, int] = {
    'safety_critical': 24,
    'chassis': 28,
    'powertrain': 32,
    'infotainment': 16,
}


@dataclass
class SecOcStrengthAssessment:
    """Combined security margin assessment for a SecOC-protected identifier."""

    identifier: int
    mac_bits: int
    freshness_bits: int
    forgery_probability: float  # 2^(-mac_bits) per attempt
    expected_forgery_time: str  # At the observed frame rate
    frames_per_second: float
    freshness_window: str  # Time or frames before counter wraps
    replay_before_forgery: bool  # True if counter wraps faster than expected forgery time
    meets_autosar: Dict[str, bool]  # domain -> meets recommendation
    severity: str  # 'critical', 'warning', 'acceptable', 'strong'
    findings: List[str] = field(default_factory=list)


def secoc_strength_assessment(
    payloads: Sequence[bytes],
    timestamps: Sequence[float],
    mac_start: int,
    mac_end: int,
    freshness_start: int,
    freshness_end: int,
    identifier: int = 0,
) -> SecOcStrengthAssessment:
    """Compute the practical security margin of a SecOC-protected identifier.

    Combines MAC truncation strength, freshness window and frame rate to determine whether
    the protection is adequate for different AUTOSAR domains.
    """
    n = len(payloads)
    mac_bits = (mac_end - mac_start) * 8
    freshness_bits = (freshness_end - freshness_start) * 8
    forgery_prob = 2.0 ** (-mac_bits) if mac_bits > 0 else 1.0
    findings: List[str] = []

    # Frame rate
    if n >= 2 and timestamps[-1] > timestamps[0]:
        fps = (n - 1) / (timestamps[-1] - timestamps[0])
    else:
        fps = 0.0

    # Expected forgery time
    if fps > 0 and forgery_prob < 1.0:
        expected_attempts = 1.0 / forgery_prob
        expected_seconds = expected_attempts / fps
        expected_forgery_time = _format_time(expected_seconds)
    else:
        expected_forgery_time = 'N/A'
        expected_seconds = float('inf')

    # Freshness reconstruction
    fresh = reconstruct_freshness(payloads, timestamps, freshness_start, freshness_end, identifier)
    freshness_window = fresh.effective_window

    # Check if replay beats forgery
    replay_before_forgery = False
    if fresh.seconds_per_wrap is not None and expected_seconds < float('inf'):
        if fresh.seconds_per_wrap < expected_seconds:
            replay_before_forgery = True
            findings.append(
                f'Counter wraps every {_format_time(fresh.seconds_per_wrap)} but forgery takes '
                f'{expected_forgery_time}: replay + counter collision may be faster than brute-force forgery'
            )

    # AUTOSAR recommendations
    meets: Dict[str, bool] = {}
    for domain, min_bits in _SECOC_RECOMMENDATIONS.items():
        meets[domain] = mac_bits >= min_bits
        if not meets[domain]:
            findings.append(
                f'MAC length {mac_bits} bits is below the AUTOSAR recommendation of {min_bits} bits for {domain}'
            )

    # Severity
    if mac_bits < 16:
        severity = 'critical'
        findings.append(f'MAC truncation to {mac_bits} bits is critically weak')
    elif mac_bits < 24:
        severity = 'warning'
        findings.append(f'MAC truncation to {mac_bits} bits provides limited protection')
    elif mac_bits < 32:
        severity = 'acceptable'
    else:
        severity = 'strong'

    if replay_before_forgery:
        if severity in ('acceptable', 'strong'):
            severity = 'warning'

    findings.append(f'Forgery probability: {forgery_prob:.2e} per attempt ({mac_bits}-bit MAC)')
    findings.append(f'Frame rate: {fps:.1f} fps')
    findings.append(f'Expected time to forgery: {expected_forgery_time}')

    return SecOcStrengthAssessment(
        identifier=identifier,
        mac_bits=mac_bits,
        freshness_bits=freshness_bits,
        forgery_probability=forgery_prob,
        expected_forgery_time=expected_forgery_time,
        frames_per_second=round(fps, 1),
        freshness_window=freshness_window,
        replay_before_forgery=replay_before_forgery,
        meets_autosar=meets,
        severity=severity,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# SecOC bypass detection (2.7)
# ---------------------------------------------------------------------------


@dataclass
class SecOcBypassFinding:
    """A detected SecOC bypass or enforcement weakness."""

    identifier: int
    bypass_type: str  # 'missing_trailer', 'static_mac', 'wrap_accepted', 'post_reset_window'
    description: str
    evidence: str
    severity: str  # 'critical', 'warning', 'info'


def detect_secoc_bypass(
    payloads: Sequence[bytes],
    timestamps: Sequence[float],
    expected_length: Optional[int] = None,
    mac_start: Optional[int] = None,
    mac_end: Optional[int] = None,
    freshness_start: Optional[int] = None,
    freshness_end: Optional[int] = None,
    identifier: int = 0,
) -> List[SecOcBypassFinding]:
    """Detect paths where SecOC protection is configured but not enforced.

    Checks for:
    - PDUs shorter than expected (missing security trailer)
    - PDUs where the MAC never changes regardless of payload (MAC not checked)
    - PDUs accepted after counter wraps without rejection
    - Transition periods after boot where SecOC is not active
    """
    findings: List[SecOcBypassFinding] = []
    n = len(payloads)

    if n < 2:
        return findings

    # 1. Missing trailer: frames shorter than expected
    if expected_length is not None:
        short_count = sum(1 for p in payloads if len(p) < expected_length)
        if short_count > 0:
            ratio = short_count / n
            sev = 'critical' if ratio > 0.1 else 'warning'
            findings.append(
                SecOcBypassFinding(
                    identifier=identifier,
                    bypass_type='missing_trailer',
                    description=(
                        f'{short_count}/{n} frames ({ratio:.1%}) are shorter than the expected '
                        f'{expected_length} bytes: security trailer may be missing'
                    ),
                    evidence=f'Short frames: {short_count}/{n}',
                    severity=sev,
                )
            )

    # 2. Static MAC: MAC bytes never change regardless of payload
    if mac_start is not None and mac_end is not None:
        macs: List[bytes] = []
        data_parts: List[bytes] = []
        for p in payloads:
            if mac_end <= len(p):
                macs.append(p[mac_start:mac_end])
                data_parts.append(p[:mac_start])

        if len(macs) >= 10:
            unique_macs = len(set(macs))
            unique_data = len(set(data_parts))
            if unique_macs == 1 and unique_data > 1:
                findings.append(
                    SecOcBypassFinding(
                        identifier=identifier,
                        bypass_type='static_mac',
                        description=(
                            f'MAC bytes [{mac_start}:{mac_end}] are always the same value despite '
                            f'{unique_data} different payloads: MAC is likely not checked by the receiver'
                        ),
                        evidence=f'Unique MACs: {unique_macs}, unique payloads: {unique_data}',
                        severity='critical',
                    )
                )
            elif unique_macs <= 3 and unique_data > 10:
                findings.append(
                    SecOcBypassFinding(
                        identifier=identifier,
                        bypass_type='static_mac',
                        description=(
                            f'Only {unique_macs} distinct MAC values across {unique_data} different payloads: '
                            f'MAC may not be properly computed'
                        ),
                        evidence=f'Unique MACs: {unique_macs}, unique payloads: {unique_data}',
                        severity='warning',
                    )
                )

    # 3. Post-reset window: if early frames lack MAC but later ones have it
    if mac_start is not None and mac_end is not None and n >= 20:
        early = payloads[: min(10, n // 5)]
        late = payloads[-(min(10, n // 5)) :]

        early_random = test_randomness(list(early), (mac_start, mac_end - 1))
        late_random = test_randomness(list(late), (mac_start, mac_end - 1))

        if early_random.verdict == 'structured' and late_random.verdict == 'random':
            # First frames with structure, later frames random => SecOC not active at start
            if len(timestamps) >= 2:
                window_ms = (timestamps[min(10, n // 5)] - timestamps[0]) * 1000.0
            else:
                window_ms = 0.0
            findings.append(
                SecOcBypassFinding(
                    identifier=identifier,
                    bypass_type='post_reset_window',
                    description=(
                        f'Early frames show structured MAC bytes but later frames are random: '
                        f'SecOC may not be active for ~{window_ms:.0f} ms after boot'
                    ),
                    evidence=f'Early verdict: {early_random.verdict}, late verdict: {late_random.verdict}',
                    severity='warning',
                )
            )

    # 4. Counter wrap acceptance: check if counter wraps back and frames continue normally
    if freshness_start is not None and freshness_end is not None:
        counter_range = 1 << ((freshness_end - freshness_start) * 8)
        counters: List[int] = []
        for p in payloads:
            if freshness_end <= len(p):
                counters.append(int.from_bytes(p[freshness_start:freshness_end], 'big'))

        wrap_indices: List[int] = []
        for i in range(1, len(counters)):
            if counters[i] < counters[i - 1] and counters[i - 1] - counters[i] > counter_range // 2:
                wrap_indices.append(i)

        if len(wrap_indices) >= 2:
            # Multiple wraps with no apparent gap => counter wraps are accepted
            wrap_gaps = []
            for wi in wrap_indices:
                if wi + 1 < len(timestamps) and wi > 0:
                    gap = timestamps[wi + 1] - timestamps[wi]
                    wrap_gaps.append(gap)
            if wrap_gaps and max(wrap_gaps) < 0.5:
                findings.append(
                    SecOcBypassFinding(
                        identifier=identifier,
                        bypass_type='wrap_accepted',
                        description=(
                            f'Counter wrapped {len(wrap_indices)} times with no visible rejection '
                            f'(max gap {max(wrap_gaps) * 1000:.0f} ms): old frames may be replayable '
                            f'after wrap'
                        ),
                        evidence=f'Wrap count: {len(wrap_indices)}, max inter-frame gap at wrap: '
                        f'{max(wrap_gaps) * 1000:.0f} ms',
                        severity='warning',
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# 1.6 CAN FD payload security assessment
# ---------------------------------------------------------------------------


@dataclass
class FdSecurityAssessment:
    """Security assessment of a CAN FD payload (up to 64 bytes)."""

    identifier: int
    payload_length: int
    randomness: Optional[RandomnessResult]
    mac_classification: Optional[MacPatternResult]
    mac_byte_range: Optional[Tuple[int, int]]
    full_mac_possible: bool
    freshness_byte_range: Optional[Tuple[int, int]]
    findings: List[str] = field(default_factory=list)


def assess_fd_security(
    payloads: List[bytes],
    identifier: int = 0,
) -> FdSecurityAssessment:
    """Assess security properties of CAN FD payloads (up to 64 bytes) with FD-specific heuristics.

    CAN FD frames can carry full 128-bit MACs and longer freshness values than classic CAN.
    This function scans for random trailing regions (MAC), monotonic regions (freshness counters)
    and classifies the security properties.
    """
    findings: List[str] = []
    if not payloads:
        return FdSecurityAssessment(identifier, 0, None, None, None, False, None, ['No payloads provided'])

    payload_len = len(payloads[0])
    if any(len(p) != payload_len for p in payloads):
        return FdSecurityAssessment(
            identifier, payload_len, None, None, None, False, None, ['Inconsistent payload lengths']
        )

    full_mac_possible = payload_len >= 16
    if payload_len > 8:
        findings.append(f'CAN FD payload: {payload_len} bytes (can carry full 128-bit MACs)')
    else:
        findings.append(f'Classic CAN payload length: {payload_len} bytes')

    fd_mac_candidates = [16, 8, 4, 3, 2] if payload_len >= 20 else [8, 4, 3, 2]
    best_mac_range: Optional[Tuple[int, int]] = None
    best_randomness: Optional[RandomnessResult] = None
    best_mac_class: Optional[MacPatternResult] = None
    best_fresh_range: Optional[Tuple[int, int]] = None
    best_score = 0.0

    for mac_len in fd_mac_candidates:
        mac_start = payload_len - mac_len
        if mac_start < 2:
            continue

        randomness = test_randomness(list(payloads), (mac_start, payload_len - 1))
        if randomness.verdict != 'random':
            continue

        for fresh_len in [1, 2, 3, 4, 8]:
            fresh_start = mac_start - fresh_len
            if fresh_start < 0:
                continue

            mono_ratio, counter_bits = _detect_monotonic_range(payloads, fresh_start, mac_start)
            if mono_ratio < 0.5:
                continue

            score = mono_ratio * 0.4 + randomness.confidence * 0.4
            mac_bits = mac_len * 8
            if mac_bits == 128:
                score += 0.15
            elif mac_bits in (24, 28, 32, 64):
                score += 0.1
            if counter_bits in (8, 16, 32):
                score += 0.05

            if score > best_score:
                best_score = score
                best_mac_range = (mac_start, payload_len - 1)
                best_randomness = randomness
                best_fresh_range = (fresh_start, mac_start - 1)
                best_mac_class = classify_mac(list(payloads), (mac_start, payload_len - 1), has_counter=True)

    if best_mac_range is not None:
        mac_bits = (best_mac_range[1] - best_mac_range[0] + 1) * 8
        findings.append(f'Detected MAC region: bytes [{best_mac_range[0]}:{best_mac_range[1] + 1}] ({mac_bits} bits)')
        if mac_bits == 128:
            findings.append('Full 128-bit MAC: strong protection, no truncation weakness')
        elif mac_bits >= 64:
            findings.append(f'{mac_bits}-bit MAC: good truncation margin for CAN FD')
        else:
            findings.append(f'{mac_bits}-bit truncated MAC: consider using longer MACs with CAN FD bandwidth')
    if best_fresh_range is not None:
        fresh_bits = (best_fresh_range[1] - best_fresh_range[0] + 1) * 8
        findings.append(
            f'Detected freshness region: bytes [{best_fresh_range[0]}:{best_fresh_range[1] + 1}] ({fresh_bits} bits)'
        )
    if best_mac_class is not None:
        findings.append(f'MAC classification: {best_mac_class.likely_algorithm}')

    return FdSecurityAssessment(
        identifier=identifier,
        payload_length=payload_len,
        randomness=best_randomness,
        mac_classification=best_mac_class,
        mac_byte_range=best_mac_range,
        full_mac_possible=full_mac_possible,
        freshness_byte_range=best_fresh_range,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 2.4 Known-plaintext MAC key recovery
# ---------------------------------------------------------------------------

_COMMON_KEYS: List[Tuple[bytes, str]] = [
    (b'\x00' * 16, 'all-zero'),
    (b'\xff' * 16, 'all-one'),
    (b'\xab' * 16, 'all-0xAB'),
    (b'\xcd' * 16, 'all-0xCD'),
    (b'\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10', 'sequential-1-to-16'),
    (bytes(range(16)), 'sequential-0-to-15'),
    (b'\xaa\x55' * 8, 'alternating-0xAA55'),
    (b'\x55\xaa' * 8, 'alternating-0x55AA'),
    (b'\xde\xad\xbe\xef' * 4, 'deadbeef'),
]


def _hmac_mac(key: bytes, data: bytes, mac_bytes: int) -> bytes:
    """Truncated HMAC-SHA-256 standing in for CMAC-AES-128 in key recovery tests."""
    return _hmac.new(key, data, hashlib.sha256).digest()[:mac_bytes]


@dataclass
class KeyRecoveryResult:
    """Result of trying to recover a SecOC MAC key."""

    identifier: int
    recovered_key: Optional[bytes]
    key_source: Optional[str]
    attempts: int
    mac_algorithm: str
    confidence: float
    verified_frames: int
    findings: List[str] = field(default_factory=list)


def try_secoc_key_recovery(
    payloads: List[bytes],
    mac_start: int,
    mac_end: int,
    freshness_start: int,
    freshness_end: int,
    data_id: Optional[int] = None,
    ecu_serial: Optional[bytes] = None,
    identifier: int = 0,
) -> KeyRecoveryResult:
    """Try to recover the SecOC MAC key using common/weak keys and known-plaintext analysis.

    Tests candidate keys by computing MACs over the authentic payload (bytes before mac_start) and comparing
    with the observed truncated MAC. Uses HMAC-SHA-256 as the MAC algorithm (standing in for CMAC-AES-128,
    as the analysis tools do).
    """
    findings: List[str] = []
    mac_bytes = mac_end - mac_start
    if mac_bytes <= 0 or not payloads:
        return KeyRecoveryResult(identifier, None, None, 0, 'HMAC-SHA-256', 0.0, 0, ['Invalid parameters'])

    candidates: List[Tuple[bytes, str]] = list(_COMMON_KEYS)

    if data_id is not None:
        did_key = (data_id & 0xFFFF).to_bytes(2, 'big') * 8
        candidates.append((did_key, 'data_id_derived'))
        did_key_4 = (data_id & 0xFFFFFFFF).to_bytes(4, 'big') * 4
        candidates.append((did_key_4, 'data_id_derived_32'))

    if ecu_serial is not None:
        padded = (ecu_serial * ((16 // len(ecu_serial)) + 1))[:16]
        candidates.append((padded, 'serial_derived'))

    test_frames = payloads[: min(50, len(payloads))]
    attempts = 0

    for key, source in candidates:
        attempts += 1
        matches = 0
        for payload in test_frames:
            if mac_end > len(payload):
                continue
            authentic = bytes(payload[:mac_start])
            observed_mac = payload[mac_start:mac_end]
            computed = _hmac_mac(key, authentic, mac_bytes)
            if computed == observed_mac:
                matches += 1

        if matches >= max(1, len(test_frames) * 0.8):
            confidence = matches / len(test_frames) if test_frames else 0.0
            findings.append(f'Key recovered ({source}): {key.hex()}, verified on {matches}/{len(test_frames)} frames')
            return KeyRecoveryResult(
                identifier=identifier,
                recovered_key=key,
                key_source=source,
                attempts=attempts,
                mac_algorithm='HMAC-SHA-256',
                confidence=round(confidence, 3),
                verified_frames=matches,
                findings=findings,
            )

    findings.append(f'No key recovered from {attempts} candidates')
    return KeyRecoveryResult(
        identifier=identifier,
        recovered_key=None,
        key_source=None,
        attempts=attempts,
        mac_algorithm='HMAC-SHA-256',
        confidence=0.0,
        verified_frames=0,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 2.5 Cross-PDU key sharing detection
# ---------------------------------------------------------------------------


@dataclass
class SharedKeyFinding:
    """A group of identifiers that share the same MAC key."""

    identifiers: List[int]
    shared_key: Optional[bytes]
    key_source: str
    verified_frames: int
    findings: List[str] = field(default_factory=list)


@dataclass
class SharedKeyDetection:
    """Result of detecting shared MAC keys across multiple PDUs."""

    groups: List[SharedKeyFinding]
    total_identifiers: int
    shared_count: int
    findings: List[str] = field(default_factory=list)


def detect_shared_secoc_keys(
    per_id_payloads: Dict[int, List[bytes]],
    per_id_mac_range: Dict[int, Tuple[int, int]],
    per_id_freshness_range: Dict[int, Tuple[int, int]],
) -> SharedKeyDetection:
    """Detect whether multiple SecOC-protected PDUs share the same MAC key.

    For each identifier, tries key recovery. Then checks whether any recovered key also verifies
    MACs on other identifiers.
    """
    findings: List[str] = []
    total = len(per_id_payloads)
    if total == 0:
        return SharedKeyDetection([], 0, 0, ['No identifiers provided'])

    recovered: Dict[int, Tuple[bytes, str]] = {}
    for can_id, payloads in per_id_payloads.items():
        mac_range = per_id_mac_range.get(can_id)
        fresh_range = per_id_freshness_range.get(can_id)
        if mac_range is None or fresh_range is None:
            continue
        result = try_secoc_key_recovery(
            payloads, mac_range[0], mac_range[1], fresh_range[0], fresh_range[1], identifier=can_id
        )
        if result.recovered_key is not None:
            recovered[can_id] = (result.recovered_key, result.key_source or 'unknown')

    if not recovered:
        findings.append('No keys recovered from any identifier')
        return SharedKeyDetection([], total, 0, findings)

    key_to_ids: Dict[bytes, List[Tuple[int, str, int]]] = {}
    for _src_id, (key, source) in recovered.items():
        if key not in key_to_ids:
            key_to_ids[key] = []

        for can_id, payloads in per_id_payloads.items():
            mac_range = per_id_mac_range.get(can_id)
            if mac_range is None:
                continue
            mac_start, mac_end = mac_range
            mac_bytes = mac_end - mac_start
            if mac_bytes <= 0:
                continue

            test_frames = payloads[: min(20, len(payloads))]
            verified = 0
            for payload in test_frames:
                if mac_end > len(payload):
                    continue
                authentic = bytes(payload[:mac_start])
                observed_mac = payload[mac_start:mac_end]
                computed = _hmac_mac(key, authentic, mac_bytes)
                if computed == observed_mac:
                    verified += 1

            if verified >= max(1, len(test_frames) * 0.8):
                already = any(cid == can_id for cid, _, _ in key_to_ids[key])
                if not already:
                    key_to_ids[key].append((can_id, source, verified))

    groups: List[SharedKeyFinding] = []
    shared_ids: set = set()
    for key, entries in key_to_ids.items():
        if len(entries) >= 2:
            ids = [e[0] for e in entries]
            total_verified = sum(e[2] for e in entries)
            source = entries[0][1]
            group_findings = [
                f'Key {key.hex()} ({source}) verified on {len(ids)} identifiers: '
                + ', '.join(f'0x{i:03X}' for i in ids)
            ]
            groups.append(
                SharedKeyFinding(
                    identifiers=ids,
                    shared_key=key,
                    key_source=source,
                    verified_frames=total_verified,
                    findings=group_findings,
                )
            )
            shared_ids.update(ids)

    if groups:
        findings.append(f'{len(shared_ids)} identifiers share keys across {len(groups)} group(s)')
        findings.append('Shared keys mean compromising one PDU exposes all PDUs in the same group')
    else:
        findings.append('No shared keys detected across identifiers')

    return SharedKeyDetection(
        groups=groups,
        total_identifiers=total,
        shared_count=len(shared_ids),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 2.6 Freshness prediction and replay window
# ---------------------------------------------------------------------------


@dataclass
class FreshnessPrediction:
    """Predicted freshness counter value at a future time."""

    identifier: int
    current_counter: int
    predicted_counter: int
    prediction_time: float
    counter_rate: float
    counter_width_bits: int
    wrap_time_seconds: float
    confidence: float
    findings: List[str] = field(default_factory=list)


def predict_freshness(
    payloads: List[bytes],
    timestamps: List[float],
    freshness_start: int,
    freshness_end: int,
    prediction_time: float = 10.0,
    identifier: int = 0,
) -> FreshnessPrediction:
    """Predict the freshness counter value at a future time by fitting a linear model to observed values."""
    findings: List[str] = []
    counter_width = freshness_end - freshness_start
    counter_bits = counter_width * 8
    max_counter = (1 << counter_bits) - 1

    if not payloads or not timestamps or counter_width <= 0:
        return FreshnessPrediction(
            identifier, 0, 0, prediction_time, 0.0, counter_bits, 0.0, 0.0, ['Insufficient data']
        )

    counters: List[int] = []
    times: List[float] = []
    for payload, ts in zip(payloads, timestamps, strict=False):
        if freshness_end <= len(payload):
            val = int.from_bytes(payload[freshness_start:freshness_end], 'big')
            counters.append(val)
            times.append(ts)

    if len(counters) < 3:
        return FreshnessPrediction(
            identifier, 0, 0, prediction_time, 0.0, counter_bits, 0.0, 0.0, ['Fewer than 3 counter samples']
        )

    # Unwrap counter values to handle wraps
    unwrapped: List[float] = [float(counters[0])]
    wrap_offset = 0.0
    for i in range(1, len(counters)):
        diff = counters[i] - counters[i - 1]
        if diff < -(max_counter // 2):
            wrap_offset += max_counter + 1
        unwrapped.append(float(counters[i]) + wrap_offset)

    # Linear regression: unwrapped_counter = rate * time + intercept
    n = len(times)
    t0 = times[0]
    dt = [t - t0 for t in times]
    mean_t = sum(dt) / n
    mean_c = sum(unwrapped) / n

    ss_tt = sum((t - mean_t) ** 2 for t in dt)
    if ss_tt < 1e-12:
        rate = 0.0
    else:
        ss_tc = sum((dt[i] - mean_t) * (unwrapped[i] - mean_c) for i in range(n))
        rate = ss_tc / ss_tt

    intercept = mean_c - rate * mean_t

    ss_res = sum((unwrapped[i] - (rate * dt[i] + intercept)) ** 2 for i in range(n))
    ss_tot = sum((unwrapped[i] - mean_c) ** 2 for i in range(n))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0

    current_counter = counters[-1]
    last_time = times[-1]

    predicted_raw = rate * (last_time - t0 + prediction_time) + intercept
    predicted_counter = int(predicted_raw) % (max_counter + 1)

    wrap_time = (max_counter + 1) / rate if rate > 0 else float('inf')

    confidence = max(0.0, min(1.0, r_squared))

    findings.append(f'Counter rate: {rate:.2f} increments/second')
    findings.append(f'Current counter: {current_counter}, predicted at +{prediction_time}s: {predicted_counter}')
    if wrap_time < float('inf'):
        findings.append(f'Counter wraps every {wrap_time:.1f} seconds')
    else:
        findings.append('Counter rate is zero or negative: no wrapping observed')
    findings.append(f'Linear fit R-squared: {r_squared:.4f}')

    return FreshnessPrediction(
        identifier=identifier,
        current_counter=current_counter,
        predicted_counter=predicted_counter,
        prediction_time=prediction_time,
        counter_rate=round(rate, 4),
        counter_width_bits=counter_bits,
        wrap_time_seconds=round(wrap_time, 2) if wrap_time < float('inf') else float('inf'),
        confidence=round(confidence, 3),
        findings=findings,
    )


@dataclass
class ReplayWindowResult:
    """The replay window for a SecOC-protected frame."""

    identifier: int
    window_seconds: float
    window_frames: int
    counter_tolerance: int
    effective_replay_rate: float
    findings: List[str] = field(default_factory=list)


def replay_window(
    payloads: List[bytes],
    timestamps: List[float],
    freshness_start: int,
    freshness_end: int,
    identifier: int = 0,
) -> ReplayWindowResult:
    """Compute the replay window: the time range during which a captured frame can be replayed.

    The receiver typically accepts frames whose freshness counter is within a small tolerance of the
    expected value. This function estimates that tolerance from the observed counter increments and
    frame rate.
    """
    findings: List[str] = []
    counter_width = freshness_end - freshness_start
    counter_bits = counter_width * 8

    if not payloads or not timestamps or counter_width <= 0 or len(payloads) < 3:
        return ReplayWindowResult(identifier, 0.0, 0, 0, 0.0, ['Insufficient data'])

    counters: List[int] = []
    times: List[float] = []
    max_counter = (1 << counter_bits) - 1
    for payload, ts in zip(payloads, timestamps, strict=False):
        if freshness_end <= len(payload):
            val = int.from_bytes(payload[freshness_start:freshness_end], 'big')
            counters.append(val)
            times.append(ts)

    if len(counters) < 3:
        return ReplayWindowResult(identifier, 0.0, 0, 0, 0.0, ['Fewer than 3 counter samples'])

    increments: List[int] = []
    intervals: List[float] = []
    for i in range(1, len(counters)):
        diff = counters[i] - counters[i - 1]
        if diff < 0:
            diff += max_counter + 1
        increments.append(diff)
        dt = times[i] - times[i - 1]
        if dt > 0:
            intervals.append(dt)

    if not intervals:
        return ReplayWindowResult(identifier, 0.0, 0, 0, 0.0, ['No valid intervals'])

    mean_interval = sum(intervals) / len(intervals)
    frame_rate = 1.0 / mean_interval if mean_interval > 0 else 0.0

    mean_increment = sum(increments) / len(increments) if increments else 1.0
    counter_rate = mean_increment / mean_interval if mean_interval > 0 else 0.0

    # Estimate tolerance as the max observed increment plus one
    tolerance = max(increments) + 1 if increments else 1

    window_seconds = tolerance / counter_rate if counter_rate > 0 else 0.0
    window_frames = int(tolerance / mean_increment) if mean_increment > 0 else 0

    effective_rate = window_frames / window_seconds if window_seconds > 0 else 0.0

    findings.append(f'Frame rate: {frame_rate:.1f} fps')
    findings.append(f'Counter rate: {counter_rate:.2f} increments/second')
    findings.append(f'Estimated receiver tolerance: {tolerance} counter values')
    findings.append(f'Replay window: {window_seconds * 1000:.0f} ms ({window_frames} frames)')
    if window_seconds > 1.0:
        findings.append('WARNING: replay window exceeds 1 second, frames may be replayable')
    elif window_seconds > 0.1:
        findings.append('Replay window is moderate: tight timing required for replay')
    else:
        findings.append('Replay window is very short: replay is difficult')

    return ReplayWindowResult(
        identifier=identifier,
        window_seconds=round(window_seconds, 4),
        window_frames=window_frames,
        counter_tolerance=tolerance,
        effective_replay_rate=round(effective_rate, 2),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 2.9 AUTOSAR SecOC profile classifier
# ---------------------------------------------------------------------------


@dataclass
class SecOcProfileClassification:
    """Classification of the AUTOSAR SecOC profile from observed parameters."""

    identifier: int
    profile: str
    mac_bits: int
    freshness_bits: int
    likely_algorithm: str
    autosar_compliant: bool
    findings: List[str] = field(default_factory=list)


_SECOC_PROFILES = [
    ('Profile1', 24, 4, 'CMAC-AES-128'),
    ('Profile2', 28, 8, 'CMAC-AES-128'),
    ('Profile3', 64, 32, 'CMAC-AES-128'),
]


def classify_secoc_profile(
    mac_bits: int,
    freshness_bits: int,
    payload_length: int,
    algorithm_hint: Optional[str] = None,
    identifier: int = 0,
) -> SecOcProfileClassification:
    """Classify the AUTOSAR SecOC profile from observed MAC and freshness parameters.

    Matches against known AUTOSAR profiles (Profile 1-3) and reports whether the configuration
    is standard or custom.
    """
    findings: List[str] = []
    best_profile = 'Custom'
    best_algorithm = algorithm_hint or 'CMAC-AES-128'
    autosar_compliant = False

    for profile_name, expected_mac, expected_fresh, algorithm in _SECOC_PROFILES:
        if mac_bits == expected_mac and freshness_bits == expected_fresh:
            best_profile = profile_name
            best_algorithm = algorithm
            autosar_compliant = True
            findings.append(
                f'Exact match: AUTOSAR {profile_name} '
                f'(MAC {expected_mac} bits, freshness {expected_fresh} bits, {algorithm})'
            )
            break

    if best_profile == 'Custom':
        for profile_name, expected_mac, expected_fresh, _algorithm in _SECOC_PROFILES:
            if abs(mac_bits - expected_mac) <= 8 and abs(freshness_bits - expected_fresh) <= 4:
                findings.append(
                    f'Near match: AUTOSAR {profile_name} '
                    f'(expected MAC {expected_mac} bits, freshness {expected_fresh} bits)'
                )
                break

        if mac_bits >= 24 and freshness_bits >= 4:
            autosar_compliant = True
            findings.append(
                'Configuration meets minimum AUTOSAR SecOC requirements (MAC >= 24 bits, freshness >= 4 bits)'
            )
        else:
            findings.append('Configuration does NOT meet minimum AUTOSAR SecOC requirements')

    if payload_length > 8 and mac_bits >= 64:
        if best_profile == 'Custom':
            findings.append('CAN FD with >= 64-bit MAC: may be Profile 3 or a custom CAN FD profile')
        if mac_bits == 128:
            findings.append('Full 128-bit MAC: maximum security, no truncation weakness')
            best_algorithm = algorithm_hint or 'CMAC-AES-128'

    if mac_bits < 24:
        findings.append(f'MAC truncation ({mac_bits} bits) below AUTOSAR safety minimum (24 bits)')
    elif mac_bits < 28:
        findings.append(f'MAC truncation ({mac_bits} bits) acceptable for body domain, below chassis minimum (28 bits)')
    elif mac_bits < 32:
        findings.append(f'MAC truncation ({mac_bits} bits) acceptable for chassis, below powertrain minimum (32 bits)')

    return SecOcProfileClassification(
        identifier=identifier,
        profile=best_profile,
        mac_bits=mac_bits,
        freshness_bits=freshness_bits,
        likely_algorithm=best_algorithm,
        autosar_compliant=autosar_compliant,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 2.10 E2E + SecOC interaction analysis
# ---------------------------------------------------------------------------


@dataclass
class E2eSecOcInteraction:
    """Analysis of E2E and SecOC counter interaction on the same PDU."""

    identifier: int
    has_e2e: bool
    has_secoc: bool
    e2e_counter_range: Optional[Tuple[int, int]]
    secoc_freshness_range: Optional[Tuple[int, int]]
    counters_independent: Optional[bool]
    shared_counter_risk: bool
    correlation: Optional[float]
    findings: List[str] = field(default_factory=list)


def analyse_e2e_secoc_interaction(
    payloads: List[bytes],
    e2e_counter_start: int,
    e2e_counter_end: int,
    secoc_freshness_start: int,
    secoc_freshness_end: int,
    identifier: int = 0,
) -> E2eSecOcInteraction:
    """Analyse whether E2E and SecOC counters on the same PDU are independent.

    If both counters are present and correlated, the security margin is lower than it appears because
    the counter is not an independent nonce for SecOC.
    """
    findings: List[str] = []

    if not payloads:
        return E2eSecOcInteraction(identifier, False, False, None, None, None, False, None, ['No payloads provided'])

    e2e_values: List[int] = []
    secoc_values: List[int] = []
    for payload in payloads:
        if e2e_counter_end <= len(payload) and secoc_freshness_end <= len(payload):
            e2e_val = int.from_bytes(payload[e2e_counter_start:e2e_counter_end], 'big')
            secoc_val = int.from_bytes(payload[secoc_freshness_start:secoc_freshness_end], 'big')
            e2e_values.append(e2e_val)
            secoc_values.append(secoc_val)

    if len(e2e_values) < 5:
        return E2eSecOcInteraction(
            identifier,
            True,
            True,
            (e2e_counter_start, e2e_counter_end),
            (secoc_freshness_start, secoc_freshness_end),
            None,
            False,
            None,
            ['Fewer than 5 valid samples'],
        )

    transitions = len(e2e_values) - 1
    e2e_mono = sum(1 for i in range(1, len(e2e_values)) if e2e_values[i] >= e2e_values[i - 1])
    secoc_mono = sum(1 for i in range(1, len(secoc_values)) if secoc_values[i] >= secoc_values[i - 1])
    has_e2e = e2e_mono / transitions >= 0.7 if transitions > 0 else False
    has_secoc = secoc_mono / transitions >= 0.7 if transitions > 0 else False

    if not has_e2e:
        findings.append('E2E counter range does not appear monotonic: may not be an alive counter')
    if not has_secoc:
        findings.append('SecOC freshness range does not appear monotonic: may not be a freshness counter')

    n = len(e2e_values)
    mean_e2e = sum(e2e_values) / n
    mean_secoc = sum(secoc_values) / n

    cov = sum((e2e_values[i] - mean_e2e) * (secoc_values[i] - mean_secoc) for i in range(n)) / n
    var_e2e = sum((v - mean_e2e) ** 2 for v in e2e_values) / n
    var_secoc = sum((v - mean_secoc) ** 2 for v in secoc_values) / n

    denom = math.sqrt(var_e2e * var_secoc)
    correlation = cov / denom if denom > 1e-12 else 0.0

    identical_count = sum(1 for i in range(n) if e2e_values[i] == secoc_values[i])
    identical_ratio = identical_count / n

    high_correlation = abs(correlation) > 0.9
    if identical_ratio > 0.9:
        counters_independent = False
        shared_counter_risk = True
        findings.append(
            f'Counters are identical in {identical_ratio * 100:.0f}% of frames: almost certainly the same counter'
        )
        findings.append('RISK: shared counter means SecOC freshness is not an independent nonce')
    elif high_correlation:
        counters_independent = False
        shared_counter_risk = True
        findings.append(f'Counters are highly correlated (r={correlation:.3f}): likely derived from the same source')
        findings.append('RISK: correlated counters reduce the effective security margin')
    elif abs(correlation) > 0.5:
        counters_independent = None
        shared_counter_risk = True
        findings.append(f'Counters show moderate correlation (r={correlation:.3f}): independence is uncertain')
    else:
        counters_independent = True
        shared_counter_risk = False
        findings.append(f'Counters appear independent (r={correlation:.3f}): E2E and SecOC use separate nonces')

    if has_e2e and has_secoc and counters_independent is True:
        findings.append('Both E2E and SecOC protections are active with independent counters: strong protection')
    elif has_e2e and has_secoc and not counters_independent:
        findings.append(
            'Both protections active but counters are NOT independent: '
            'the actual security margin is lower than the sum of their bit widths'
        )

    return E2eSecOcInteraction(
        identifier=identifier,
        has_e2e=has_e2e,
        has_secoc=has_secoc,
        e2e_counter_range=(e2e_counter_start, e2e_counter_end),
        secoc_freshness_range=(secoc_freshness_start, secoc_freshness_end),
        counters_independent=counters_independent,
        shared_counter_risk=shared_counter_risk,
        correlation=round(correlation, 4) if correlation is not None else None,
        findings=findings,
    )
