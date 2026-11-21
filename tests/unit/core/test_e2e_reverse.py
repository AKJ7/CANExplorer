"""Tests for the AUTOSAR E2E reverse engineering module (core/reverse/e2e_reverse.py).

Generates synthetic frames with known E2E profiles using the existing Profile*.protect() methods, then verifies
that detection, Data ID recovery, alive counter detection and bypass detection work correctly.
"""

import pytest

from can_explorer.core.e2e import Profile2, Profile4, Profile5, Profile6, Profile7, Profile22
from can_explorer.core.reverse.e2e_reverse import (
    AliveCounterResult,
    E2eBypassFinding,
    E2eProfileResult,
    compute_e2e_header,
    detect_alive_counter,
    detect_e2e,
    detect_e2e_bypass,
    detect_e2e_profile,
    recover_data_ids,
)


# ---------------------------------------------------------------------------
# Helpers: synthetic frame generation
# ---------------------------------------------------------------------------


def _make_profile2_frames(data_ids, n=100, payload_len=8):
    """Generate n frames protected with Profile 2."""
    profile = Profile2(data_ids)
    payloads = []
    for i in range(n):
        # Varying user data
        base = bytearray(payload_len)
        base[0] = 0  # CRC placeholder
        base[1] = 0  # Counter placeholder
        base[2] = (i * 7 + 3) & 0xFF
        base[3] = (i * 13 + 11) & 0xFF
        if payload_len > 4:
            base[4] = (i * 31 + 5) & 0xFF
        if payload_len > 5:
            base[5] = (i >> 2) & 0xFF
        counter = i % 16
        payloads.append(profile.protect(bytes(base), counter))
    return payloads


def _make_profile22_frames(data_ids, n=100, payload_len=8, offset=0):
    """Generate n frames protected with Profile 22."""
    profile = Profile22(data_ids, offset=offset)
    payloads = []
    for i in range(n):
        base = bytearray(payload_len)
        base[offset] = 0  # CRC placeholder
        base[offset + 1] = 0  # Counter placeholder
        for j in range(payload_len):
            if j != offset and j != offset + 1:
                base[j] = (i * (j + 3) + j) & 0xFF
        counter = i % 16
        payloads.append(profile.protect(bytes(base), counter))
    return payloads


def _make_profile5_frames(data_id, n=100, payload_len=8):
    """Generate n frames protected with Profile 5."""
    profile = Profile5(data_id)
    payloads = []
    for i in range(n):
        base = bytearray(payload_len)
        base[0] = 0  # CRC low placeholder
        base[1] = 0  # CRC high placeholder
        base[2] = 0  # Counter placeholder
        base[3] = (i * 7 + 1) & 0xFF
        if payload_len > 4:
            base[4] = (i * 13) & 0xFF
        if payload_len > 5:
            base[5] = (i * 3 + 7) & 0xFF
        counter = i % 256
        payloads.append(profile.protect(bytes(base), counter))
    return payloads


def _make_profile4_frames(data_id, n=100, payload_len=16):
    """Generate n frames protected with Profile 4."""
    profile = Profile4(data_id)
    payloads = []
    for i in range(n):
        base = bytearray(payload_len)
        # Header area (bytes 0-11) will be filled by protect()
        for j in range(12, payload_len):
            base[j] = (i * (j + 1) + j) & 0xFF
        counter = i % 65536
        payloads.append(profile.protect(bytes(base), counter))
    return payloads


def _make_profile6_frames(data_id, n=100, payload_len=12):
    """Generate n frames protected with Profile 6."""
    profile = Profile6(data_id)
    payloads = []
    for i in range(n):
        base = bytearray(payload_len)
        # Header area (bytes 0-4) will be filled by protect()
        for j in range(5, payload_len):
            base[j] = (i * (j + 2) + j) & 0xFF
        counter = i % 256
        payloads.append(profile.protect(bytes(base), counter))
    return payloads


