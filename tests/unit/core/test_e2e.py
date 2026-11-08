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


def test_profile5_matches_autosar_example():
    # AUTOSAR PRS_E2EProtocol §6.8.5, Table 6.53: DataID 0x1234, offset 0, 8-byte PDU with 5 data bytes (all zero),
    # counter 0 -> CRC bytes 0x1c 0xca (little endian).
    profile = e2e.Profile5(0x1234)
    protected = profile.protect(bytes(8), counter=0)
    assert protected[0] == 0x1C and protected[1] == 0xCA and protected[2] == 0x00
    result = profile.check(protected)
    assert result.ok and result.counter == 0 and result.crc == 0xCA1C


def test_profile5_round_trip_and_tamper():
    profile = e2e.Profile5(0x0A0B)
    payload = bytes([0x00, 0x00, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])  # bytes 0-1 CRC, byte 2 counter
    protected = profile.protect(payload, counter=200)
    assert protected[2] == 200 and profile.check(protected).ok
    tampered = bytearray(protected)
    tampered[4] ^= 0xFF
    assert not profile.check(bytes(tampered)).ok
    wrong_counter = bytearray(protected)
    wrong_counter[2] = 201  # The counter is covered by the CRC, so changing it alone fails the check
    assert not profile.check(bytes(wrong_counter)).ok
    assert not profile.check(b'\x00\x00').ok  # Too short


def test_profile22_matches_autosar_example():
    # AUTOSAR PRS_E2EProtocol §6.13.5, Table 6.101: DataIDList 0x01..0x10, 8-byte PDU of zeros.
    profile = e2e.Profile22(list(range(0x01, 0x11)))
    for counter, expected_crc in ((1, 0x1B), (2, 0x98), (0, 0x0E), (15, 0xCD)):
        protected = profile.protect(bytes(8), counter=counter)
        assert protected[0] == expected_crc and protected[1] & 0x0F == counter & 0x0F
        assert profile.check(protected).ok


def test_profile22_round_trip_and_tamper():
    profile = e2e.Profile22(list(range(0x20, 0x30)))
    protected = profile.protect(bytes([0x00, 0x00, 0x11, 0x22, 0x33]), counter=7)
    assert protected[1] & 0x0F == 7 and profile.check(protected).ok
    tampered = bytearray(protected)
    tampered[3] ^= 0xFF
    assert not profile.check(bytes(tampered)).ok


def test_profile22_requires_sixteen_ids():
    import pytest

    with pytest.raises(ValueError, match='16 entries'):
        e2e.Profile22([1, 2, 3])
