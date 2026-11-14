import dataclasses
import random

import can
import pytest
from cantools.autosar.secoc import apply_authenticator

from can_explorer.core.decoding import DbcDecoder, FrameDecoder
from can_explorer.core.keystore import KeyStore
from can_explorer.core.secoc import cmac_aes
from can_explorer.core.secoc_check import (
    CounterFreshness,
    KeyStoreSecOcKeys,
    SecOcConfig,
    SecOcError,
    SecOcVerifier,
    StaticSecOcKeys,
    algorithm_of,
    load_freshness_script,
    secoc_config_of,
)

KEY = bytes(range(16))
# The geometry of the secoc_arxml fixture: 2 payload bytes, Data ID 18, 4 of 32 freshness bits, 28 MAC bits
CONFIG = SecOcConfig('SecuredStatus', 18, 2, 28, 32, 4, 'CMAC_AES128', 'FreshnessCounter')


def verifier(keys=None) -> SecOcVerifier:
    return SecOcVerifier(keys or StaticSecOcKeys({'*': (KEY, '')}))


@pytest.mark.parametrize(
    'name, algorithm',
    [
        ('CMAC_AES128', 'cmac-aes'),
        ('SecOC_Cmac', 'cmac-aes'),
        ('HMAC-SHA256', 'hmac-sha256'),
        ('hmac_sha_256', 'hmac-sha256'),
        ('HMAC_SHA1', None),
        ('OEM_Auth', None),
        ('', None),
        (None, None),
    ],
)
def test_algorithm_of(name, algorithm):
    assert algorithm_of(name) == algorithm


def test_config_from_arxml(secoc_arxml):
    database = DbcDecoder(secoc_arxml)
    [config] = database.secoc_protected()
    assert config == CONFIG
    assert config.problem == '' and config.length == 6
    assert secoc_config_of(database.database.get_message_by_name('EngineData')) is None


def test_secured_frames_match_cantools(secoc_arxml):
    """cantools packs secured PDUs independently (bitstruct); both must give the same bytes."""
    message = DbcDecoder(secoc_arxml).database.get_message_by_name('SecuredStatus')
    secure = verifier()
    rng = random.Random(7)
    for freshness in [0, 1, 15, 16, 0xFFFFFFFF] + [rng.getrandbits(32) for _ in range(20)]:
        payload = bytes(rng.getrandbits(8) for _ in range(2))
        expected = apply_authenticator(payload + bytes(4), message, lambda _m, data, _f: cmac_aes(KEY, data), freshness)
        assert secure.secure(CONFIG, payload, freshness) == bytes(expected)


@pytest.mark.parametrize('tx_bits, auth_bits, full_bits', [(8, 24, 64), (12, 20, 48), (3, 13, 16), (32, 64, 32)])
def test_other_trailer_geometries_match_cantools(secoc_arxml, tx_bits, auth_bits, full_bits):
    message = DbcDecoder(secoc_arxml).database.get_message_by_name('SecuredStatus')
    properties = message.autosar.secoc
    properties._freshness_tx_bit_length = tx_bits
    properties._auth_tx_bit_length = auth_bits
    properties._freshness_bit_length = full_bits
    config = secoc_config_of(message)
    rng = random.Random(tx_bits)
    for _ in range(10):
        freshness, payload = rng.getrandbits(full_bits), bytes([rng.getrandbits(8), rng.getrandbits(8)])
        expected = apply_authenticator(
            payload + bytes(config.length - 2), message, lambda _m, data, _f: cmac_aes(KEY, data), freshness
        )
        frame = verifier().secure(config, payload, freshness)
        assert frame == bytes(expected)
        assert config.split(frame)[1] == freshness & ((1 << tx_bits) - 1)


def test_counter_reconstruction_across_wraps():
    sender, receiver = verifier(), verifier()
    for freshness in range(1, 40):  # The 4 transmitted bits wrap at 16 and 32
        frame = sender.secure(CONFIG, bytes([freshness, 0xAA]))
        assert frame == sender.secure(CONFIG, bytes([freshness, 0xAA]), freshness)
        check = receiver.check(CONFIG, frame)
        assert check.ok and check.freshness == freshness, check.describe()
    assert receiver.check(CONFIG, sender.secure(CONFIG, b'\1\2', 45)).ok  # A few lost frames are fine