def _make_profile7_frames(data_id, n=100, payload_len=24):
    """Generate n frames protected with Profile 7."""
    profile = Profile7(data_id)
    payloads = []
    for i in range(n):
        base = bytearray(payload_len)
        # Header area (bytes 0-19) will be filled by protect()
        for j in range(20, payload_len):
            base[j] = (i * (j + 3) + j) & 0xFF
        counter = i % (2**32)
        payloads.append(profile.protect(bytes(base), counter))
    return payloads


# ---------------------------------------------------------------------------
# Alive counter detection
# ---------------------------------------------------------------------------


class TestAliveCounter:
    def test_4bit_counter_low_nibble(self):
        """Profile 2 layout: counter is the low nibble of byte 1."""
        data_ids = list(range(10, 26))
        payloads = _make_profile2_frames(data_ids, n=100)
        counter = detect_alive_counter(payloads)
        assert counter is not None
        assert counter.position == 1
        assert counter.width == 4
        assert counter.modulus == 16
        assert not counter.nibble_high
        assert counter.increment_rate >= 0.8

    def test_8bit_counter(self):
        """Profile 5 layout: counter is byte 2 (8-bit)."""
        payloads = _make_profile5_frames(0x1234, n=300)
        counter = detect_alive_counter(payloads)
        assert counter is not None
        assert counter.position == 2
        assert counter.width == 8
        assert counter.modulus == 256
        assert counter.increment_rate >= 0.8

    def test_too_few_frames(self):
        payloads = [bytes(8)] * 5
        assert detect_alive_counter(payloads) is None

    def test_no_counter(self):
        """Random data should not look like a counter."""
        import hashlib

        payloads = [hashlib.sha256(i.to_bytes(4, 'big')).digest()[:8] for i in range(100)]
        counter = detect_alive_counter(payloads)
        # It may still pick up something by chance, but it should not be confident
        if counter is not None:
            assert counter.confidence < 0.6

    def test_counter_with_gaps(self):
        """Counter that skips values (missed frames) should still be detected."""
        data_ids = list(range(16))
        profile = Profile2(data_ids)
        payloads = []
        counter_val = 0
        for i in range(100):
            base = bytearray(8)
            base[2] = (i * 7) & 0xFF
            base[3] = (i * 3) & 0xFF
            payloads.append(profile.protect(bytes(base), counter_val))
            # Skip some counter values to simulate missed frames
            counter_val = (counter_val + (2 if i % 7 == 0 else 1)) % 16
        counter = detect_alive_counter(payloads)
        assert counter is not None
        assert counter.position == 1
        assert counter.width == 4


# ---------------------------------------------------------------------------
# Profile detection
# ---------------------------------------------------------------------------


