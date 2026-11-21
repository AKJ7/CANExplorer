"""Tests for the linear dependency search module: finding CRC, XOR and affine relations in CAN payloads."""

import random

from can_explorer.core.reverse.linear import find_linear_relations


def _crc16_ccitt(data: bytes, start: int = 0xFFFF) -> int:
    """CRC-16/CCITT (polynomial 0x1021) of data."""
    crc = start
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _crc32(data: bytes) -> int:
    """Simple CRC-32 (polynomial 0x04C11DB7) with start 0xFFFFFFFF and final XOR 0xFFFFFFFF."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc ^ 0xFFFFFFFF


def _crc8_j1850(data: bytes) -> int:
    """CRC-8 SAE J1850 (polynomial 0x1D, start 0xFF, final XOR 0xFF)."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x1D) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc ^ 0xFF


class TestLinearRelations:
    def test_crc16_protected_payloads(self):
        """Payloads with a CRC-16 (0x1021) in the last two bytes should be found."""
        rng = random.Random(10)
        payloads = []
        for _ in range(200):
            data = rng.randbytes(6)
            crc = _crc16_ccitt(data)
            payloads.append(data + crc.to_bytes(2, 'little'))
        relations = find_linear_relations(payloads)
        assert len(relations) >= 1
        # At least one relation should cover the last 2 bytes
        output_starts = [min(r.output_bits) for r in relations]
        assert any(s == 48 for s in output_starts)  # Byte 6 starts at bit 48

    def test_crc32_protected_payloads(self):
        """Payloads with a CRC-32 in the last four bytes should find a relation."""
        rng = random.Random(20)
        payloads = []
        for _ in range(200):
            data = rng.randbytes(4)
            crc = _crc32(data)
            payloads.append(data + crc.to_bytes(4, 'little'))
        relations = find_linear_relations(payloads)
        # Should find a 32-bit relation covering the last 4 bytes
        found_32 = [r for r in relations if len(r.output_bits) == 32]
        assert len(found_32) >= 1

    def test_xor_masked_bytes(self):
        """Bytes XOR'd with a fixed mask should be found as a linear relation."""
        rng = random.Random(30)
        payloads = []
        for _ in range(200):
            data = rng.randbytes(7)
            # Last byte is XOR of all other bytes plus a constant
            xor_val = 0
            for b in data:
                xor_val ^= b
            xor_val ^= 0x42  # constant
            payloads.append(data + bytes([xor_val & 0xFF]))
        relations = find_linear_relations(payloads)
        assert len(relations) >= 1

    def test_truly_random_data_no_relations(self):
        """Truly random data should produce no linear relations."""
        rng = random.Random(40)
        payloads = [rng.randbytes(8) for _ in range(200)]
        relations = find_linear_relations(payloads)
        assert len(relations) == 0

    def test_generalises_crc8(self):
        """The linear search should also find CRC-8 relations (generalising find_checksums)."""
        rng = random.Random(50)
        payloads = []
        for _ in range(200):
            data = rng.randbytes(7)
            crc = _crc8_j1850(data)
            payloads.append(data + bytes([crc]))
        relations = find_linear_relations(payloads)
        # Should find an 8-bit relation at byte 7
        found_8 = [r for r in relations if len(r.output_bits) == 8 and min(r.output_bits) == 56]
        assert len(found_8) >= 1

    def test_too_few_frames(self):
        """With too few frames, no relations should be found."""
        rng = random.Random(60)
        payloads = [rng.randbytes(8) for _ in range(10)]
        relations = find_linear_relations(payloads, min_frames=50)
        assert relations == []

    def test_single_byte_payloads(self):
        """Single byte payloads should return no relations (need at least 2 bytes)."""
        payloads = [bytes([i & 0xFF]) for i in range(200)]
        relations = find_linear_relations(payloads)
        assert relations == []
