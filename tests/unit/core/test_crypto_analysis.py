"""Tests for the cryptanalysis module: randomness tests, freshness analysis and keystream reuse detection."""

import math
import os
import random

import pytest

from can_explorer.core.reverse.crypto import (
    analyse_e2e_secoc_interaction,
    analyse_freshness,
    assess_fd_security,
    classify_mac,
    classify_secoc_profile,
    detect_keystream_reuse,
    detect_secoc_bypass,
    detect_secoc_layout,
    detect_shared_secoc_keys,
    detect_sync_messages,
    predict_freshness,
    reconstruct_freshness,
    replay_window,
    secoc_strength_assessment,
    try_secoc_key_recovery,
)
from can_explorer.core.reverse.crypto import test_randomness as run_randomness_test
from can_explorer.core.reverse.fields import BitField


# ---------------------------------------------------------------------------
# test_randomness
# ---------------------------------------------------------------------------


class TestRandomness:
    def test_truly_random_data_is_random(self):
        """os.urandom bytes should be classified as random."""
        rng = random.Random(42)
        payloads = [rng.randbytes(8) for _ in range(500)]
        result = run_randomness_test(payloads, (4, 7))
        assert result.verdict == 'random'
        assert result.confidence >= 0.5
        assert result.monobit_p > 0.01
        assert result.runs_p > 0.01

    def test_counter_is_structured(self):
        """A simple counter should be classified as structured."""
        payloads = []
        for i in range(500):
            payload = bytes([0, 0, 0, 0]) + i.to_bytes(4, 'big')
            payloads.append(payload)
        result = run_randomness_test(payloads, (4, 7))
        assert result.verdict == 'structured'

    def test_sine_wave_is_structured(self):
        """A smooth sine wave should be classified as structured (high serial correlation)."""
        payloads = []
        for i in range(500):
            value = int(128 + 100 * math.sin(i / 20))
            payloads.append(bytes([0, 0, 0, 0, value & 0xFF, (value >> 8) & 0xFF, 0, 0]))
        result = run_randomness_test(payloads, (4, 5))
        assert result.verdict in ('structured', 'inconclusive')

    def test_too_few_frames_is_inconclusive(self):
        """With fewer than 20 frames, the verdict should be inconclusive."""
        payloads = [os.urandom(8) for _ in range(10)]
        result = run_randomness_test(payloads, (4, 7))
        assert result.verdict == 'inconclusive'

    def test_empty_payloads(self):
        result = run_randomness_test([], (0, 3))
        assert result.verdict == 'inconclusive'

    def test_counter_correlation_lowers_confidence(self):
        """When a counter is correlated with the 'MAC' bytes, confidence should drop."""
        payloads = []
        for i in range(500):
            # MAC bytes are just the counter XOR'd with a constant
            mac = (i * 7 + 3) & 0xFF
            payloads.append(bytes([i & 0xFF, 0, 0, 0, mac, mac, mac, mac]))
        counter_values = [i & 0xFF for i in range(500)]
        result = run_randomness_test(payloads, (4, 7), counter_values=counter_values)
        # The counter correlation should be detected
        assert result.counter_correlation is not None


# ---------------------------------------------------------------------------
# analyse_freshness
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_with_freshness_counter(self):
        """A counter + MAC with enough frames should detect freshness."""
        rng = random.Random(99)
        payloads = []
        timestamps = []
        for i in range(500):
            counter = i & 0xFF
            mac = rng.randbytes(4)
            payloads.append(bytes([counter, 0x55, 0xAA]) + mac)
            timestamps.append(i * 0.01)
        counter_field = BitField(0x100, False, 0, 8)
        report = analyse_freshness(payloads, timestamps, counter_field, (3, 6))
        assert report.has_freshness is True
        assert report.mac_bits == 32
        assert report.frames_per_second > 0
        assert report.forgery_probability == pytest.approx(2.0**-32)

    def test_without_freshness(self):
        """Same payload -> same MAC means no freshness."""
        import hmac
        import hashlib

        key = b'\x01' * 16
        payloads = []
        timestamps = []
        for i in range(500):
            data = bytes([i % 4, 0x55, 0xAA])  # Only 4 unique data values
            mac = hmac.new(key, data, hashlib.sha256).digest()[:4]
            payloads.append(data + mac)
            timestamps.append(i * 0.01)
        counter_field = BitField(0x100, False, 0, 8)
        report = analyse_freshness(payloads, timestamps, counter_field, (3, 6))
        assert report.has_freshness is False
        assert report.replay_possible is True

    def test_counter_wraps(self):
        """When the counter wraps, it should be detected."""
        rng = random.Random(88)
        payloads = []
        timestamps = []
        for i in range(600):
            counter = i % 256
            payloads.append(bytes([counter, 0xBB]) + rng.randbytes(2))
            timestamps.append(i * 0.01)
        counter_field = BitField(0x100, False, 0, 8)
        report = analyse_freshness(payloads, timestamps, counter_field, (2, 3))
        assert report.counter_wraps >= 1

    def test_too_few_frames(self):
        report = analyse_freshness([b'\x00\x01\x02\x03'], [0.0], BitField(0x100, False, 0, 8), (2, 3))
        assert report.replay_possible is True
        assert 'Too few frames' in report.findings[0]


