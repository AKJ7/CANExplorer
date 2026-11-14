import asyncio
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from can_explorer.core import security_profiles, uds_auth
from can_explorer.core.keystore import KeyStore
from can_explorer.core.secoc_check import (
    ChainedSecOcKeys,
    KeyStoreSecOcKeys,
    SecOcConfig,
    SecOcVerifier,
    StaticSecOcKeys,
    compute_mac,
)
from can_explorer.core.security_profiles import (
    KeyServerProfile,
    LoadedProfile,
    ProfileRegistry,
    SecurityProfileError,
)
from can_explorer.core.simulation import UdsServer
from can_explorer.core.uds import UdsClient, UdsError, load_key_source

KEY = bytes(range(16))
CONFIG = SecOcConfig('SecuredStatus', 18, 2, 28, 32, 4, 'CMAC_AES128', 'FreshnessCounter')
TOKEN = 'secret-token'


class Loopback:
    def __init__(self, server: UdsServer):
        self.server, self._responses = server, []

    async def send(self, payload: bytes) -> None:
        self._responses += self.server.handle(bytes(payload))

    async def receive(self, timeout=None) -> bytes:
        return self._responses.pop(0)


@pytest.fixture
def registry():
    """A registry of its own as the application's, restored afterwards."""
    previous = security_profiles.registry()
    profiles = ProfileRegistry()
    security_profiles.set_registry(profiles)
    yield profiles
    security_profiles.set_registry(previous)


# Profiles from files


def test_profile_files(tmp_path):
    instance = tmp_path / 'instance.py'
    instance.write_text(
        'class P:\n'
        '    name = "OEM"\n'
        '    description = "Bench keys"\n'
        '    def compute_key(self, level, seed):\n'
        '        return bytes(b ^ level for b in seed)\n'
        'profile = P()\n'
    )
    factory = tmp_path / 'factory.py'
    factory.write_text('class P:\n    name = "Made"\ndef create_profile():\n    return P()\n')
    loaded = LoadedProfile(security_profiles.load_profile_file(instance), str(instance))
    assert loaded.as_dict() == {
        'name': 'OEM',
        'source': str(instance),
        'description': 'Bench keys',
        'capabilities': ['compute_key'],
    }
    assert security_profiles.load_profile_file(factory).name == 'Made'
    empty = tmp_path / 'empty.py'
    empty.write_text('x = 1\n')
    with pytest.raises(SecurityProfileError, match='defines neither profile nor create_profile'):
        security_profiles.load_profile_file(empty)
    broken = tmp_path / 'broken.py'
    broken.write_text('raise RuntimeError("no licence")\n')
    with pytest.raises(SecurityProfileError, match='Error in security profile broken.py: no licence'):
        security_profiles.load_profile_file(broken)


def test_entry_point_profiles(monkeypatch):
    class EntryPoint:
        def __init__(self, name, factory):
            self.name, self._factory = name, factory

        def load(self):
            if isinstance(self._factory, Exception):
                raise self._factory
            return self._factory

    class Profile:
        name = 'Installed'

    monkeypatch.setattr(
        security_profiles.importlib.metadata,
        'entry_points',
        lambda group: [EntryPoint('good', Profile), EntryPoint('bad', ImportError('missing'))],
    )
    [loaded] = security_profiles.entry_point_profiles()  # The broken plug-in does not stop the others
    assert loaded.name == 'Installed' and loaded.source == 'entry point good'


def test_registry_order_and_replacement():
    class Profile:
        def __init__(self, name, key=None):
            self.name, self._key = name, key

        def secoc_key(self, pdu, data_id):
            return (self._key, 'cmac-aes') if self._key else None

    profiles = ProfileRegistry()
    profiles.add(LoadedProfile(Profile('Empty'), 'a'))
    profiles.add(LoadedProfile(Profile('Keys', KEY), 'b'))
    key, reason = profiles.key_for(CONFIG)
    assert key.key == KEY and key.entry == 'profile:Keys' and reason == ''
    profiles.add(LoadedProfile(Profile('Keys', bytes(16)), 'c'))  # Same name: replaced
    assert len(profiles.profiles) == 2 and profiles.key_for(CONFIG)[0].key == bytes(16)
    profiles.remove('Keys')
    assert profiles.key_for(CONFIG) == (None, 'no security profile has a key for it')
    with pytest.raises(SecurityProfileError, match="No security profile named 'Keys'"):
        profiles.get('Keys')


# SecOC through profiles


