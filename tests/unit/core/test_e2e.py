from can_explorer.core import e2e

# AUTOSAR CRC library published check values (SWS CRC), for two standard inputs.
ZEROS = bytes([0x00, 0x00, 0x00, 0x00])
F2 = bytes([0xF2, 0x01, 0x83])


def test_crc8_sae_j1850():
    assert e2e.crc8(ZEROS) == 0x59
    assert e2e.crc8(F2) == 0x37
    assert e2e.crc8(bytes([0x0F, 0xAA, 0x00, 0x55])) == 0x79


def test_crc8h2f():
    assert e2e.crc8h2f(ZEROS) == 0x12
    assert e2e.crc8h2f(F2) == 0xC2


def test_crc16_ccitt():
    assert e2e.crc16(ZEROS) == 0x84C0
    assert e2e.crc16(F2) == 0xD374
    assert e2e.crc16(bytes([0x0F, 0xAA, 0x00, 0x55])) == 0x2023


def test_crc32():
    assert e2e.crc32(ZEROS) == 0x2144DF1C
    assert e2e.crc32(F2) == 0x24AB9D77


def test_crc32p4():
    assert e2e.crc32p4(ZEROS) == 0x6FB32240
    assert e2e.crc32p4(F2) == 0x4F721A25


def test_profile2_protect_and_check_round_trip():
    data_ids = list(range(0x10, 0x20))  # 16 Data IDs, one per counter value
    profile = e2e.Profile2(data_ids)
    payload = bytes([0x00, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])  # byte 0 = CRC slot, byte 1 low nibble = counter

    protected = profile.protect(payload, counter=5)
    assert protected[1] & 0x0F == 5
    result = profile.check(protected)
    assert result.ok and result.counter == 5 and result.crc == result.expected_crc

    # A corrupted byte fails the check
    tampered = bytearray(protected)
    tampered[3] ^= 0xFF
    assert not profile.check(bytes(tampered)).ok

    # A different counter selects a different Data ID, so the same bytes no longer verify
    wrong_counter = bytearray(protected)
    wrong_counter[1] = (wrong_counter[1] & 0xF0) | 6
    assert not profile.check(bytes(wrong_counter)).ok


def test_profile2_requires_sixteen_ids():
    import pytest

    with pytest.raises(ValueError, match='16 entries'):
        e2e.Profile2([1, 2, 3])