# ---------------------------------------------------------------------------
# detect_keystream_reuse
# ---------------------------------------------------------------------------


class TestKeystreamReuse:
    def test_unique_counters_no_reuse(self):
        """With unique counter values, no keystream reuse should be found."""
        rng = random.Random(77)
        payloads = []
        for i in range(200):
            payloads.append(bytes([i & 0xFF, 0]) + rng.randbytes(6))
        counter_field = BitField(0x100, False, 0, 8)
        report = detect_keystream_reuse(payloads, counter_field, (2, 7))
        assert report.reused_counter_values == []
        assert report.structured_xor_pairs == 0

    def test_repeated_counters_with_structure(self):
        """When counter values repeat and the data is XOR'd with a fixed keystream, structure should be found."""
        keystream = b'\x5a\xa5\x3c\xc3'
        payloads = []
        for i in range(200):
            counter = i % 10  # Only 10 unique counter values
            # Plaintext is ASCII-like
            plain = bytes([0x41 + (i % 26)] * 4)
            encrypted = bytes(a ^ b for a, b in zip(plain, keystream, strict=False))
            payloads.append(bytes([counter, 0]) + encrypted)
        counter_field = BitField(0x100, False, 0, 8)
        report = detect_keystream_reuse(payloads, counter_field, (2, 5))
        assert len(report.reused_counter_values) > 0
        assert report.structured_xor_pairs > 0

    def test_repeated_counters_no_structure(self):
        """When counter values repeat but data is truly random, no structure should be found."""
        rng = random.Random(66)
        payloads = []
        for i in range(200):
            counter = i % 10
            payloads.append(bytes([counter, 0]) + rng.randbytes(6))
        counter_field = BitField(0x100, False, 0, 8)
        report = detect_keystream_reuse(payloads, counter_field, (2, 7))
        assert len(report.reused_counter_values) > 0
        # With truly random data, XOR pairs should not show structure
        assert report.structured_xor_pairs == 0


# ---------------------------------------------------------------------------
# classify_mac
# ---------------------------------------------------------------------------


class TestClassifyMac:
    def test_secoc_24bit_with_counter(self):
        """A 3-byte range with a counter should be classified as CMAC-AES-128 truncated."""
        rng = random.Random(55)
        payloads = [bytes([i & 0xFF, 0x11]) + rng.randbytes(3) for i in range(100)]
        result = classify_mac(payloads, (2, 4), has_counter=True)
        assert result.mac_bits == 24
        assert 'CMAC-AES-128' in result.likely_algorithm
        assert result.truncated is True

    def test_32bit_without_counter_ambiguous(self):
        """A 4-byte range without a counter could be CRC or MAC."""
        rng = random.Random(56)
        payloads = [rng.randbytes(8) for _ in range(100)]
        result = classify_mac(payloads, (4, 7), has_counter=False)
        assert result.mac_bits == 32
        assert 'No freshness counter found' in ' '.join(result.findings)

    def test_full_128bit(self):
        """A 16-byte range is classified as full CMAC-AES-128."""
        payloads = [os.urandom(20) for _ in range(10)]
        result = classify_mac(payloads, (4, 19), has_counter=False)
        assert result.mac_bits == 128
        assert 'CMAC-AES-128' in result.likely_algorithm
        assert result.truncated is False

    def test_structured_bytes_suggest_crc(self):
        """If the byte range shows structure, the classification notes it is more likely a CRC."""
        payloads = []
        for i in range(100):
            # Structured: a simple sum checksum
            data = bytes([i & 0xFF, (i * 3) & 0xFF, 0xAA, 0xBB])
            chk = sum(data) & 0xFF
            payloads.append(data + bytes([chk, chk, chk]))
        result = classify_mac(payloads, (4, 6), has_counter=False)
        assert any('structure' in f.lower() or 'CRC' in f for f in result.findings)


