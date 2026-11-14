import datetime
import hmac

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import NameOID

from can_explorer.core import uds_auth
from can_explorer.core.keystore import KeyStore
from can_explorer.core.simulation import UdsServer
from can_explorer.core.uds import UdsClient, UdsNegativeResponse, describe_request, describe_response, nrc_name
from can_explorer.core.uds_auth import AuthenticationServer, UdsAuthenticationError


def name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def issue(subject, key, issuer=None, issuer_key=None, days=30, start_days=-1, ca=False):
    now = datetime.datetime.now(datetime.timezone.utc)
    algorithm = None if isinstance(issuer_key or key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return (
        x509.CertificateBuilder()
        .subject_name(name(subject))
        .issuer_name(issuer.subject if issuer else name(subject))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=start_days))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(issuer_key or key, algorithm)
    )


@pytest.fixture(scope='module')
def pki():
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = issue('OEM CA', ca_key, ca=True)
    tester_key = ec.generate_private_key(ec.SECP256R1())
    ecu_key = ec.generate_private_key(ec.SECP256R1())
    return {
        'ca': ca,
        'ca_key': ca_key,
        'tester': issue('Tester', tester_key, ca, ca_key),
        'tester_key': tester_key,
        'ecu': issue('ECU', ecu_key, ca, ca_key),
        'ecu_key': ecu_key,
    }


class Loopback:
    """A UDS transport answered by a simulated ECU."""

    def __init__(self, server: UdsServer):
        self.server, self._responses = server, []

    async def send(self, payload: bytes) -> None:
        self._responses += self.server.handle(bytes(payload))

    async def receive(self, timeout=None) -> bytes:
        return self._responses.pop(0)


def ecu(pki, scheme='ecdsa-sha256', **kwargs):
    authentication = AuthenticationServer([pki['ca']], scheme, pki['ecu'], pki['ecu_key'], **kwargs)
    return UdsServer(authentication=authentication, authentication_dids=(0xF198,))


# Messages


def test_requests():
    assert uds_auth.request(0x00) == bytes([0x29, 0x00])
    assert uds_auth.request(0x01, 0, certificate=b'\xaa\xbb') == bytes.fromhex('2901 00 0002aabb 0000')
    assert uds_auth.request(0x02, 3, certificate=b'\xaa', challenge=b'\x01\x02') == bytes.fromhex(
        '2902 03 0001aa 00020102'
    )
    assert uds_auth.request(0x03, proof_of_ownership=b'\x11\x22') == bytes.fromhex('2903 00021122 0000')
    assert uds_auth.request(0x04, certificate=b'\xcc', certificate_evaluation_id=0x0102) == bytes.fromhex(
        '2904 0102 0001cc'
    )
    indicator = bytes(range(16))
    assert uds_auth.request(0x05, 1, algorithm_indicator=indicator) == bytes([0x29, 0x05, 0x01]) + indicator
    assert uds_auth.request(0x06, proof_of_ownership=b'\x01', challenge=b'\x02', algorithm_indicator=indicator) == (
        bytes([0x29, 0x06]) + indicator + bytes.fromhex('000101 000102 0000')
    )
    assert uds_auth.request(0x08) == bytes([0x29, 0x08])
    with pytest.raises(UdsAuthenticationError, match='16 bytes'):
        uds_auth.request(0x05, algorithm_indicator=b'\x01')
    with pytest.raises(UdsAuthenticationError, match='Unknown authentication task'):
        uds_auth.request(0x09)
    with pytest.raises(UdsAuthenticationError, match='does not fit'):
        uds_auth.request(0x01, certificate=bytes(0x10000))


