import can
import pytest

from can_explorer.core import j1939_assurance as ja
from can_explorer.core.protocols.j1939 import CPg, J1939_22Decoder, split_multi_pg

KEY = bytes(range(16))
DATA_IDS = tuple(range(0x01, 0x11))
SAFETY = ja.SafetyConfig(DATA_IDS, offset=0, max_delta=2)
SECURITY = ja.SecurityConfig(KEY.hex(), freshness_bits=31, tag_bits=32)


def _fd(can_id, data):
    return can.Message(arbitration_id=can_id, is_extended_id=True, is_fd=True, data=bytes(data))


def _cpg(tos, tf, pgn, payload):
    return bytes([(tos << 5) | (tf << 2) | (pgn >> 16), (pgn >> 8) & 0xFF, pgn & 0xFF, len(payload)]) + payload


def test_trailer_formats_follow_the_public_table():
    sizes = {(f.tos, f.tf): (f.security_bytes, f.safety_bytes) for f in ja.TRAILER_FORMATS.values()}
    assert sizes == {(1, 1): (4, 0), (1, 2): (0, 4), (1, 3): (4, 4), (1, 5): (8, 0), (1, 6): (0, 8), (2, 0): (0, 0)}
    for reserved in ((1, 0), (1, 4), (1, 7), (2, 1), (3, 0), (7, 7)):
        assert ja.trailer_format(*reserved) is None


def test_split_payload_takes_the_trailer_off_the_end():
    split = ja.split_payload(1, 3, bytes(range(12)))
    assert (split.data, split.security, split.safety) == (bytes(range(4)), bytes(range(4, 8)), bytes(range(8, 12)))
    # New Eagle's bus trace: a TOS 1 / TF 5 C-PG of 12 bytes = 4 data bytes + the 8-byte cybersecurity trailer
    trace = bytes.fromhex('34 02 00 0C 00 00 00 00 00 00 00 00 8B BD 9C C5')
    (pg,) = split_multi_pg(trace)
    assert (pg.tos, pg.tf) == (1, 5)
    assert pg.split().data == bytes(4) and pg.split().security == bytes.fromhex('000000008BBD9CC5')
    assert 'shorter than its 8-byte trailer' in ja.split_payload(1, 5, bytes(3)).error
    assert ja.split_payload(3, 0, b'\x01').format is None


def test_decoder_decodes_the_pg_data_before_the_trailer():
    request = bytes([0xEC, 0xFE, 0x00])  # Request for Vehicle Identification
    frame = _cpg(1, 2, 0xEA00, request + bytes.fromhex('11223344'))
    text = J1939_22Decoder(assurance=ja.AssuranceChecker()).describe(_fd(0x18250000 | 0xFF00, frame))
    assert 'PGN 0x0EA00 Request: EC FE 00 — requests PGN 0x0FEEC Vehicle Identification' in text
    assert '[32-bit OEM functional safety assurance data: safety 11223344; not checked]' in text


def test_safety_trailer_checked_with_profile_22():
    config = ja.AssuranceConfig(0xF004, safety=SAFETY)
    checker = ja.AssuranceChecker([config])
    data = bytes(range(8))
    for counter, expected in ((1, 'E2E P22 OK, counter 1'), (3, 'counter 3 (1 lost)'), (3, 'REPEATED')):
        payload = ja.protect(config, 1, 2, data, source=0x02, counter=counter)
        assert expected in checker.check(0xF004, 0x02, 1, 2, payload).describe()
    payload = ja.protect(config, 1, 2, data, source=0x02, counter=7)
    assert 'WRONG SEQUENCE' in checker.check(0xF004, 0x02, 1, 2, payload).describe()
    tampered = bytearray(payload)
    tampered[0] ^= 1  # The PG data is protected
    result = checker.check(0xF004, 0x02, 1, 2, bytes(tampered))
    assert not result.ok and 'does not verify with this configuration' in result.describe()
    assert checker.check(0xFEF1, 0x02, 1, 2, payload) is None  # Not configured


def test_safety_header_at_an_offset_and_after_cybersecurity_data():
    short = ja.SecurityConfig(KEY.hex(), freshness_bits=7, tag_bits=24)  # 7 + 1 + 24 bits: a 32-bit trailer
    config = ja.AssuranceConfig(0xF004, safety=ja.SafetyConfig(DATA_IDS, offset=2), security=short)
    payload = ja.protect(config, 1, 3, b'\xaa\xbb', source=0x10, counter=5, freshness=9)
    assert len(payload) == 2 + 4 + 4  # TF 3: a 32-bit cybersecurity and a 32-bit safety trailer
    assert payload[-1] & 0x0F == 5  # Counter nibble right after the CRC at byte 2 of the safety trailer
    result = ja.AssuranceChecker([config]).check(0xF004, 0x10, 1, 3, payload)
    assert result.ok and result.describe() == 'E2E P22 OK, counter 5; tag OK, FV 9'
    # A layout that does not fill the trailer is reported, not guessed around
    wide = ja.AssuranceChecker([ja.AssuranceConfig(0xF004, security=SECURITY)]).check(0xF004, 0x10, 1, 3, payload)
    assert 'do not fill the 32-bit trailer' in wide.describe()