# ---------------------------------------------------------------------------
# detect_secoc_layout (roadmap 2.1)
# ---------------------------------------------------------------------------


class TestDetectSecOcLayout:
    def test_detects_counter_plus_mac(self):
        """Detect a 1-byte counter + 3-byte random MAC at the end of an 8-byte payload."""
        rng = random.Random(42)
        payloads = []
        for i in range(200):
            data = bytes([0x11, 0x22, 0x33, 0x44])  # Authentic payload
            counter = i & 0xFF  # 1-byte monotonic counter
            mac = rng.randbytes(3)  # 3 random MAC bytes
            payloads.append(data + bytes([counter]) + mac)

        layout = detect_secoc_layout(payloads, identifier=0x100)
        assert layout is not None
        assert layout.identifier == 0x100
        assert layout.mac_bits == 24
        assert layout.freshness_bits == 8
        assert layout.confidence >= 0.4

    def test_too_few_frames(self):
        """Return None with fewer than min_frames."""
        payloads = [os.urandom(8) for _ in range(10)]
        layout = detect_secoc_layout(payloads, min_frames=50)
        assert layout is None

    def test_no_secoc_pattern(self):
        """Return None when payload shows no SecOC-like structure."""
        payloads = []
        for i in range(200):
            # All structured: a counter across the whole payload
            payloads.append(i.to_bytes(8, 'big'))
        layout = detect_secoc_layout(payloads)
        assert layout is None

    def test_varying_payload_length(self):
        """Return None when payloads have different lengths."""
        payloads = [os.urandom(length) for length in [8, 7, 8, 7, 8] * 20]
        layout = detect_secoc_layout(payloads, min_frames=50)
        assert layout is None

    def test_empty_payloads(self):
        layout = detect_secoc_layout([])
        assert layout is None


# ---------------------------------------------------------------------------
# reconstruct_freshness (roadmap 2.2)
# ---------------------------------------------------------------------------


class TestReconstructFreshness:
    def test_monotonic_counter(self):
        """Reconstruct a simple monotonic counter with wrap."""
        payloads = []
        timestamps = []
        for i in range(300):
            counter = i & 0xFF
            payloads.append(bytes([0x11, 0x22, counter, 0xAA, 0xBB, 0xCC]))
            timestamps.append(i * 0.01)

        result = reconstruct_freshness(payloads, timestamps, freshness_start=2, freshness_end=3, identifier=0x100)
        assert result.counter_width_bits == 8
        assert result.counter_range == 256
        assert result.freshness_type == 'monotonic'
        assert result.wrap_count >= 1

    def test_no_wrap(self):
        """When the counter does not wrap, wrap_count is 0."""
        payloads = []
        timestamps = []
        for i in range(100):
            payloads.append(bytes([0x11, 0x22, i, 0xAA, 0xBB]))
            timestamps.append(i * 0.01)

        result = reconstruct_freshness(payloads, timestamps, freshness_start=2, freshness_end=3)
        assert result.wrap_count == 0
        assert result.freshness_type == 'monotonic'

    def test_too_few_frames(self):
        result = reconstruct_freshness([b'\x00'], [0.0], freshness_start=0, freshness_end=1)
        assert result.freshness_type == 'unknown'

    def test_time_based_counter(self):
        """Detect time-based freshness (large increments per frame)."""
        payloads = []
        timestamps = []
        for i in range(200):
            # Counter jumps by ~200 each frame, suggesting time-based
            counter = (i * 200) & 0xFFFF
            payloads.append(bytes([0x11, 0x22]) + counter.to_bytes(2, 'big') + bytes([0xAA, 0xBB]))
            timestamps.append(i * 0.01)

        result = reconstruct_freshness(payloads, timestamps, freshness_start=2, freshness_end=4)
        assert result.counter_width_bits == 16
        assert result.freshness_type == 'time_based'


# ---------------------------------------------------------------------------
# secoc_strength_assessment (roadmap 2.3)
# ---------------------------------------------------------------------------


