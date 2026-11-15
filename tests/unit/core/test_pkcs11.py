"""The PKCS#11 token profile, against a simulated token (python-pkcs11's interface over keys held in memory): the
mechanisms the profile asks for, and the signatures it returns, are checked against the schemes of uds_auth."""

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import pkcs12

pkcs11 = pytest.importorskip('pkcs11')
from pkcs11 import MGF, Attribute, Mechanism, ObjectClass  # noqa: E402

from can_explorer.core import security_profiles, uds_auth  # noqa: E402
from can_explorer.core.keystore import KeyStore, KeyStoreError  # noqa: E402
from can_explorer.core.pkcs11 import Pkcs11Profile  # noqa: E402
from can_explorer.core.security_profiles import LoadedProfile, SecurityProfileError  # noqa: E402
from can_explorer.core.simulation import UdsServer  # noqa: E402
from can_explorer.core.uds import UdsClient  # noqa: E402
from tests.unit.core.test_uds_auth import Loopback, issue  # noqa: E402


class FakeKey:
    def __init__(self, private_key, mechanisms):
        self.private_key, self.mechanisms = private_key, mechanisms

    def sign(self, data, mechanism=None, mechanism_param=None):
        self.mechanisms.append((mechanism, mechanism_param))
        key = self.private_key
        kinds = {
            Mechanism.ECDSA_SHA256: ec.EllipticCurvePrivateKey,
            Mechanism.SHA256_RSA_PKCS: rsa.RSAPrivateKey,
            Mechanism.SHA256_RSA_PKCS_PSS: rsa.RSAPrivateKey,
            Mechanism.EDDSA: ed25519.Ed25519PrivateKey,
        }
        if mechanism in kinds and not isinstance(key, kinds[mechanism]):
            raise pkcs11.exceptions.KeyTypeInconsistent()  # CKR_KEY_TYPE_INCONSISTENT, as a token answers
        if mechanism == Mechanism.ECDSA_SHA256:  # PKCS#11: r ‖ s
            r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
            size = (key.curve.key_size + 7) // 8
            return r.to_bytes(size, 'big') + s.to_bytes(size, 'big')
        if mechanism == Mechanism.SHA256_RSA_PKCS:
            return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        if mechanism == Mechanism.SHA256_RSA_PKCS_PSS:
            assert mechanism_param == (Mechanism.SHA256, MGF.SHA256, 32)
            return key.sign(data, padding.PSS(padding.MGF1(hashes.SHA256()), 32), hashes.SHA256())
        if mechanism == Mechanism.EDDSA:
            return key.sign(data)
        raise pkcs11.exceptions.MechanismInvalid()


class FakeSession:
    def __init__(self, token):
        self.token = token

    def __enter__(self):
        self.token.sessions += 1
        return self

    def __exit__(self, *args):
        self.token.sessions -= 1

    def get_objects(self, attrs):
        assert attrs[Attribute.CLASS] == ObjectClass.CERTIFICATE
        certificate = self.token.certificates.get(attrs[Attribute.LABEL])
        return [{Attribute.VALUE: certificate.public_bytes(serialization.Encoding.DER)}] if certificate else []

    def get_key(self, object_class=None, label=None):
        assert object_class == ObjectClass.PRIVATE_KEY
        if label not in self.token.keys:
            raise pkcs11.exceptions.NoSuchKey()
        return FakeKey(self.token.keys[label], self.token.mechanisms)


class FakeToken:
    def __init__(self, label, pin):
        self.label, self.pin = label, pin
        self.keys, self.certificates, self.mechanisms = {}, {}, []
        self.sessions = 0

    def open(self, user_pin=None):
        if user_pin != self.pin:
            raise pkcs11.exceptions.PinIncorrect()
        return FakeSession(self)


class FakeLibrary:
    def __init__(self, *tokens):
        self.tokens = {token.label: token for token in tokens}

    def get_token(self, token_label=None):
        if token_label not in self.tokens:
            raise pkcs11.exceptions.NoSuchToken()
        return self.tokens[token_label]