def test_cybersecurity_tag_freshness_and_replay():
    config = ja.AssuranceConfig(0xFEF1, source=0x02, security=SECURITY)
    checker = ja.AssuranceChecker([config])
    data = bytes.fromhex('0102030405060708')
    first = ja.protect(config, 1, 5, data, source=0x02, freshness=100)
    assert checker.check(0xFEF1, 0x02, 1, 5, first).describe() == 'tag OK, FV 100'
    replay = checker.check(0xFEF1, 0x02, 1, 5, first)
    assert not replay.ok and 'replayed' in replay.describe()
    assert checker.check(0xFEF1, 0x02, 1, 5, ja.protect(config, 1, 5, data, source=0x02, freshness=101)).ok
    # The tag covers PGN and source address: another sender's frame does not verify with this key
    other = ja.AssuranceConfig(0xFEF1, security=SECURITY)
    forged = ja.protect(other, 1, 5, data, source=0x03, freshness=500)
    assert ja.AssuranceChecker([other]).check(0xFEF1, 0x02, 1, 5, forged).ok is False
    # Wrong key: reported as not verifying with this configuration
    wrong = ja.AssuranceChecker([ja.AssuranceConfig(0xFEF1, security=ja.SecurityConfig('00' * 16, 31, 32))])
    assert 'does not verify with this configuration' in wrong.check(0xFEF1, 0x02, 1, 5, first).describe()


def test_tag_is_the_truncated_cmac_over_nonce_and_plaintext():
    from can_explorer.core.secoc import cmac_aes

    nonce = ja.nonce(0xFEF1, 0x02, 100, False, 31)
    assert nonce == bytes([0x00, 0xFE, 0xF1, 0x02, 0x00, 0x00, 0x00, 0x64])
    assert ja.nonce(0xFEF1, 0x02, 1, True, 8)[:3] == bytes([0x80, 0xFE, 0xF1])  # E at bit 23
    full = cmac_aes(KEY, nonce + b'\x01')
    assert ja.secure_tag(KEY, 0xFEF1, 0x02, 100, False, b'\x01', SECURITY) == int.from_bytes(full[:4], 'big')
    packed = ja.pack_security(100, True, 0xDEADBEEF, SECURITY)
    assert ja.unpack_security(packed, SECURITY) == ja.SecureTrailer(100, True, 0xDEADBEEF)


def test_encrypted_payload_is_labelled_not_checked():
    config = ja.AssuranceConfig(0xFEF1, security=SECURITY)
    payload = bytes(4) + ja.pack_security(7, True, 0x12345678, SECURITY)
    result = ja.AssuranceChecker([config]).check(0xFEF1, 0x00, 1, 5, payload)
    assert result.ok and 'encrypted payload (FV 7): not decrypted' in result.describe()


def test_locked_or_missing_key_is_reported():
    config = ja.AssuranceConfig(0xFEF1, security=ja.SecurityConfig('keystore:nope', 31, 32))
    payload = ja.protect(ja.AssuranceConfig(0xFEF1, security=SECURITY), 1, 5, b'\x01', source=0)
    text = ja.AssuranceChecker([config]).check(0xFEF1, 0, 1, 5, payload).describe()
    assert text.startswith('tag not checked:')


def test_configuration_round_trip_and_errors(tmp_path):
    configs = [
        ja.AssuranceConfig(0xF004, safety=SAFETY, name='engine'),
        ja.AssuranceConfig(0xFEF1, source=0x02, security=SECURITY),
    ]
    path = tmp_path / 'assurance.json'
    ja.save_configs(path, configs)
    assert ja.load_configs(path) == configs
    with pytest.raises(ja.AssuranceError, match='freshness_bits'):
        ja.AssuranceConfig.from_dict({'pgn': '0xF004', 'security': {'key': 'aa', 'tag_bits': 32}})
    with pytest.raises(ja.AssuranceError, match='16 Data IDs'):
        ja.SafetyConfig((1, 2))
    with pytest.raises(ja.AssuranceError, match='safety check, a cybersecurity check or both'):
        ja.AssuranceConfig(0xF004)
    with pytest.raises(ja.AssuranceError, match='Profile 22'):
        ja.SafetyConfig.from_dict({'profile': 2, 'data_ids': list(DATA_IDS)})
    with pytest.raises(ja.AssuranceError, match='Two assurance'):
        ja.AssuranceChecker([configs[0], configs[0]])


def test_decoder_uses_the_users_configuration_file(tmp_path, monkeypatch):
    config = ja.AssuranceConfig(0xF004, safety=SAFETY)
    path = tmp_path / 'j1939_assurance.json'
    ja.save_configs(path, [config])
    monkeypatch.setenv(ja.CONFIG_VARIABLE, str(path))
    decoder = J1939_22Decoder()
    payload = ja.protect(config, 1, 2, bytes(8), source=0x00, counter=1)
    text = decoder.describe(_fd(0x1825FF00, _cpg(1, 2, 0xF004, payload)))
    assert 'E2E P22 OK, counter 1]' in text
    decoder.reset()  # Forgets the counters
    assert 'E2E P22 OK, counter 1]' in decoder.describe(_fd(0x1825FF00, _cpg(1, 2, 0xF004, payload)))
    path.write_text('not json')
    assert not J1939_22Decoder().assurance  # A broken file is logged and ignored


def test_cpg_split_matches_module():
    pg = CPg(1, 1, 0xF004, bytes(6))
    assert pg.split().data == bytes(2) and pg.split().security == bytes(4)