def test_responses():
    response = uds_auth.parse_response(bytes.fromhex('6901 11 0003010203 0000'))
    assert response.challenge == b'\x01\x02\x03' and response.return_text.startswith('certificate verified, ownership')
    response = uds_auth.parse_response(bytes.fromhex('6902 11 000101 000102 000103 0000'))
    assert (response.challenge, response.certificate, response.proof_of_ownership) == (b'\x01', b'\x02', b'\x03')
    assert uds_auth.parse_response(bytes.fromhex('6903 12 0000')).return_parameter == 0x12
    indicator = bytes(range(16))
    response = uds_auth.parse_response(bytes([0x69, 0x05, 0x00]) + indicator + bytes.fromhex('000155 0000'))
    assert response.algorithm_indicator == indicator and response.challenge == b'\x55'
    assert uds_auth.parse_response(bytes.fromhex('6900 10')).return_text == 'de-authentication successful'
    for bad, message in (
        ('6901 11 0005 0102', 'ends before its server challenge'),
        ('6903 12 0000 ff', 'unexpected bytes'),
        ('7f29 51', 'Not a positive'),
    ):
        with pytest.raises(UdsAuthenticationError, match=message):
            uds_auth.parse_response(bytes.fromhex(bad.replace(' ', '')))


def test_descriptions_and_nrcs():
    assert describe_request(uds_auth.request(0x01, certificate=bytes(300), challenge=b'')) == (
        'Authentication verifyCertificateUnidirectional, certificate 300 bytes'
    )
    assert describe_request(uds_auth.request(0x03, proof_of_ownership=bytes(64))) == (
        'Authentication proofOfOwnership, proof 64 bytes'
    )
    assert describe_response(bytes.fromhex('6901 11 0003010203 0000'.replace(' ', ''))) == (
        'Positive response to Authentication verifyCertificateUnidirectional: certificate verified, ownership '
        'verification necessary, challenge 3 bytes'
    )
    assert (
        describe_response(bytes([0x7F, 0x29, 0x58]))
        == 'Negative response to Authentication: ownershipVerificationFailed'
    )
    assert nrc_name(0x34) == 'authenticationRequired' and nrc_name(0x5D) == 'deAuthenticationFailed'


# Signatures and certificates


@pytest.mark.parametrize(
    'scheme, key',
    [
        ('ecdsa-sha256', ec.generate_private_key(ec.SECP256R1())),
        ('ecdsa-sha256-raw', ec.generate_private_key(ec.SECP384R1())),
        ('ed25519', ed25519.Ed25519PrivateKey.generate()),
        ('rsa-pss-sha256', rsa.generate_private_key(65537, 2048)),
        ('rsa-pkcs1-sha256', rsa.generate_private_key(65537, 2048)),
    ],
)
def test_signature_schemes(scheme, key):
    challenge = b'challenge-from-the-ecu'
    proof = uds_auth.sign(key, challenge, scheme)
    assert uds_auth.verify(key.public_key(), proof, challenge, scheme)
    assert not uds_auth.verify(key.public_key(), proof, challenge + b'!', scheme)
    if scheme == 'ecdsa-sha256-raw':
        assert len(proof) == 96  # r and s of P-384, 48 bytes each
        assert not uds_auth.verify(key.public_key(), proof[:-1], challenge, scheme)


def test_scheme_must_fit_the_key():
    with pytest.raises(UdsAuthenticationError, match='does not fit the signature scheme ed25519'):
        uds_auth.sign(ec.generate_private_key(ec.SECP256R1()), b'x', 'ed25519')
    with pytest.raises(UdsAuthenticationError, match='Unknown signature scheme'):
        uds_auth.sign(ec.generate_private_key(ec.SECP256R1()), b'x', 'md5')
    assert uds_auth.scheme_for(ed25519.Ed25519PrivateKey.generate()) == 'ed25519'