def test_secoc_with_a_profile_authenticator():
    """A profile computing the MAC itself (e.g. in an HSM): frames verify without the key being here."""
    calls = []

    class Hsm:
        name = 'HSM'

        def secoc_authenticator(self, pdu, data_id, data):
            calls.append((pdu, data_id))
            return compute_mac('cmac-aes', KEY, data) if pdu == 'SecuredStatus' else None

    profiles = ProfileRegistry()
    profiles.add(LoadedProfile(Hsm(), 'test'))
    reference = SecOcVerifier(StaticSecOcKeys({'*': (KEY, '')}))
    frame = reference.secure(CONFIG, b'\x12\x34', 7)
    verifier = SecOcVerifier(profiles)
    check = verifier.check(CONFIG, frame)
    assert check.ok and check.freshness == 7
    assert calls[0] == ('SecuredStatus', 18)
    assert verifier.secure(CONFIG, b'\x12\x34', 8) == reference.secure(CONFIG, b'\x12\x34', 8)
    tampered = bytes([frame[0] ^ 1]) + frame[1:]
    assert not verifier.check(CONFIG, tampered).ok
    other = SecOcConfig('Other', 19, 2, 28, 32, 4, 'CMAC_AES128', 'FreshnessCounter')
    assert 'HSM has no key for Other' in verifier.check(other, frame).reason


def test_chained_keys_ask_the_profiles_first(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    store.add_secoc_key('Stored', bytes(16), '*')

    class Profile:
        name = 'Remote'

        def secoc_key(self, pdu, data_id):
            return (KEY, '') if data_id == 18 else None

    profiles = ProfileRegistry()
    chained = ChainedSecOcKeys(profiles, KeyStoreSecOcKeys(store))
    assert chained.key_for(CONFIG)[0].entry == 'Stored'
    profiles.add(LoadedProfile(Profile(), 'test'))
    chained.invalidate()
    assert chained.key_for(CONFIG)[0].entry == 'profile:Remote'
    store.lock()
    chained.invalidate()
    other = SecOcConfig('Other', 19, 2, 28, 32, 4, 'CMAC_AES128', 'FreshnessCounter')
    assert chained.key_for(other) == (None, 'the key store is locked')  # The last source's reason


# Seed/key and authentication through profiles


def test_seed_key_from_a_profile(registry):
    class Profile:
        name = 'OEM'

        def compute_key(self, level, seed):
            return None if level == 3 else bytes(b ^ 0xFF for b in seed)

    class NoSeedKey:
        name = 'Plain'

    registry.add(LoadedProfile(Profile(), 'test'))
    registry.add(LoadedProfile(NoSeedKey(), 'test'))
    compute_key = load_key_source('profile:OEM')
    assert compute_key(1, b'\x01\x02') == b'\xfe\xfd'
    with pytest.raises(UdsError, match="'OEM' has no key for level 3"):
        compute_key(3, b'\x01')
    with pytest.raises(UdsError, match="'Plain' has no seed/key algorithm"):
        load_key_source('profile:Plain')
    with pytest.raises(UdsError, match="No security profile named 'Missing'"):
        load_key_source('profile:Missing')


def _tester_pki():
    from tests.unit.core.test_uds_auth import issue

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = issue('OEM CA', ca_key, ca=True)
    tester_key = ec.generate_private_key(ec.SECP256R1())
    return ca, issue('Remote tester', tester_key, ca, ca_key), tester_key


async def test_certificate_exchange_with_a_profile(registry):
    ca, tester, tester_key = _tester_pki()
    signed = []

    class Hsm:
        name = 'HSM'

        def certificate(self):
            return tester

        def sign(self, data, scheme):
            signed.append(scheme)
            return uds_auth.sign(tester_key, data, scheme)

    registry.add(LoadedProfile(Hsm(), 'test'))
    certificate, signer = uds_auth.credentials(None, 'profile:HSM')
    assert certificate == tester and callable(signer)
    server = UdsServer(authentication=uds_auth.AuthenticationServer([ca], 'ecdsa-sha256'))
    result = await UdsClient(Loopback(server)).authenticate(certificate, signer, 'ecdsa-sha256')
    assert result.authenticated and signed == ['ecdsa-sha256']

    class SeedKeyOnly:
        name = 'Seeds'

        def compute_key(self, level, seed):
            return seed

    registry.add(LoadedProfile(SeedKeyOnly(), 'test'))
    with pytest.raises(uds_auth.UdsAuthenticationError, match='has no certificate to authenticate with'):
        uds_auth.credentials(None, 'profile:Seeds')


async def test_challenge_response_with_a_profile(registry):
    shared = b'shared-key'

    class Profile:
        name = 'ACR'

        def prove_challenge(self, challenge, indicator):
            return hmac.new(shared, challenge, hashlib.sha256).digest()

        def verify_challenge(self, challenge, proof, indicator):
            return hmac.compare_digest(hmac.new(shared, challenge, hashlib.sha256).digest(), proof)

    registry.add(LoadedProfile(Profile(), 'test'))
    prove, verify_server, how = uds_auth.challenge_response_credentials(None, 'profile:ACR')
    assert how == 'security profile ACR'
    acr = uds_auth.symmetric_challenge_response(shared, 'hmac-sha256')
    server = UdsServer(authentication=uds_auth.AuthenticationServer([], challenge_response=acr))
    result = await UdsClient(Loopback(server)).authenticate_challenge_response(prove, True, verify_server)
    assert result.authenticated and result.steps[-1] == "the ECU's proof verified"


# The key server


class KeyServer(BaseHTTPRequestHandler):
    """A key server answering the REST API of core/security_profiles.py (and 404 for /auth/prove)."""

    tester_key = None
    certificate_pem = ''
    requests = []

    def log_message(self, *args):
        pass

    def _answer(self, code, body=None):
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, body):
        KeyServer.requests.append((self.command, self.path, body))
        if self.headers.get('Authorization') != f'Bearer {TOKEN}':
            return self._answer(401)
        if self.command == 'GET' and self.path == '/secoc/keys':
            return self._answer(
                200, {'keys': [{'scope': '*', 'key': '00' * 16}, {'scope': 'SecuredStatus', 'key': KEY.hex()}]}
            )
        if self.command == 'POST' and self.path == '/uds/key':
            seed = bytes.fromhex(body['seed'])
            return self._answer(200, {'key': bytes(b ^ body['level'] for b in seed).hex()})
        if self.command == 'GET' and self.path == '/auth/certificate':
            return self._answer(200, {'certificate': KeyServer.certificate_pem})
        if self.command == 'POST' and self.path == '/auth/sign':
            signature = uds_auth.sign(KeyServer.tester_key, bytes.fromhex(body['data']), body['scheme'])
            return self._answer(200, {'signature': signature.hex()})
        if self.command == 'POST' and self.path == '/auth/verify':
            return self._answer(200, {'valid': body['proof'] == 'aa'})
        return self._answer(404)

    def do_GET(self):
        self._handle(None)

    def do_POST(self):
        self._handle(json.loads(self.rfile.read(int(self.headers['Content-Length']))))