class TestProfileDetection:
    def test_profile2(self):
        data_ids = [0x2A, 0x3B, 0x4C, 0x5D, 0x6E, 0x7F, 0x80, 0x91, 0xA2, 0xB3, 0xC4, 0xD5, 0xE6, 0xF7, 0x08, 0x19]
        payloads = _make_profile2_frames(data_ids, n=200)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        best = profiles[0]
        assert best.profile == 2
        assert best.crc_position == 0
        assert best.crc_width == 1
        assert best.confidence >= 0.9
        assert best.confidence_label == 'confirmed'
        # Check recovered Data IDs
        assert best.data_ids is not None
        assert best.data_ids.data_ids == data_ids

    def test_profile22(self):
        data_ids = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00]
        payloads = _make_profile22_frames(data_ids, n=200)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        # Find the Profile 22 result (there may also be a Profile 2 result with lower confidence)
        p22 = [r for r in profiles if r.profile == 22]
        assert len(p22) >= 1
        best = p22[0]
        assert best.crc_position == 0
        assert best.confidence >= 0.9
        assert best.data_ids is not None
        assert best.data_ids.data_ids == data_ids

    def test_profile5(self):
        data_id = 0xABCD
        payloads = _make_profile5_frames(data_id, n=100)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        best = profiles[0]
        assert best.profile == 5
        assert best.crc_position == 0
        assert best.crc_width == 2
        assert best.confidence >= 0.9
        assert best.data_ids is not None
        assert best.data_ids.data_ids == data_id

    def test_profile4(self):
        data_id = 0xDEADBEEF
        payloads = _make_profile4_frames(data_id, n=100)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        best = profiles[0]
        assert best.profile == 4
        assert best.crc_position == 8
        assert best.crc_width == 4
        assert best.confidence >= 0.9
        assert best.data_ids is not None
        assert best.data_ids.data_ids == data_id

    def test_profile6(self):
        data_id = 0x1234
        payloads = _make_profile6_frames(data_id, n=100)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        best = profiles[0]
        assert best.profile == 6
        assert best.crc_position == 0
        assert best.crc_width == 2
        assert best.confidence >= 0.9
        assert best.data_ids is not None
        assert best.data_ids.data_ids == data_id

    def test_profile7(self):
        data_id = 0xCAFEBABE
        payloads = _make_profile7_frames(data_id, n=100)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        best = profiles[0]
        assert best.profile == 7
        assert best.crc_position == 0
        assert best.crc_width == 8
        assert best.confidence >= 0.9
        assert best.data_ids is not None
        assert best.data_ids.data_ids == data_id

    def test_no_e2e(self):
        """Random payloads should not detect any profile."""
        import hashlib

        payloads = [hashlib.sha256(i.to_bytes(4, 'big')).digest()[:8] for i in range(100)]
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) == 0

    def test_too_few_frames(self):
        data_ids = list(range(16))
        payloads = _make_profile2_frames(data_ids, n=5)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) == 0


# ---------------------------------------------------------------------------
# Data ID recovery
# ---------------------------------------------------------------------------


class TestDataIdRecovery:
    def test_recover_profile2_data_ids(self):
        data_ids = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10]
        payloads = _make_profile2_frames(data_ids, n=200)
        counter = detect_alive_counter(payloads)
        assert counter is not None
        recovery = recover_data_ids(payloads, 2, 0, counter)
        assert recovery is not None
        assert recovery.data_ids == data_ids
        assert recovery.verification_rate >= 0.9

    def test_recover_profile4_data_id(self):
        data_id = 0xDEADBEEF
        payloads = _make_profile4_frames(data_id, n=50)
        recovery = recover_data_ids(payloads, 4, 8)
        assert recovery is not None
        assert recovery.data_ids == data_id

    def test_recover_profile5_data_id(self):
        data_id = 0xBEEF
        payloads = _make_profile5_frames(data_id, n=100)
        recovery = recover_data_ids(payloads, 5, 0)
        assert recovery is not None
        assert recovery.data_ids == data_id
        assert recovery.verification_rate >= 0.9

    def test_recover_profile7_data_id(self):
        data_id = 0xCAFEBABE
        payloads = _make_profile7_frames(data_id, n=50)
        recovery = recover_data_ids(payloads, 7, 0)
        assert recovery is not None
        assert recovery.data_ids == data_id


# ---------------------------------------------------------------------------
# Bypass detection
# ---------------------------------------------------------------------------