def test_certificate_problems(pki):
    trusted = [pki['ca']]
    assert uds_auth.certificate_problem(pki['tester'], trusted) is None
    assert uds_auth.certificate_problem(pki['ca'], trusted) is None  # A trusted certificate itself
    other_key = ec.generate_private_key(ec.SECP256R1())
    expired = issue('Old tester', other_key, pki['ca'], pki['ca_key'], days=-1, start_days=-10)
    assert uds_auth.certificate_problem(expired, trusted) == uds_auth.INVALID_TIME_PERIOD
    stranger_ca_key = ec.generate_private_key(ec.SECP256R1())
    stranger = issue('Stranger', other_key, issue('Other CA', stranger_ca_key, ca=True), stranger_ca_key)
    assert uds_auth.certificate_problem(stranger, trusted) == uds_auth.INVALID_CHAIN_OF_TRUST
    forged_issuer = issue('OEM CA', stranger_ca_key, ca=True)  # Same name as the trusted CA, different key
    forged = issue('Forged', other_key, forged_issuer, stranger_ca_key)
    assert uds_auth.certificate_problem(forged, trusted) == uds_auth.INVALID_SIGNATURE
    der = pki['tester'].public_bytes(serialization.Encoding.DER)
    assert uds_auth.load_certificate(der) == pki['tester']
    with pytest.raises(UdsAuthenticationError, match='Not an X.509 certificate'):
        uds_auth.load_certificate(b'garbage')


# The exchange, against the simulated ECU


async def test_unidirectional_authentication_unlocks_protected_data(pki):
    server = ecu(pki)
    client = UdsClient(Loopback(server))
    with pytest.raises(UdsNegativeResponse) as refused:
        await client.read_data_by_identifier(0xF198)
    assert refused.value.code == 0x34  # authenticationRequired
    result = await client.authenticate(pki['tester'], pki['tester_key'], 'ecdsa-sha256')
    assert result.authenticated and server.authentication.authenticated
    assert result.description == (
        'verifyCertificateUnidirectional: certificate verified, ownership verification necessary; '
        'proofOfOwnership: ownership verified, authentication complete'
    )
    server.dids[0xF198] = b'SHOP'
    assert await client.read_data_by_identifier(0xF198) == b'SHOP'
    assert await client.deauthenticate() == 'de-authentication successful'
    assert not server.authentication.authenticated
    assert await uds_auth.authentication_configuration(client) == (
        'authentication configuration APCE (certificate exchange)'
    )


async def test_bidirectional_authentication_checks_the_ecu(pki):
    client = UdsClient(Loopback(ecu(pki)))
    result = await client.authenticate(pki['tester'], pki['tester_key'], 'ecdsa-sha256', True, [pki['ca']])
    assert result.authenticated and result.server_certificate == pki['ecu']
    assert 'ECU certificate CN=ECU trusted and proven' in result.description
    other_ca_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(UdsAuthenticationError, match='not trusted: certificateVerificationFailed-InvalidChainOfTrust'):
        await client.authenticate(
            pki['tester'], pki['tester_key'], 'ecdsa-sha256', True, [issue('Other', other_ca_key, ca=True)]
        )
    # An ECU proving with a key that is not its certificate's
    impostor = ecu(pki)
    impostor.authentication.private_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(UdsAuthenticationError, match='proof of ownership of its certificate does not verify'):
        await UdsClient(Loopback(impostor)).authenticate(pki['tester'], pki['tester_key'], 'ecdsa-sha256', True)


async def test_the_ecu_refuses_bad_certificates_and_proofs(pki):
    server = ecu(pki)
    client = UdsClient(Loopback(server))
    stranger_key = ec.generate_private_key(ec.SECP256R1())
    stranger = issue('Stranger', stranger_key)
    with pytest.raises(UdsNegativeResponse) as refused:
        await client.authenticate(stranger, stranger_key, 'ecdsa-sha256')
    assert nrc_name(refused.value.code) == 'certificateVerificationFailed-InvalidChainOfTrust'
    # The right certificate, but a proof made with another key: ownership is not proven
    with pytest.raises(UdsNegativeResponse) as refused:
        await client.authenticate(pki['tester'], stranger_key, 'ecdsa-sha256')
    assert refused.value.code == 0x58 and not server.authentication.authenticated
    # A proof in a scheme the ECU does not use
    with pytest.raises(UdsNegativeResponse) as refused:
        await UdsClient(Loopback(ecu(pki, 'ecdsa-sha256-raw'))).authenticate(
            pki['tester'], pki['tester_key'], 'ecdsa-sha256'
        )
    assert refused.value.code == 0x58