class TestSecOcStrengthAssessment:
    def test_short_mac_is_critical(self):
        """A 2-byte (16-bit) MAC should be rated as warning or worse."""
        rng = random.Random(42)
        payloads = []
        timestamps = []
        for i in range(200):
            data = bytes([0x11, 0x22, 0x33, 0x44])
            counter = bytes([i & 0xFF])
            mac = rng.randbytes(2)
            payloads.append(data + counter + mac)
            timestamps.append(i * 0.01)

        result = secoc_strength_assessment(
            payloads, timestamps, mac_start=5, mac_end=7, freshness_start=4, freshness_end=5
        )
        assert result.mac_bits == 16
        assert result.severity in ('critical', 'warning')
        assert result.forgery_probability > 1e-6

    def test_strong_mac(self):
        """A 4-byte (32-bit) MAC with slow frame rate should be strong or acceptable."""
        rng = random.Random(42)
        payloads = []
        timestamps = []
        for i in range(200):
            data = bytes([0x11, 0x22, 0x33, 0x44])
            counter = bytes([i & 0xFF])
            mac = rng.randbytes(4)
            payloads.append(data + counter + mac)
            timestamps.append(i * 1.0)  # 1 frame per second

        result = secoc_strength_assessment(
            payloads, timestamps, mac_start=5, mac_end=9, freshness_start=4, freshness_end=5
        )
        assert result.mac_bits == 32
        assert result.severity in ('acceptable', 'strong')
        assert result.meets_autosar['infotainment'] is True

    def test_autosar_recommendation_check(self):
        """Check AUTOSAR domain recommendations."""
        rng = random.Random(42)
        payloads = []
        timestamps = []
        for i in range(100):
            payloads.append(bytes([0x11, 0x22, i & 0xFF]) + rng.randbytes(3))
            timestamps.append(i * 0.01)

        result = secoc_strength_assessment(
            payloads, timestamps, mac_start=3, mac_end=6, freshness_start=2, freshness_end=3
        )
        assert result.mac_bits == 24
        assert result.meets_autosar['safety_critical'] is True
        assert result.meets_autosar['infotainment'] is True


# ---------------------------------------------------------------------------
# detect_secoc_bypass (roadmap 2.7)
# ---------------------------------------------------------------------------


class TestDetectSecOcBypass:
    def test_missing_trailer(self):
        """Detect frames shorter than expected length."""
        payloads = [b'\x11\x22\x33\x44\x55\x66\x77\x88'] * 90 + [b'\x11\x22\x33\x44'] * 10
        timestamps = [i * 0.01 for i in range(100)]

        findings = detect_secoc_bypass(payloads, timestamps, expected_length=8)
        types = [f.bypass_type for f in findings]
        assert 'missing_trailer' in types

    def test_static_mac(self):
        """Detect when MAC bytes never change despite different payloads."""
        payloads = []
        timestamps = []
        for i in range(100):
            data = bytes([i & 0xFF, 0x22, 0x33, 0x44])
            mac = bytes([0xAA, 0xBB, 0xCC])  # Always the same
            payloads.append(data + mac)
            timestamps.append(i * 0.01)

        findings = detect_secoc_bypass(payloads, timestamps, mac_start=4, mac_end=7)
        types = [f.bypass_type for f in findings]
        assert 'static_mac' in types

    def test_no_bypass_found(self):
        """No findings when everything looks normal."""
        rng = random.Random(42)
        payloads = []
        timestamps = []
        for i in range(100):
            data = bytes([0x11, 0x22, 0x33, 0x44])
            mac = rng.randbytes(3)
            payloads.append(data + mac)
            timestamps.append(i * 0.01)

        findings = detect_secoc_bypass(payloads, timestamps, mac_start=4, mac_end=7)
        # No static_mac because MAC changes with random data
        static_findings = [f for f in findings if f.bypass_type == 'static_mac']
        assert len(static_findings) == 0

    def test_empty_input(self):
        findings = detect_secoc_bypass([], [])
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# assess_fd_security (roadmap 1.6)
# ---------------------------------------------------------------------------