class TestBypassDetection:
    def test_static_crc(self):
        """CRC that never changes despite varying payload data."""
        payloads = []
        for i in range(50):
            base = bytearray(8)
            base[0] = 0x42  # Static CRC
            base[1] = i % 16
            base[2] = (i * 7) & 0xFF
            base[3] = (i * 13) & 0xFF
            payloads.append(bytes(base))

        bypasses = detect_e2e_bypass(payloads, crc_position=0, crc_width=1)
        static = [b for b in bypasses if b.bypass_type == 'static_crc']
        assert len(static) == 1
        assert static[0].severity == 'critical'

    def test_counter_freeze(self):
        """Counter that never changes."""
        counter = AliveCounterResult(
            position=1, width=4, modulus=16, nibble_high=False, increment_rate=0.0, confidence=0.5
        )
        payloads = []
        for i in range(50):
            base = bytearray(8)
            base[0] = (i * 3) & 0xFF
            base[1] = 0x05  # Frozen counter (low nibble = 5)
            base[2] = (i * 7) & 0xFF
            payloads.append(bytes(base))

        bypasses = detect_e2e_bypass(payloads, crc_position=0, counter=counter)
        frozen = [b for b in bypasses if b.bypass_type == 'counter_freeze']
        assert len(frozen) == 1
        assert frozen[0].severity == 'critical'

    def test_crc_all_zero(self):
        """CRC always 0x00."""
        payloads = []
        for i in range(50):
            base = bytearray(8)
            base[0] = 0x00  # CRC always zero
            base[1] = i % 16
            base[2] = (i * 7) & 0xFF
            payloads.append(bytes(base))

        bypasses = detect_e2e_bypass(payloads, crc_position=0, crc_width=1)
        zero = [b for b in bypasses if b.bypass_type == 'crc_zero_or_ff']
        assert len(zero) >= 1

    def test_crc_all_ff(self):
        """CRC always 0xFF."""
        payloads = []
        for i in range(50):
            base = bytearray(8)
            base[0] = 0xFF  # CRC always 0xFF
            base[1] = i % 16
            base[2] = (i * 7) & 0xFF
            payloads.append(bytes(base))

        bypasses = detect_e2e_bypass(payloads, crc_position=0, crc_width=1)
        ff = [b for b in bypasses if b.bypass_type == 'crc_zero_or_ff']
        assert len(ff) >= 1

    def test_no_bypass_on_valid_frames(self):
        """Properly protected frames should not trigger bypass detection."""
        data_ids = list(range(16))
        payloads = _make_profile2_frames(data_ids, n=100)
        counter = detect_alive_counter(payloads)
        bypasses = detect_e2e_bypass(payloads, crc_position=0, counter=counter)
        # No static CRC or counter freeze expected
        static = [b for b in bypasses if b.bypass_type in ('static_crc', 'counter_freeze')]
        assert len(static) == 0

    def test_counter_backwards(self):
        """Counter that frequently jumps backwards."""
        counter = AliveCounterResult(
            position=1, width=4, modulus=16, nibble_high=False, increment_rate=0.5, confidence=0.5
        )
        payloads = []
        vals = [0, 1, 2, 3, 2, 1, 4, 5, 3, 6, 7, 5, 8, 9, 7, 10, 11, 9, 12, 13, 11, 14, 15, 13, 0, 1, 15, 2, 3, 1]
        for i in range(30):
            base = bytearray(8)
            base[0] = (i * 3) & 0xFF
            base[1] = vals[i] & 0x0F
            base[2] = (i * 7) & 0xFF
            payloads.append(bytes(base))

        bypasses = detect_e2e_bypass(payloads, crc_position=0, counter=counter)
        backward = [b for b in bypasses if b.bypass_type == 'counter_backwards']
        assert len(backward) == 1
        assert backward[0].severity == 'warning'


# ---------------------------------------------------------------------------
# E2E header computation
# ---------------------------------------------------------------------------