def test_ecu_message_checks(pki):
    server = ecu(pki).authentication
    assert server.handle(uds_auth.request(0x03, proof_of_ownership=b'\x01')) == bytes([0x7F, 0x29, 0x24])
    assert server.handle(uds_auth.request(0x01, 1, certificate=b'\x01')) == bytes([0x7F, 0x29, 0x31])
    assert server.handle(uds_auth.request(0x01, 0, certificate=b'')) == bytes([0x7F, 0x29, 0x13])
    assert server.handle(bytes.fromhex('290100 0005 01')) == bytes([0x7F, 0x29, 0x13])
    assert server.handle(uds_auth.request(0x01, 0, certificate=b'not a certificate')) == bytes([0x7F, 0x29, 0x54])
    assert server.handle(uds_auth.request(0x05, 0, algorithm_indicator=bytes(16))) == bytes([0x7F, 0x29, 0x12])
    ed_key = ed25519.Ed25519PrivateKey.generate()
    ed_certificate = issue('Ed tester', ed_key, pki['ca'], pki['ca_key'])
    ed_der = ed_certificate.public_bytes(serialization.Encoding.DER)
    assert server.handle(uds_auth.request(0x01, 0, certificate=ed_der)) == bytes([0x7F, 0x29, 0x53])  # Wrong key type
    assert UdsServer().handle(bytes([0x29, 0x00])) == [bytes([0x7F, 0x29, 0x11])]  # ECUs without the service