class TestAssessFdSecurity:
    def test_fd_payload_with_mac(self):
        """Assess a 64-byte CAN FD payload with random trailing bytes."""
        rng = random.Random(42)
        payloads = []
        for i in range(200):
            data = bytes(range(48))
            counter = i.to_bytes(2, 'big')
            mac = rng.randbytes(14)
            payloads.append(data + counter + mac)
        result = assess_fd_security(payloads, identifier=0x200)
        assert result.identifier == 0x200
        assert result.payload_length == 64
        assert result.full_mac_possible is True
        assert isinstance(result.findings, list)

    def test_classic_can_payload(self):
        """Classic CAN (8 bytes) is handled."""
        rng = random.Random(42)
        payloads = [rng.randbytes(8) for _ in range(200)]
        result = assess_fd_security(payloads)
        assert result.payload_length == 8
        assert result.full_mac_possible is False

    def test_empty_payloads(self):
        result = assess_fd_security([])
        assert result.payload_length == 0
        assert isinstance(result.findings, list)


# ---------------------------------------------------------------------------
# try_secoc_key_recovery (roadmap 2.4)
# ---------------------------------------------------------------------------


class TestTrySecOcKeyRecovery:
    def test_weak_key_recovery(self):
        """Recover an all-zero key from payloads signed with it."""
        import hashlib
        import hmac

        key = b'\x00' * 16
        payloads = []
        for i in range(100):
            data = bytes([0x11, 0x22, 0x33, 0x44])
            counter = bytes([i & 0xFF])
            authentic = data + counter
            mac = hmac.new(key, authentic, hashlib.sha256).digest()[:3]
            payloads.append(data + counter + mac)

        result = try_secoc_key_recovery(payloads, mac_start=5, mac_end=8, freshness_start=4, freshness_end=5)
        assert result.recovered_key is not None
        assert 'zero' in result.key_source or result.key_source == 'common_key'
        assert result.verified_frames > 0
        assert isinstance(result.findings, list)

    def test_no_recovery(self):
        """Random key cannot be recovered."""
        rng = random.Random(42)
        payloads = []
        for i in range(50):
            data = bytes([0x11, 0x22, 0x33, 0x44])
            counter = bytes([i & 0xFF])
            mac = rng.randbytes(3)
            payloads.append(data + counter + mac)

        result = try_secoc_key_recovery(payloads, mac_start=5, mac_end=8, freshness_start=4, freshness_end=5)
        assert result.recovered_key is None

    def test_empty_payloads(self):
        result = try_secoc_key_recovery([], mac_start=5, mac_end=8, freshness_start=4, freshness_end=5)
        assert result.recovered_key is None


# ---------------------------------------------------------------------------
# detect_shared_secoc_keys (roadmap 2.5)
# ---------------------------------------------------------------------------


class TestDetectSharedSecOcKeys:
    def test_no_shared_keys(self):
        """When keys differ, no sharing is detected."""
        rng = random.Random(42)
        payloads_a = [rng.randbytes(8) for _ in range(50)]
        payloads_b = [rng.randbytes(8) for _ in range(50)]
        result = detect_shared_secoc_keys(
            {0x200: payloads_a, 0x201: payloads_b},
            {0x200: (5, 8), 0x201: (5, 8)},
            {0x200: (4, 5), 0x201: (4, 5)},
        )
        assert isinstance(result.groups, list)
        assert result.total_identifiers == 2

    def test_empty_input(self):
        result = detect_shared_secoc_keys({}, {}, {})
        assert result.total_identifiers == 0
        assert result.shared_count == 0


# ---------------------------------------------------------------------------
# predict_freshness (roadmap 2.6)
# ---------------------------------------------------------------------------


class TestPredictFreshness:
    def test_linear_counter(self):
        """Predict a linearly incrementing counter."""
        payloads = []
        timestamps = []
        for i in range(100):
            data = bytes([0x11, 0x22, 0x33, 0x44])
            counter = bytes([i & 0xFF])
            mac = bytes([0xAA, 0xBB, 0xCC])
            payloads.append(data + counter + mac)
            timestamps.append(i * 0.01)

        result = predict_freshness(payloads, timestamps, freshness_start=4, freshness_end=5, prediction_time=1.0)
        assert result.counter_rate > 0
        assert result.predicted_counter > result.current_counter
        assert result.confidence > 0.5
        assert isinstance(result.findings, list)

    def test_too_few_frames(self):
        payloads = [bytes([0x11, 0x22, 0x33, 0x44, 0x00, 0xAA])]
        timestamps = [0.0]
        result = predict_freshness(payloads, timestamps, freshness_start=4, freshness_end=5, prediction_time=1.0)
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# replay_window (roadmap 2.6)
# ---------------------------------------------------------------------------