class TestComputeE2eHeader:
    def test_profile2_roundtrip(self):
        data_ids = list(range(16))
        profile = Profile2(data_ids)
        base = bytes(8)
        for counter in range(16):
            protected = compute_e2e_header(2, base, counter, data_ids)
            result = profile.check(protected)
            assert result.ok, f'Profile 2 roundtrip failed for counter {counter}'

    def test_profile22_roundtrip(self):
        data_ids = [0xAA] * 16
        profile = Profile22(data_ids)
        base = bytes(8)
        for counter in range(16):
            protected = compute_e2e_header(22, base, counter, data_ids)
            result = profile.check(protected)
            assert result.ok, f'Profile 22 roundtrip failed for counter {counter}'

    def test_profile5_roundtrip(self):
        data_id = 0x5678
        profile = Profile5(data_id)
        base = bytes(8)
        for counter in range(10):
            protected = compute_e2e_header(5, base, counter, data_id)
            result = profile.check(protected)
            assert result.ok, f'Profile 5 roundtrip failed for counter {counter}'

    def test_profile4_roundtrip(self):
        data_id = 0x12345678
        profile = Profile4(data_id)
        base = bytes(16)
        for counter in range(10):
            protected = compute_e2e_header(4, base, counter, data_id)
            result = profile.check(protected)
            assert result.ok, f'Profile 4 roundtrip failed for counter {counter}'

    def test_profile6_roundtrip(self):
        data_id = 0x9ABC
        profile = Profile6(data_id)
        base = bytes(12)
        for counter in range(10):
            protected = compute_e2e_header(6, base, counter, data_id)
            result = profile.check(protected)
            assert result.ok, f'Profile 6 roundtrip failed for counter {counter}'

    def test_profile7_roundtrip(self):
        data_id = 0xFEDCBA98
        profile = Profile7(data_id)
        base = bytes(24)
        for counter in range(10):
            protected = compute_e2e_header(7, base, counter, data_id)
            result = profile.check(protected)
            assert result.ok, f'Profile 7 roundtrip failed for counter {counter}'

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError, match='Unknown E2E profile'):
            compute_e2e_header(99, bytes(8), 0, 0)


# ---------------------------------------------------------------------------
# Full detection pipeline
# ---------------------------------------------------------------------------


class TestDetectE2e:
    def test_full_pipeline_profile2(self):
        data_ids = [0x10 + i for i in range(16)]
        payloads = _make_profile2_frames(data_ids, n=200)
        result = detect_e2e(payloads)
        assert result.best is not None
        assert result.best.profile == 2
        assert result.best.confidence >= 0.9
        assert result.counter is not None

    def test_full_pipeline_profile5(self):
        data_id = 0x1111
        payloads = _make_profile5_frames(data_id, n=100)
        result = detect_e2e(payloads)
        assert result.best is not None
        assert result.best.profile == 5
        assert result.best.data_ids is not None
        assert result.best.data_ids.data_ids == data_id

    def test_full_pipeline_profile4(self):
        data_id = 0xABCDEF01
        payloads = _make_profile4_frames(data_id, n=100)
        result = detect_e2e(payloads)
        assert result.best is not None
        assert result.best.profile == 4

    def test_full_pipeline_profile6(self):
        data_id = 0x5678
        payloads = _make_profile6_frames(data_id, n=100)
        result = detect_e2e(payloads)
        assert result.best is not None
        assert result.best.profile == 6

    def test_full_pipeline_profile7(self):
        data_id = 0xDEADC0DE
        payloads = _make_profile7_frames(data_id, n=100)
        result = detect_e2e(payloads)
        assert result.best is not None
        assert result.best.profile == 7

    def test_full_pipeline_no_e2e(self):
        """Random payloads should return no profiles."""
        import hashlib

        payloads = [hashlib.sha256(i.to_bytes(4, 'big')).digest()[:8] for i in range(100)]
        result = detect_e2e(payloads)
        assert result.best is None
        assert len(result.profiles) == 0

    def test_full_pipeline_too_few_frames(self):
        result = detect_e2e([bytes(8)] * 3)
        assert result.best is None

    def test_full_pipeline_with_bypass(self):
        """Frames with a static CRC should trigger bypass detection."""
        payloads = []
        for i in range(50):
            base = bytearray(8)
            base[0] = 0x42  # Static CRC
            base[1] = i % 16
            base[2] = (i * 7) & 0xFF
            base[3] = (i * 13) & 0xFF
            payloads.append(bytes(base))

        detect_e2e(payloads)
        # May or may not detect a profile, but if it does, bypass should be found
        # With random CRC values, detection probably won't work, so test bypass directly
        bypasses = detect_e2e_bypass(payloads, crc_position=0, crc_width=1)
        static = [b for b in bypasses if b.bypass_type == 'static_crc']
        assert len(static) >= 1

    def test_corrupted_frames(self):
        """A few corrupted frames should not prevent detection."""
        data_ids = list(range(16))
        payloads = _make_profile2_frames(data_ids, n=200)
        # Corrupt 5% of frames
        corrupted = list(payloads)
        for i in range(0, len(corrupted), 20):
            c = bytearray(corrupted[i])
            c[0] ^= 0xFF  # Flip the CRC byte
            corrupted[i] = bytes(c)
        result = detect_e2e(corrupted)
        assert result.best is not None
        assert result.best.profile == 2
        assert result.best.confidence >= 0.85

    def test_findings_populated(self):
        data_ids = list(range(16))
        payloads = _make_profile2_frames(data_ids, n=100)
        result = detect_e2e(payloads)
        assert len(result.findings) > 0