@pytest.fixture
def key_server():
    ca, tester, tester_key = _tester_pki()
    KeyServer.tester_key = tester_key
    KeyServer.certificate_pem = tester.public_bytes(serialization.Encoding.PEM).decode()
    KeyServer.requests = []
    http = ThreadingHTTPServer(('127.0.0.1', 0), KeyServer)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_address[1]}/', ca, tester
    http.shutdown()
    http.server_close()


def test_key_server_profile(key_server):
    url, _ca, tester = key_server
    profile = KeyServerProfile('Server', url, TOKEN)
    assert TOKEN not in repr(profile) and profile.url == url.rstrip('/')
    assert security_profiles.capabilities(profile) == list(security_profiles.CAPABILITIES[:1]) + [
        'compute_key',
        'certificate',
        'sign',
        'prove_challenge',
        'verify_challenge',
    ]
    assert profile.secoc_key('SecuredStatus', 18) == (KEY, '')  # Named for the PDU: wins over '*'
    assert profile.secoc_key('Other', 19) == (bytes(16), '')
    assert sum(1 for request in KeyServer.requests if request[1] == '/secoc/keys') == 1  # Fetched once
    profile.forget()
    profile.secoc_key('Other', 19)
    assert sum(1 for request in KeyServer.requests if request[1] == '/secoc/keys') == 2
    assert profile.compute_key(1, b'\x10\x20') == b'\x11\x21'
    assert KeyServer.requests[-1] == ('POST', '/uds/key', {'level': 1, 'seed': '1020'})
    assert profile.certificate() == tester
    signature = profile.sign(b'challenge', 'ecdsa-sha256')
    assert uds_auth.verify(tester.public_key(), signature, b'challenge', 'ecdsa-sha256')
    assert profile.prove_challenge(b'\x01', bytes(16)) is None  # 404: not offered
    assert profile.verify_challenge(b'\x01', b'\xaa', bytes(16)) is True
    assert KeyServer.requests[-1][2] == {'challenge': '01', 'proof': 'aa', 'algorithm_indicator': '00' * 16}


def test_key_server_errors(key_server):
    url, _ca, _tester = key_server
    with pytest.raises(SecurityProfileError, match='refused /uds/key: HTTP 401'):
        KeyServerProfile('Server', url, 'wrong').compute_key(1, b'\x00')
    with pytest.raises(SecurityProfileError, match='cannot be reached'):
        KeyServerProfile('Server', 'http://127.0.0.1:9/', TOKEN, timeout=1).compute_key(1, b'\x00')
    with pytest.raises(SecurityProfileError, match='starts with https://'):
        KeyServerProfile('Server', 'ftp://keys', TOKEN)


async def test_key_server_signs_the_challenge_response(key_server, registry):
    """A key server without /auth/prove: the challenge is signed (/auth/sign), the private key never here."""
    url, _ca, tester = key_server
    registry.add(LoadedProfile(KeyServerProfile('Server', url, TOKEN), 'key server Server'))
    with pytest.raises(uds_auth.UdsAuthenticationError, match='choose the signature scheme'):
        prove, _verify, _how = uds_auth.challenge_response_credentials(None, 'profile:Server')
        await asyncio.to_thread(prove, b'\x01', bytes(16))
    prove, _verify, _how = uds_auth.challenge_response_credentials(None, 'profile:Server', 'ecdsa-sha256')
    acr = uds_auth.asymmetric_challenge_response([tester.public_key()], 'ecdsa-sha256')
    server = UdsServer(authentication=uds_auth.AuthenticationServer([], challenge_response=acr))
    result = await UdsClient(Loopback(server)).authenticate_challenge_response(prove)
    assert result.authenticated and server.authentication.authenticated