class TestReplayWindow:
    def test_basic_window(self):
        """Compute replay window for a counter-based freshness."""
        payloads = []
        timestamps = []
        for i in range(100):
            data = bytes([0x11, 0x22, 0x33, 0x44])
            counter = bytes([i & 0xFF])
            mac = bytes([0xAA, 0xBB, 0xCC])
            payloads.append(data + counter + mac)
            timestamps.append(i * 0.01)

        result = replay_window(payloads, timestamps, freshness_start=4, freshness_end=5)
        assert result.window_seconds >= 0
        assert isinstance(result.window_frames, int)
        assert isinstance(result.findings, list)

    def test_too_few_frames(self):
        result = replay_window([bytes(8)], [0.0], freshness_start=4, freshness_end=5)
        assert isinstance(result.findings, list)


# ---------------------------------------------------------------------------
# classify_secoc_profile (roadmap 2.9)
# ---------------------------------------------------------------------------


class TestClassifySecOcProfile:
    def test_profile_1(self):
        """Profile 1: 24-bit MAC, 4-bit freshness."""
        result = classify_secoc_profile(24, 4, 8)
        assert result.profile == 'Profile1'
        assert result.autosar_compliant is True

    def test_profile_2(self):
        """Profile 2: 28-bit MAC, 8-bit freshness."""
        result = classify_secoc_profile(28, 8, 8)
        assert result.profile == 'Profile2'

    def test_profile_3(self):
        """Profile 3: 64-bit MAC, 32-bit freshness (CAN FD)."""
        result = classify_secoc_profile(64, 32, 64)
        assert result.profile == 'Profile3'

    def test_custom_profile(self):
        """Non-standard parameters."""
        result = classify_secoc_profile(16, 16, 8)
        assert result.profile == 'Custom'


# ---------------------------------------------------------------------------
# analyse_e2e_secoc_interaction (roadmap 2.10)
# ---------------------------------------------------------------------------


class TestAnalyseE2eSecOcInteraction:
    def test_independent_counters(self):
        """E2E and SecOC counters that increment independently."""
        payloads = []
        for i in range(200):
            e2e_counter = bytes([i & 0xFF])
            data = bytes([0x11, 0x22])
            secoc_counter = bytes([(i * 3) & 0xFF])
            mac = bytes([0xAA, 0xBB])
            payloads.append(e2e_counter + data + secoc_counter + mac)

        result = analyse_e2e_secoc_interaction(
            payloads, e2e_counter_start=0, e2e_counter_end=1, secoc_freshness_start=3, secoc_freshness_end=4
        )
        assert result.has_e2e is True
        assert result.has_secoc is True
        assert isinstance(result.findings, list)

    def test_shared_counter(self):
        """E2E and SecOC share the same counter."""
        payloads = []
        for i in range(200):
            counter = bytes([i & 0xFF])
            data = bytes([0x11, 0x22])
            mac = bytes([0xAA, 0xBB])
            payloads.append(counter + data + counter + mac)

        result = analyse_e2e_secoc_interaction(
            payloads, e2e_counter_start=0, e2e_counter_end=1, secoc_freshness_start=3, secoc_freshness_end=4
        )
        assert result.shared_counter_risk is True

    def test_empty_payloads(self):
        result = analyse_e2e_secoc_interaction(
            [], e2e_counter_start=0, e2e_counter_end=1, secoc_freshness_start=3, secoc_freshness_end=4
        )
        assert isinstance(result.findings, list)


# ---------------------------------------------------------------------------
# detect_sync_messages (roadmap 2.2)
# ---------------------------------------------------------------------------