# ---------------------------------------------------------------------------
# CAN FD payloads
# ---------------------------------------------------------------------------


class TestCanFd:
    def test_profile4_canfd(self):
        """Profile 4 with a CAN FD payload (64 bytes)."""
        data_id = 0x12345678
        payloads = _make_profile4_frames(data_id, n=100, payload_len=64)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        assert profiles[0].profile == 4
        assert profiles[0].confidence >= 0.9

    def test_profile7_canfd(self):
        """Profile 7 with a CAN FD payload (64 bytes)."""
        data_id = 0xCAFEBABE
        payloads = _make_profile7_frames(data_id, n=100, payload_len=64)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        assert profiles[0].profile == 7
        assert profiles[0].confidence >= 0.9

    def test_profile6_canfd(self):
        """Profile 6 with a CAN FD payload (32 bytes)."""
        data_id = 0x9ABC
        payloads = _make_profile6_frames(data_id, n=100, payload_len=32)
        profiles = detect_e2e_profile(payloads)
        assert len(profiles) >= 1
        assert profiles[0].profile == 6
        assert profiles[0].confidence >= 0.9


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_payloads(self):
        result = detect_e2e([])
        assert result.best is None

    def test_single_byte_payloads(self):
        payloads = [bytes([i]) for i in range(50)]
        result = detect_e2e(payloads)
        assert result.best is None

    def test_mixed_lengths(self):
        """Payloads with different lengths should be filtered to the most common length."""
        data_ids = list(range(16))
        payloads = _make_profile2_frames(data_ids, n=100)
        # Add some payloads with a different length
        payloads.extend([bytes(4)] * 10)
        result = detect_e2e(payloads)
        assert result.best is not None
        assert result.best.profile == 2

    def test_data_id_recovery_description(self):
        """The DataIdRecovery description should include the Data IDs."""
        from can_explorer.core.reverse.e2e_reverse import DataIdRecovery

        recovery = DataIdRecovery([0x01, 0x02, 0x03, 0x04] + [0] * 12, 0.95)
        desc = recovery.description
        assert '0x01' in desc
        assert '95%' in desc

    def test_profile_result_description(self):
        """The E2eProfileResult description property should work."""
        result = E2eProfileResult(
            profile=2,
            crc_position=0,
            crc_width=1,
            counter=None,
            data_ids=None,
            confidence=0.95,
            confidence_label='confirmed',
        )
        desc = result.description
        assert 'Profile 2' in desc
        assert 'confirmed' in desc

    def test_bypass_finding_fields(self):
        finding = E2eBypassFinding('static_crc', 'CRC never changes', 'critical')
        assert finding.bypass_type == 'static_crc'
        assert finding.severity == 'critical'