def test_tampering_and_replays_fail():
    sender, receiver = verifier(), verifier()
    frame = sender.secure(CONFIG, b'\x11\x22', 5)
    assert receiver.check(CONFIG, frame).describe() == 'SecOC ok, FV 5'
    tampered = bytes([frame[0] ^ 1]) + frame[1:]
    assert receiver.check(CONFIG, tampered).describe() == 'SecOC MAC ERROR, FV 21'  # 5 again means a wrap: 16 + 5
    assert not receiver.check(CONFIG, frame).ok  # A replayed frame reconstructs to a newer value and fails


def test_replay_with_the_full_freshness_value():
    config = dataclasses.replace(CONFIG, freshness_bits=8, freshness_tx_bits=8)
    sender, receiver = verifier(), verifier()
    first, second = sender.secure(config, b'\1\2', 7), sender.secure(config, b'\1\2', 8)
    assert receiver.check(config, first).ok and receiver.check(config, second).ok
    replayed = receiver.check(config, first)
    assert not replayed.ok and replayed.describe() == 'SecOC FRESHNESS ERROR, FV 7 not newer than 8'


def test_unsynchronised_freshness_is_reported():
    sender = verifier()
    late = sender.secure(CONFIG, b'\1\2', 0x1234)  # High bits the receiver cannot know yet
    check = verifier().check(CONFIG, late)
    assert not check.ok and not check.synchronised
    assert check.describe() == 'SecOC MAC ERROR, FV 4 (freshness not synchronised yet)'


def test_not_checked():
    assert verifier().check(CONFIG, b'\1\2\3').describe() == (
        'SecOC: not checked (frame shorter than the 6 bytes of the secured PDU)'
    )
    no_id = dataclasses.replace(CONFIG, data_id=None)
    assert verifier().check(no_id, bytes(6)).reason == 'the ARXML gives no Data ID'
    unknown = dataclasses.replace(CONFIG, auth_algorithm='OEM_Auth')
    assert "algorithm 'OEM_Auth' not recognised" in verifier().check(unknown, bytes(6)).reason
    assert StaticSecOcKeys({}).key_for(CONFIG)[1] == 'no key for SecuredStatus'
    with pytest.raises(SecOcError, match='no key'):
        SecOcVerifier(StaticSecOcKeys({})).secure(CONFIG, b'\1\2')


def test_key_algorithm_overrides_the_arxml_name():
    config = dataclasses.replace(CONFIG, auth_algorithm='OEM_Auth')
    keys = StaticSecOcKeys({'SecuredStatus': (b'k' * 20, 'hmac-sha256')})
    frame = SecOcVerifier(keys).secure(config, b'\1\2', 1)
    assert SecOcVerifier(keys).check(config, frame).ok
    assert not verifier().check(dataclasses.replace(config, auth_algorithm='CMAC'), frame).ok