@pytest.fixture
def token():
    card = FakeToken('Workshop card', '1234')
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = issue('OEM CA', ca_key, ca=True)
    for label, key in (
        ('ecdsa', ec.generate_private_key(ec.SECP256R1())),
        ('rsa', rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        ('ed25519', ed25519.Ed25519PrivateKey.generate()),
    ):
        card.keys[label] = key
        card.certificates[label] = issue(f'Tester {label}', key, ca, ca_key)
    card.ca = ca
    return card


def profile(token, key='ecdsa', pin='1234', library=None):
    return Pkcs11Profile('Card', '/usr/lib/vendor-pkcs11.so', token.label, key, pin, lambda module: library)


@pytest.mark.parametrize(
    'key, scheme',
    [
        ('ecdsa', 'ecdsa-sha256'),
        ('ecdsa', 'ecdsa-sha256-raw'),
        ('rsa', 'rsa-pss-sha256'),
        ('rsa', 'rsa-pkcs1-sha256'),
        ('ed25519', 'ed25519'),
    ],
)
def test_signatures_follow_the_schemes(token, key, scheme):
    card = profile(token, key, library=FakeLibrary(token))
    certificate = card.certificate()
    assert certificate == token.certificates[key]
    signature = card.sign(b'challenge from the ECU', scheme)
    assert uds_auth.verify(certificate.public_key(), signature, b'challenge from the ECU', scheme)
    assert token.sessions == 0  # Each operation opens and closes its session


def test_token_errors(token):
    library = FakeLibrary(token)
    assert 'PIN' not in repr(profile(token, library=library)) and '1234' not in repr(profile(token, library=library))
    with pytest.raises(SecurityProfileError, match='Wrong PIN'):
        profile(token, pin='0000', library=library).sign(b'x', 'ecdsa-sha256')
    with pytest.raises(SecurityProfileError, match='No PKCS#11 token labelled'):
        profile(token, library=FakeLibrary()).sign(b'x', 'ecdsa-sha256')
    with pytest.raises(SecurityProfileError, match="No single private key labelled 'missing'"):
        profile(token, 'missing', library=library).sign(b'x', 'ecdsa-sha256')
    assert profile(token, 'missing', library=library).certificate() is None
    with pytest.raises(SecurityProfileError, match='cannot sign with the scheme'):
        profile(token, library=library).sign(b'x', 'hmac')
    with pytest.raises(SecurityProfileError, match='could not sign'):
        profile(token, 'ed25519', library=library).sign(b'x', 'rsa-pss-sha256')  # The token refuses the mechanism


async def test_uds_authentication_with_the_token(token):
    previous = security_profiles.registry()
    registry = security_profiles.ProfileRegistry()
    security_profiles.set_registry(registry)
    try:
        registry.add(LoadedProfile(profile(token, library=FakeLibrary(token)), 'pkcs11 Card'))
        certificate, signer = uds_auth.credentials(None, 'profile:Card')
        server = UdsServer(authentication=uds_auth.AuthenticationServer([token.ca], 'ecdsa-sha256'))
        result = await UdsClient(Loopback(server)).authenticate(certificate, signer, 'ecdsa-sha256')
        assert result.authenticated and token.mechanisms[-1] == (Mechanism.ECDSA_SHA256, None)
    finally:
        security_profiles.set_registry(previous)


def test_pkcs12_files_and_pkcs11_entries(token, tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    key, certificate = token.keys['ecdsa'], token.certificates['ecdsa']
    data = pkcs12.serialize_key_and_certificates(
        b'tester', key, certificate, [token.ca], serialization.BestAvailableEncryption(b'p12-password')
    )
    info = store.add_tls_client_pkcs12('Tester', data, 'p12-password')
    assert info.params['count'] == '2'  # The certificate and its chain
    loaded, private_key = uds_auth.credentials(store, 'Tester')
    assert loaded == certificate and private_key.private_numbers() == key.private_numbers()
    with pytest.raises(KeyStoreError, match='wrong password'):
        store.add_tls_client_pkcs12('Other', data, 'nope')
    only_certificate = pkcs12.serialize_key_and_certificates(
        b'ca', None, None, [token.ca], serialization.NoEncryption()
    )
    with pytest.raises(KeyStoreError, match='no certificate with its private key'):
        store.add_tls_client_pkcs12('Other', only_certificate)

    info = store.add_pkcs11_key('Card', '/usr/lib/vendor-pkcs11.so', 'Workshop card', 'ecdsa', '1234')
    assert info.params == {'module': '/usr/lib/vendor-pkcs11.so', 'token': 'Workshop card', 'key': 'ecdsa'}
    assert store.secrets('Card') == {'pin': b'1234'}
    with pytest.raises(KeyStoreError, match='needs the module'):
        store.add_pkcs11_key('Bad', '', 'Workshop card', 'ecdsa')
