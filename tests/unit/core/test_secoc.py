import can

from can_explorer.core import secoc


def test_aes128_fips197():
    ciphertext = secoc.aes128_encrypt(
        bytes.fromhex('000102030405060708090a0b0c0d0e0f'), bytes.fromhex('00112233445566778899aabbccddeeff')
    )
    assert ciphertext.hex() == '69c4e0d86a7b0430d8cdb78070b4c55a'


def test_cmac_aes128_nist_vectors():
    # NIST SP 800-38B, key and message M
    key = bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c')
    message = bytes.fromhex(
        '6bc1bee22e409f96e93d7e117393172a'
        'ae2d8a571e03ac9c9eb76fac45af8e51'
        '30c81c46a35ce411e5fbc1191a0a52ef'
        'f69f2445df4f9b17ad2b417be66c3710'
    )
    expected = {
        0: 'bb1d6929e95937287fa37d129b756746',
        16: '070a16b46b4d4144f79bdd9dd04a287c',
        40: 'dfa66747de9ae63030ca32611497c827',
        64: '51f0bebf7e3b9d92fc49741779363cfe',
    }
    for length, mac in expected.items():
        assert secoc.cmac_aes128(key, message[:length]).hex() == mac


def test_secoc_generate_and_verify():
    key = bytes(range(16))
    data_id = 0x1234
    payload = bytes([0x11, 0x22, 0x33, 0x44])
    freshness = bytes([0, 0, 0, 5])

    mac = secoc.generate_mac(key, data_id, payload, freshness, mac_length_bits=32)
    assert len(mac) == 4
    assert secoc.verify(key, data_id, payload, freshness, mac, 32)
    # A changed payload, data id, freshness, or key all fail verification
    assert not secoc.verify(key, data_id, bytes([0x11, 0x22, 0x33, 0x45]), freshness, mac, 32)
    assert not secoc.verify(key, 0x1235, payload, freshness, mac, 32)
    assert not secoc.verify(key, data_id, payload, bytes([0, 0, 0, 6]), mac, 32)
    assert not secoc.verify(bytes(16), data_id, payload, freshness, mac, 32)


def test_split_and_decoder():
    assert secoc.split(bytes([1, 2, 3, 4, 0xAA, 0xBB, 0xCC, 0xDD]), mac_bytes=4) == (
        bytes([1, 2, 3, 4]),
        b'',
        bytes([0xAA, 0xBB, 0xCC, 0xDD]),
    )
    # With an in-PDU freshness value
    authentic, freshness, mac = secoc.split(bytes([1, 2, 3, 0x07, 0xAA, 0xBB]), mac_bytes=2, freshness_bytes=1)
    assert authentic == bytes([1, 2, 3]) and freshness == bytes([0x07]) and mac == bytes([0xAA, 0xBB])

    decoder = secoc.SecOcDecoder()
    message = can.Message(arbitration_id=0x200, is_extended_id=False, data=bytes([0x11, 0x22, 0xAA, 0xBB, 0xCC, 0xDD]))
    assert decoder.describe(message) == 'SecOC: authentic 11 22 | MAC AABBCCDD'
    assert secoc.SecOcDecoder(mac_bytes=8).describe(can.Message(data=bytes(4))) is None  # Too short for the MAC