def test_keys_from_the_key_store(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    keys = KeyStoreSecOcKeys(store)
    assert 'no key for SecuredStatus (Data ID 0x12)' in keys.key_for(CONFIG)[1]
    store.add_secoc_key('Fallback', bytes(16), scope='*')
    store.add_secoc_key('ById', bytes([2]) * 16, scope='0x12')
    store.add_secoc_key('ByName', KEY, scope='Other, SecuredStatus')
    keys.invalidate()
    key, _ = keys.key_for(CONFIG)
    assert key.entry == 'ByName' and key.key == KEY and 'ByName' in repr(key) and KEY.hex() not in repr(key)
    assert keys.key_for(dataclasses.replace(CONFIG, pdu='Else'))[0].entry == 'ById'
    assert keys.key_for(dataclasses.replace(CONFIG, pdu='Else', data_id=1))[0].entry == 'Fallback'

    store.lock()
    locked = KeyStoreSecOcKeys(store)
    assert locked.key_for(CONFIG) == (None, 'the key store is locked')
    store.unlock('pw')
    assert locked.key_for(CONFIG)[0].entry == 'ByName'  # Unlocking takes effect without invalidating


def test_counter_freshness_next_value():
    freshness = CounterFreshness()
    assert [freshness.next_value(CONFIG) for _ in range(3)] == [1, 2, 3]
    freshness.accepted(CONFIG, 100)  # Sending continues after values seen on the bus
    assert freshness.next_value(CONFIG) == 101
    freshness.reset()
    assert freshness.next_value(CONFIG) == 1


def test_freshness_script(tmp_path):
    script = tmp_path / 'oem_freshness.py'
    script.write_text(
        'TRIP = 0x0300\n'
        'seen = []\n'
        'def freshness(pdu, data_id, truncated, tx_bits, full_bits):\n'
        '    return (TRIP << tx_bits) | truncated\n'
        'def accepted(pdu, data_id, value):\n'
        '    seen.append((pdu, value))\n'
        'def next_value(pdu, data_id, full_bits):\n'
        '    return (TRIP << 4) | 9\n',
        encoding='utf-8',
    )
    manager = load_freshness_script(script)
    keys = StaticSecOcKeys({'*': (KEY, '')})
    frame = SecOcVerifier(keys, manager).secure(CONFIG, b'\1\2')
    check = SecOcVerifier(keys, load_freshness_script(script)).check(CONFIG, frame)
    assert check.ok and check.freshness == 0x3009
    assert verifier().check(CONFIG, frame).synchronised is False  # The counter manager cannot know the trip


@pytest.mark.parametrize(
    'source, message',
    [('x = 1\n', 'does not define freshness'), ('raise RuntimeError("boom")\n', 'Error in freshness file')],
)
def test_bad_freshness_scripts(tmp_path, source, message):
    script = tmp_path / 'bad.py'
    script.write_text(source, encoding='utf-8')
    with pytest.raises(SecOcError, match=message):
        load_freshness_script(script)


def test_trace_descriptions(secoc_arxml):
    database = DbcDecoder(secoc_arxml)
    decoder = FrameDecoder(database=database, secoc=verifier())
    sender = verifier()

    def frame(freshness, payload=b'\x05\x00'):
        return can.Message(arbitration_id=0x300, is_extended_id=False, data=sender.secure(CONFIG, payload, freshness))

    assert decoder.describe(frame(1)) == '[SecOC ok, FV 1]'
    assert decoder.describe(frame(2)) == '[SecOC ok, FV 2]'
    other = can.Message(arbitration_id=0x123, data=bytes(8))
    assert decoder.describe(other) == ''
    decoder.reset()  # E.g. a new recording: the freshness state starts over
    assert decoder.describe(frame(0x15)).endswith('(freshness not synchronised yet)]')
    decoder.database = database  # Reloading the database resets it too
    assert decoder.describe(frame(5)) == '[SecOC ok, FV 5]'
    # Without a verifier (no security manager), secured frames are not annotated
    assert FrameDecoder(database=database).describe(frame(6)) == ''


def test_secured_pdus_inside_a_container(secoc_container_arxml):
    from can_explorer.core.security_events import SecurityMonitor

    database = DbcDecoder(secoc_container_arxml)
    assert [config.pdu for config in database.secoc_protected()] == ['SecuredStatus', 'SecuredStatus']
    decoder = FrameDecoder(database=database, secoc=verifier())
    monitor = SecurityMonitor(decoder)
    bundle = database.database.get_message_by_name('Bundle')
    secured = bundle.get_contained_message_by_name('SecuredStatus')
    sender = verifier()

    def container(data):
        payload = bundle.encode([(secured, data)])
        return can.Message(arbitration_id=0x200, is_extended_id=False, is_fd=True, data=payload, timestamp=1.0)

    good = container(sender.secure(CONFIG, b'\x05\x00', 1))
    assert decoder.describe(good) == '[SecuredStatus: SecOC ok, FV 1]'
    monitor.on_message(good)
    forged_data = sender.secure(CONFIG, b'\x05\x00', 2)
    forged = container(bytes([forged_data[0] ^ 1]) + forged_data[1:])
    assert decoder.describe(forged).startswith('[SecuredStatus: SecOC MAC ERROR')
    monitor.on_message(forged)
    [event] = monitor.rows()
    assert event['kind'] == 'secoc' and event['identifier'] == '200'