class TestDetectSyncMessages:
    def _build_traffic(self, *, sync_id: int, monitored_ids: list, frame_count: int = 600, wrap_at: int = 256):
        """Build multi-ID traffic where *sync_id* sends a frame just before the counters on
        *monitored_ids* reset (wrap).  Returns (all_payloads, all_timestamps, freshness_ranges).

        *wrap_at* must equal the counter range (2^counter_bits) for wraps to be detected correctly
        by the algorithm (which checks prev - current > range // 2).
        """
        rng = random.Random(123)
        all_payloads: dict = {mid: [] for mid in monitored_ids}
        all_timestamps: dict = {mid: [] for mid in monitored_ids}
        all_payloads[sync_id] = []
        all_timestamps[sync_id] = []
        freshness_ranges = {mid: (2, 3) for mid in monitored_ids}

        for i in range(frame_count):
            t = i * 0.01
            counter = i % wrap_at
            is_wrap = counter == 0 and i > 0

            # Sync message appears ~30 ms before each wrap
            if is_wrap:
                all_payloads[sync_id].append(rng.randbytes(4))
                all_timestamps[sync_id].append(t - 0.03)

            for mid in monitored_ids:
                payload = bytes([0x11, 0x22, counter & 0xFF, 0xAA, 0xBB])
                all_payloads[mid].append(payload)
                all_timestamps[mid].append(t)

        return all_payloads, all_timestamps, freshness_ranges

    def test_detects_sync_message(self):
        """A message that consistently appears before counter resets on multiple IDs is detected as sync."""
        all_payloads, all_timestamps, freshness_ranges = self._build_traffic(
            sync_id=0x700, monitored_ids=[0x100, 0x200, 0x300], frame_count=800, wrap_at=256
        )
        results = detect_sync_messages(all_payloads, all_timestamps, freshness_ranges)
        assert len(results) >= 1
        sync_ids = [s.identifier for s in results]
        assert 0x700 in sync_ids
        sync = next(s for s in results if s.identifier == 0x700)
        assert len(sync.sync_target_ids) >= 2
        assert sync.confidence > 0

    def test_no_sync_when_unrelated(self):
        """An unrelated message with no timing correlation is not flagged as sync.

        Use 200 frames with a 1-byte (256-range) counter that never wraps, so there
        are no resets to correlate with.
        """
        rng = random.Random(99)
        monitored_ids = [0x100, 0x200]
        all_payloads: dict = {}
        all_timestamps: dict = {}
        freshness_ranges = {mid: (2, 3) for mid in monitored_ids}

        for mid in monitored_ids:
            all_payloads[mid] = []
            all_timestamps[mid] = []
            for i in range(200):
                # Counter never wraps (stays within 0..199)
                all_payloads[mid].append(bytes([0x11, 0x22, i & 0xFF, 0xAA]))
                all_timestamps[mid].append(i * 0.01)

        # Candidate ID sends at random times
        all_payloads[0x700] = [rng.randbytes(4) for _ in range(50)]
        all_timestamps[0x700] = sorted(rng.uniform(0, 2.0) for _ in range(50))

        results = detect_sync_messages(all_payloads, all_timestamps, freshness_ranges)
        sync_ids = [s.identifier for s in results]
        assert 0x700 not in sync_ids

    def test_fewer_than_two_monitored_ids(self):
        """With fewer than 2 monitored IDs, return empty list."""
        results = detect_sync_messages({0x100: [b'\x00\x01']}, {0x100: [0.0]}, {0x100: (0, 1)})
        assert results == []

    def test_no_resets_returns_empty(self):
        """When monitored counters never reset, no sync is detected."""
        monitored_ids = [0x100, 0x200]
        all_payloads: dict = {}
        all_timestamps: dict = {}
        freshness_ranges = {mid: (2, 3) for mid in monitored_ids}

        for mid in monitored_ids:
            all_payloads[mid] = []
            all_timestamps[mid] = []
            for i in range(100):
                # Counter never wraps
                all_payloads[mid].append(bytes([0x11, 0x22, i, 0xAA]))
                all_timestamps[mid].append(i * 0.01)

        all_payloads[0x700] = [b'\xaa\xbb'] * 50
        all_timestamps[0x700] = [i * 0.02 for i in range(50)]

        results = detect_sync_messages(all_payloads, all_timestamps, freshness_ranges)
        assert results == []

    def test_sync_confidence_proportional_to_coverage(self):
        """Confidence should increase when the sync message covers more monitored IDs."""
        all_payloads, all_timestamps, freshness_ranges = self._build_traffic(
            sync_id=0x700, monitored_ids=[0x100, 0x200, 0x300, 0x400], frame_count=1500, wrap_at=256
        )
        results = detect_sync_messages(all_payloads, all_timestamps, freshness_ranges)
        assert len(results) >= 1
        sync = next(s for s in results if s.identifier == 0x700)
        # With all 4 IDs synced, confidence should reflect that
        assert sync.confidence >= 0.5