async def test_credentials_from_the_key_store(pki, tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    pem = pki['tester'].public_bytes(serialization.Encoding.PEM)
    key_pem = pki['tester_key'].private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    store.add_tls_client('Tester', pem, key_pem)
    store.add_tls_ca('OEM', pki['ca'].public_bytes(serialization.Encoding.PEM))
    certificate, private_key = uds_auth.credentials(store, 'Tester')
    assert certificate == pki['tester']
    assert uds_auth.trusted_certificates(store, 'OEM') == [pki['ca']]
    result = await UdsClient(Loopback(ecu(pki))).authenticate(certificate, private_key, 'ecdsa-sha256')
    assert result.authenticated
    with pytest.raises(UdsAuthenticationError, match="No client certificate named 'OEM'"):
        uds_auth.credentials(store, 'OEM')
    store.lock()
    with pytest.raises(UdsAuthenticationError, match='locked'):
        uds_auth.credentials(store, 'Tester')
    assert uds_auth.trusted_certificates(store, 'OEM') == [pki['ca']]  # Certificates are readable while locked


# Authentication by challenge-response (ACR)


def test_algorithm_indicator():
    assert uds_auth.algorithm_indicator('') == bytes(16)
    assert uds_auth.algorithm_indicator('01 02') == b'\x01\x02' + bytes(14)
    # SHA-256 (2.16.840.1.101.3.4.2.1), DER encoded with its tag and length
    assert uds_auth.algorithm_indicator('2.16.840.1.101.3.4.2.1') == bytes.fromhex('0609608648016503040201') + bytes(5)
    for bad, message in (('00' * 17, 'has 16 bytes'), ('xyz', 'hex or a dotted OID'), ('3.1', 'Not an object')):
        with pytest.raises(UdsAuthenticationError, match=message):
            uds_auth.algorithm_indicator(bad)


def test_proof_macs():
    key = bytes(range(16))
    assert uds_auth.proof_mac(key, b'abc', 'hmac-sha256') == hmac.new(key, b'abc', 'sha256').digest()
    # AES-CMAC: RFC 4493 example 2 (key 2b7e1516…, message 6bc1bee2…)
    rfc_key = bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c')
    assert uds_auth.proof_mac(rfc_key, bytes.fromhex('6bc1bee22e409f96e93d7e117393172a'), 'aes-cmac') == bytes.fromhex(
        '070a16b46b4d4144f79bdd9dd04a287c'
    )
    with pytest.raises(UdsAuthenticationError, match='16, 24 or 32-byte key'):
        uds_auth.symmetric_prover(b'short', 'aes-cmac')
    with pytest.raises(UdsAuthenticationError, match='Unknown MAC'):
        uds_auth.proof_mac(key, b'', 'md5')


def acr_ecu(config, **kwargs):
    return UdsServer(authentication=AuthenticationServer([], challenge_response=config, **kwargs))


async def test_symmetric_challenge_response(pki):
    key = bytes(range(16))
    server = acr_ecu(uds_auth.symmetric_challenge_response(key, 'aes-cmac'))
    server.authentication_dids = (0xF198,)
    server.dids[0xF198] = b'SHOP'
    client = UdsClient(Loopback(server))
    assert await uds_auth.authentication_configuration(client) == (
        'authentication configuration ACR with symmetric cryptography'
    )
    result = await client.authenticate_challenge_response(uds_auth.symmetric_prover(key, 'aes-cmac'))
    assert result.authenticated and server.authentication.authenticated
    assert result.description == (
        'requestChallengeForAuthentication: request accepted; '
        'verifyProofOfOwnershipUnidirectional: ownership verified, authentication complete'
    )
    assert await client.read_data_by_identifier(0xF198) == b'SHOP'
    # Bidirectional: the ECU proves the shared key too
    result = await client.authenticate_challenge_response(
        uds_auth.symmetric_prover(key, 'aes-cmac'), True, uds_auth.symmetric_verifier(key, 'aes-cmac')
    )
    assert result.authenticated and result.steps[-1] == "the ECU's proof verified"
    result = await client.authenticate_challenge_response(uds_auth.symmetric_prover(key, 'aes-cmac'), True)
    assert result.steps[-1] == "the ECU's proof was not checked"
    # A wrong key: ownershipVerificationFailed
    with pytest.raises(UdsNegativeResponse) as refused:
        await client.authenticate_challenge_response(uds_auth.symmetric_prover(bytes(16), 'aes-cmac'))
    assert refused.value.code == 0x58 and not server.authentication.authenticated
    # An ECU proving with another key is caught
    impostor = acr_ecu(uds_auth.symmetric_challenge_response(bytes(16), 'aes-cmac'))
    impostor.authentication.challenge_response.verify = uds_auth.symmetric_verifier(key, 'aes-cmac')
    with pytest.raises(UdsAuthenticationError, match="ECU's proof of ownership does not verify"):
        await UdsClient(Loopback(impostor)).authenticate_challenge_response(
            uds_auth.symmetric_prover(key, 'aes-cmac'), True, uds_auth.symmetric_verifier(key, 'aes-cmac')
        )


async def test_asymmetric_challenge_response(pki):
    config = uds_auth.asymmetric_challenge_response(
        [pki['tester'].public_key()], 'ecdsa-sha256', pki['ecu_key'], indicator=uds_auth.algorithm_indicator('01')
    )
    client = UdsClient(Loopback(acr_ecu(config)))
    assert await uds_auth.authentication_configuration(client) == (
        'authentication configuration ACR with asymmetric cryptography'
    )
    result = await client.authenticate_challenge_response(
        uds_auth.asymmetric_prover(pki['tester_key'], 'ecdsa-sha256'),
        True,
        uds_auth.asymmetric_verifier([pki['ecu'].public_key()], 'ecdsa-sha256'),
    )
    assert result.authenticated
    assert result.steps[1] == 'the ECU uses algorithm 01'  # It answered with another algorithm than asked for
    stranger = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(UdsNegativeResponse) as refused:
        await client.authenticate_challenge_response(uds_auth.asymmetric_prover(stranger, 'ecdsa-sha256'))
    assert refused.value.code == 0x58
    with pytest.raises(UdsAuthenticationError, match='None of the public keys fits'):
        uds_auth.asymmetric_verifier([pki['ecu'].public_key()], 'ed25519')


async def test_challenge_response_additional_parameter():
    config = uds_auth.symmetric_challenge_response(b'k', needed_additional_parameter=b'\x01')
    client = UdsClient(Loopback(acr_ecu(config)))
    with pytest.raises(UdsAuthenticationError, match=r'needs an additional parameter \(01\)'):
        await client.authenticate_challenge_response(uds_auth.symmetric_prover(b'k'))
    result = await client.authenticate_challenge_response(uds_auth.symmetric_prover(b'k'), additional_parameter=b'VIN')
    assert result.authenticated


def test_challenge_response_ecu_message_checks():
    key = b'k'
    server = acr_ecu(uds_auth.symmetric_challenge_response(key)).authentication
    proof = uds_auth.request(0x06, algorithm_indicator=bytes(16), proof_of_ownership=b'\x01')
    assert server.handle(proof) == bytes([0x7F, 0x29, 0x24])  # No challenge requested
    assert server.handle(bytes([0x29, 0x05, 0x00])) == bytes([0x7F, 0x29, 0x13])
    assert server.handle(uds_auth.request(0x05, 1, algorithm_indicator=bytes(16))) == bytes([0x7F, 0x29, 0x31])
    answer = uds_auth.parse_response(server.handle(uds_auth.request(0x05, 0, algorithm_indicator=bytes(16))))
    assert len(answer.challenge) == 32 and answer.return_parameter == 0x00
    good = uds_auth.request(
        0x06, algorithm_indicator=bytes(16), proof_of_ownership=uds_auth.proof_mac(key, answer.challenge, 'hmac-sha256')
    )
    assert server.handle(good)[:3] == bytes([0x69, 0x06, 0x12])
    assert server.handle(good) == bytes([0x7F, 0x29, 0x24])  # One proof per challenge
    # Bidirectional needs the client challenge, and an ECU able to prove
    server.handle(uds_auth.request(0x05, 0, algorithm_indicator=bytes(16)))
    assert server.handle(uds_auth.request(0x07, algorithm_indicator=bytes(16))) == bytes([0x7F, 0x29, 0x31])
    server.challenge_response.prove = None
    server.handle(uds_auth.request(0x05, 0, algorithm_indicator=bytes(16)))
    assert server.handle(uds_auth.request(0x07, algorithm_indicator=bytes(16), challenge=b'\x01')) == bytes(
        [0x7F, 0x29, 0x12]
    )
    assert describe_request(good) == 'Authentication verifyProofOfOwnershipUnidirectional, proof 32 bytes'


async def test_challenge_response_credentials_from_the_key_store(pki, tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    store.add_auth_key('Shared', bytes(range(16)), 'aes-cmac')
    store.add_tls_client(
        'Tester',
        pki['tester'].public_bytes(serialization.Encoding.PEM),
        pki['tester_key'].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )
    store.add_tls_ca('ECUs', pki['ecu'].public_bytes(serialization.Encoding.PEM))
    prove, verify_server, how = uds_auth.challenge_response_credentials(store, 'Shared')
    assert how == 'shared key (aes-cmac)' and verify_server is not None
    server = acr_ecu(uds_auth.symmetric_challenge_response(bytes(range(16)), 'aes-cmac'))
    assert (await UdsClient(Loopback(server)).authenticate_challenge_response(prove, True, verify_server)).authenticated
    prove, verify_server, how = uds_auth.challenge_response_credentials(store, 'Tester', trusted='ECUs')
    assert how == 'private key (ecdsa-sha256)'
    server = acr_ecu(
        uds_auth.asymmetric_challenge_response([pki['tester'].public_key()], 'ecdsa-sha256', pki['ecu_key'])
    )
    assert (await UdsClient(Loopback(server)).authenticate_challenge_response(prove, True, verify_server)).authenticated
    _prove, verify_server, _how = uds_auth.challenge_response_credentials(store, 'Tester')
    assert verify_server is None  # No trusted certificates: the ECU's proof cannot be checked
    with pytest.raises(UdsAuthenticationError, match="No authentication key or client certificate named 'ECUs'"):
        uds_auth.challenge_response_credentials(store, 'ECUs')
    store.lock()
    with pytest.raises(UdsAuthenticationError, match='locked'):
        uds_auth.challenge_response_credentials(store, 'Shared')
