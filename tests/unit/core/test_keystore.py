import datetime
import json
import os
import stat
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from can_explorer.core import keystore
from can_explorer.core.keystore import KeyStore, KeyStoreError, Locked, WrongPassword, default_path, parse_hex

FAST = 2**4  # scrypt cost for tests


def make_certificate(name='DoIP tester', days=30, issuer_key=None, issuer_name=None):
    """A self-signed (or issuer-signed) EC certificate and its private key."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name or subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=issuer_key is None, path_length=None), critical=True)
        .sign(issuer_key or key, hashes.SHA256())
    )
    return certificate, key


def pem(certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def key_pem(key, password=None) -> bytes:
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)


@pytest.fixture
def store(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=FAST)
    store.create('correct horse')
    return store


def test_create_unlock_lock(tmp_path):
    path = tmp_path / 'sub' / 'keys.json'
    store = KeyStore(path, scrypt_cost=FAST)
    assert not store.exists and store.entries() == []
    store.create('secret')
    assert store.exists and store.unlocked
    if sys.platform != 'win32':
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # Owner only
    store.lock()
    assert not store.unlocked
    with pytest.raises(WrongPassword):
        store.unlock('wrong')
    store.unlock('secret')
    assert store.unlocked
    with pytest.raises(KeyStoreError, match='exists already'):
        store.create('again')
    with pytest.raises(KeyStoreError, match='needs a password'):
        KeyStore(tmp_path / 'other.json', scrypt_cost=FAST).create('')


def test_secrets_are_encrypted_and_metadata_is_listed_while_locked(store):
    key = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
    store.add_secoc_key('Powertrain', key, scope='EngineData, 0x12')
    text = store.path.read_text(encoding='utf-8')
    assert key.hex() not in text and 'correct horse' not in text
    store.lock()
    [info] = store.entries()
    assert info.name == 'Powertrain' and info.purpose == keystore.SECOC
    assert info.params == {'scope': 'EngineData, 0x12', 'algorithm': ''}
    assert info.secret_names == ('key',)
    with pytest.raises(Locked):
        store.secrets('Powertrain')
    with pytest.raises(Locked):
        store.add_secoc_key('Other', key)
    store.unlock('correct horse')
    assert store.secrets('Powertrain') == {'key': key}
    assert 'correct horse' not in repr(store) and key.hex() not in repr(store.entries())


def test_reopened_by_another_instance(store):
    store.add_macsec_sa('Zone', bytes(16), sci=0x0011223344550001)
    other = KeyStore(store.path, scrypt_cost=FAST)
    assert other.names() == ['Zone'] and not other.unlocked
    other.unlock('correct horse')
    assert other.secrets('Zone') == {'sak': bytes(16)}


def test_editing_metadata_in_the_file_breaks_the_entry(store):
    store.add_secoc_key('Narrow', bytes(16), scope='EngineData')
    data = json.loads(store.path.read_text(encoding='utf-8'))
    data['entries'][0]['params']['scope'] = '*'  # Try to widen the key's use without the password
    store.path.write_text(json.dumps(data), encoding='utf-8')
    reopened = KeyStore(store.path, scrypt_cost=FAST)
    reopened.unlock('correct horse')
    with pytest.raises(KeyStoreError, match='damaged or was modified'):
        reopened.secrets('Narrow')


def test_names_are_unique_unless_replaced(store):
    store.add_secoc_key('K', bytes(16))
    with pytest.raises(KeyStoreError, match='exists already'):
        store.add_secoc_key('K', bytes(16))
    store.add_secoc_key('K', bytes([1]) * 16, replace=True)
    assert store.secrets('K') == {'key': bytes([1]) * 16}
    store.remove('K')
    assert store.entries() == []
    with pytest.raises(KeyStoreError, match='No key store entry'):
        store.remove('K')


def test_change_password(store):
    store.add_secoc_key('K', bytes(range(16)))
    with pytest.raises(WrongPassword):
        store.change_password('wrong', 'new')
    store.change_password('correct horse', 'battery staple')
    reopened = KeyStore(store.path, scrypt_cost=FAST)
    with pytest.raises(WrongPassword):
        reopened.unlock('correct horse')
    reopened.unlock('battery staple')
    assert reopened.secrets('K') == {'key': bytes(range(16))}


def test_unlock_from_environment(store):
    store.lock()
    assert not store.unlock_from_environment({})
    assert store.unlock_from_environment({keystore.PASSWORD_VARIABLE: 'correct horse'})
    assert store.unlocked
    with pytest.raises(WrongPassword):
        store.unlock_from_environment({keystore.PASSWORD_VARIABLE: 'nope'})


def test_not_a_key_store(tmp_path):
    path = tmp_path / 'keys.json'
    path.write_text('{"format": "something else"}', encoding='utf-8')
    with pytest.raises(KeyStoreError, match='not a CANExplorer key store'):
        KeyStore(path).entries()


@pytest.mark.parametrize(
    'kwargs, message',
    [
        ({'key': bytes(15)}, '16, 24 or 32 bytes'),
        ({'key': bytes(16), 'algorithm': 'md5'}, 'Unknown SecOC algorithm'),
        ({'key': b'', 'algorithm': 'hmac-sha256'}, 'empty'),
    ],
)
def test_secoc_key_validation(store, kwargs, message):
    with pytest.raises(KeyStoreError, match=message):
        store.add_secoc_key('K', **kwargs)


def test_secoc_hmac_key_may_have_any_length(store):
    info = store.add_secoc_key('H', bytes(20), algorithm='hmac-sha256', scope=' A ,, B ')
    assert info.params == {'scope': 'A, B', 'algorithm': 'hmac-sha256'}


def test_tls_client_certificate(store):
    certificate, key = make_certificate()
    info = store.add_tls_client('Tester', pem(certificate), key_pem(key, b'pw'), key_password=b'pw')
    assert info.purpose == keystore.TLS_CLIENT
    assert info.params['subject'] == 'CN=DoIP tester' and info.params['count'] == '1'
    assert info.params['certificate'].startswith('-----BEGIN CERTIFICATE-----')
    private = store.secrets('Tester')['private_key']
    assert b'ENCRYPTED' not in private  # Re-serialised: the store's encryption protects it
    assert serialization.load_pem_private_key(private, None).public_key() == key.public_key()


def test_tls_client_rejects_a_foreign_key_and_bad_input(store):
    certificate, _ = make_certificate()
    _, other_key = make_certificate()
    with pytest.raises(KeyStoreError, match='does not belong'):
        store.add_tls_client('T', pem(certificate), key_pem(other_key))
    with pytest.raises(KeyStoreError, match='Cannot read the private key'):
        store.add_tls_client('T', pem(certificate), key_pem(other_key, b'pw'))  # Missing password
    with pytest.raises(KeyStoreError, match='Cannot read the certificate'):
        store.add_tls_client('T', b'not a certificate', key_pem(other_key))


def test_tls_ca_and_expiry(store):
    old, _ = make_certificate('Old CA', days=-1)  # Expired yesterday
    new, _ = make_certificate('New CA')
    info = store.add_tls_ca('Trusted', pem(new) + pem(old))
    assert info.params['count'] == '2' and info.secret_names == ()
    assert keystore.expired(info)  # The earliest expiry counts
    assert not keystore.expired(store.add_tls_ca('Fresh', pem(new)))
    assert not keystore.expired(
        store.entries_by_name()['Fresh'], now=datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    )


@pytest.mark.parametrize(
    'args, message',
    [
        ((0, 'aes-gcm', bytes(20)), 'SPI'),
        ((1, 'des', bytes(8)), 'Unknown ESP encryption'),
        ((1, 'aes-gcm', bytes(16)), 'salt'),
        ((1, 'aes-gcm', bytes(20), 'hmac-sha256-128', bytes(32)), 'authenticates by itself'),
        ((1, 'aes-cbc', bytes(16)), 'needs an integrity algorithm'),
        ((1, 'aes-cbc', bytes(16), 'hmac-sha256-128'), 'needs a key'),
        ((1, 'aes-cbc', bytes(10), 'hmac-sha256-128', bytes(32)), '16, 24 or 32'),
        ((1, 'null', bytes(16), 'hmac-sha1-96', bytes(20)), 'no key'),
    ],
)
def test_ipsec_validation(store, args, message):
    with pytest.raises(KeyStoreError, match=message):
        store.add_ipsec_sa('SA', *args)


def test_ipsec_and_macsec_entries(store):
    store.add_ipsec_sa('GCM', 0x1000, 'aes-gcm', bytes(20))
    store.add_ipsec_sa('CBC', 0x2000, 'aes-cbc', bytes(16), 'hmac-sha256-128', bytes(32))
    store.add_macsec_sa('Any', bytes(32))
    by_name = store.entries_by_name()
    assert by_name['GCM'].params == {'spi': '0x00001000', 'encryption': 'aes-gcm', 'integrity': 'none'}
    assert by_name['Any'].params == {'sci': '', 'cipher': 'gcm-aes-256'}
    assert store.secrets('CBC') == {'encryption_key': bytes(16), 'integrity_key': bytes(32)}
    with pytest.raises(KeyStoreError, match='SAK'):
        store.add_macsec_sa('Bad', bytes(24))


def test_default_path():
    home = keystore.Path('/home/u')
    assert default_path({}, 'linux', home) == keystore.Path('/home/u/.config/CANExplorer/keystore.json')
    assert default_path({'XDG_CONFIG_HOME': '/x'}, 'linux', home) == keystore.Path('/x/CANExplorer/keystore.json')
    assert default_path({'APPDATA': 'C:/R'}, 'win32', home) == keystore.Path('C:/R/CANExplorer/keystore.json')
    assert default_path({}, 'darwin', home).parts[-3:] == ('Application Support', 'CANExplorer', 'keystore.json')
    assert default_path({'CANEXPLORER_KEYSTORE': '/k.json'}, 'linux', home) == keystore.Path('/k.json')


@pytest.mark.parametrize('text', ['00112233', '00 11 22 33', '00:11:22:33', '0x00112233', '00-11-22-33\n'])
def test_parse_hex(text):
    assert parse_hex(text) == bytes.fromhex('00112233')


def test_parse_hex_rejects_garbage():
    with pytest.raises(KeyStoreError, match='not hexadecimal'):
        parse_hex('xyz', 'SAK')


SEED_KEY_SCRIPT = b'SECRET = 0x5A\n\ndef compute_key(level, seed):\n    return bytes(byte ^ SECRET for byte in seed)\n'


def test_seed_key_algorithms(store):
    from can_explorer.core.uds import UdsError, load_key_source

    info = store.add_seed_key('OEM level 1', SEED_KEY_SCRIPT, 'oem_key.py')
    assert info.purpose == keystore.SEED_KEY and info.params == {'file': 'oem_key.py'}
    assert b'SECRET' not in store.path.read_bytes()  # Encrypted like every secret
    compute_key = load_key_source('keystore:OEM level 1', store)
    assert compute_key(1, b'\x00\xff') == b'\x5a\xa5'
    with pytest.raises(KeyStoreError, match='does not compile'):
        store.add_seed_key('Bad', b'def compute_key(:\n')
    with pytest.raises(KeyStoreError, match='does not define compute_key'):
        store.add_seed_key('Bad', b'def other(level, seed):\n    return seed\n')
    with pytest.raises(UdsError, match="Seed/key algorithm 'Missing'"):
        load_key_source('keystore:Missing', store)
    store.add_seed_key('Raises', b'raise RuntimeError("boom")\ndef compute_key(level, seed):\n    return seed\n')
    with pytest.raises(UdsError, match="Error in seed/key algorithm 'Raises': boom"):
        load_key_source('keystore:Raises', store)
    store.lock()
    with pytest.raises(UdsError, match='locked'):
        load_key_source('keystore:OEM level 1', store)


def test_key_sources_default_to_the_application_store(store, tmp_path):
    from can_explorer.core.uds import load_key_source

    store.add_seed_key('Default', SEED_KEY_SCRIPT)
    previous = keystore.default_store()
    keystore.set_default_store(store)
    try:
        assert load_key_source('keystore:Default')(1, b'\x01') == b'\x5b'
    finally:
        keystore.set_default_store(previous)
    script = tmp_path / 'key.py'
    script.write_bytes(SEED_KEY_SCRIPT)
    assert load_key_source(str(script))(1, b'\x01') == b'\x5b'  # Files work as before


def test_expires_in_days(store):
    soon, _ = make_certificate('Soon', days=10)
    info = store.add_tls_ca('Soon', pem(soon))
    assert 9 < keystore.expires_in_days(info) <= 10
    assert keystore.expires_in_days(store.add_secoc_key('K', bytes(16))) is None


def test_bundles_move_entries_between_stores(store, tmp_path):
    store.add_secoc_key('SecOC', bytes(range(16)), scope='EngineData')
    store.add_macsec_sa('Zone', bytes(16))
    bundle = tmp_path / 'keys.ckb'
    assert store.export_bundle(bundle, ['SecOC'], 'bundle pass') == 1
    assert bytes(range(16)).hex() not in bundle.read_text()
    other = KeyStore(tmp_path / 'other.json', scrypt_cost=FAST)
    other.create('other password')
    with pytest.raises(KeyStoreError, match='Wrong passphrase'):
        other.import_bundle(bundle, 'nope')
    assert other.import_bundle(bundle, 'bundle pass') == (['SecOC'], [])
    assert other.secrets('SecOC') == {'key': bytes(range(16))}
    assert other.entries_by_name()['SecOC'].params == {'scope': 'EngineData', 'algorithm': ''}
    assert other.import_bundle(bundle, 'bundle pass') == ([], ['SecOC'])  # Existing entries are kept …
    assert other.import_bundle(bundle, 'bundle pass', replace=True) == (['SecOC'], [])  # … unless replaced
    with pytest.raises(KeyStoreError, match="No key store entry named 'Missing'"):
        store.export_bundle(bundle, ['Missing'], 'x')
    with pytest.raises(KeyStoreError, match='needs a passphrase'):
        store.export_bundle(bundle, ['SecOC'], '')
    store.lock()
    with pytest.raises(Locked):
        store.export_bundle(bundle, ['SecOC'], 'x')
    (tmp_path / 'not.ckb').write_text('{}')
    with pytest.raises(KeyStoreError, match='not a CANExplorer key bundle'):
        other.import_bundle(tmp_path / 'not.ckb', 'x')


def test_keychain(tmp_path):
    from can_explorer.core import keychain

    path = tmp_path / 'keys.json'
    assert keychain.available()  # The tests' in-memory keychain
    assert keychain.recall(path) is None
    keychain.remember(path, 'secret')
    assert keychain.recall(path) == 'secret' and keychain.recall(tmp_path / 'other.json') is None
    keychain.forget(path)
    keychain.forget(path)  # Forgetting twice is fine
    assert keychain.recall(path) is None


def test_keychain_unavailable(monkeypatch, tmp_path):
    import keyring.backends.fail

    from can_explorer.core import keychain

    monkeypatch.setattr(keychain, '_keyring', lambda: type('K', (), {'get_keyring': keyring.backends.fail.Keyring})())
    assert not keychain.available() and keychain.recall(tmp_path / 'keys.json') is None


def test_uds_authentication_keys(store):
    info = store.add_auth_key('Shared', bytes(range(16)), 'aes-cmac')
    assert info.purpose == keystore.AUTH_KEY and info.params == {'mac': 'aes-cmac'}
    assert store.secrets('Shared') == {'key': bytes(range(16))}
    assert store.add_auth_key('Any length', b'k').params == {'mac': 'hmac-sha256'}
    for args, message in (
        ((b'', 'hmac-sha256'), 'empty'),
        ((b'short', 'aes-cmac'), '16, 24 or 32 bytes'),
        ((b'k', 'md5'), 'Unknown MAC'),
    ):
        with pytest.raises(KeyStoreError, match=message):
            store.add_auth_key('Bad', *args)


def test_key_servers(store):
    with pytest.raises(KeyStoreError, match='starts with https://'):
        store.add_key_server('Server', 'keys.example.com', 'token')
    with pytest.raises(KeyStoreError, match="No trusted certificates named 'OEM'"):
        store.add_key_server('Server', 'https://keys.example.com', 'token', 'OEM')
    info = store.add_key_server('Server', ' https://keys.example.com/api ', 'bearer-s3cr3t')
    assert info.params == {'url': 'https://keys.example.com/api', 'trusted': ''}
    assert store.secrets('Server') == {'token': b'bearer-s3cr3t'}  # The token is a secret, kept encrypted
    assert 'bearer-s3cr3t' not in store.path.read_text()
