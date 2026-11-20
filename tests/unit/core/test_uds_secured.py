import hashlib
import hmac

import pytest

from can_explorer.core import security_profiles, uds_auth, uds_secured
from can_explorer.core.keystore import KeyStore, KeyStoreError
from can_explorer.core.simulation import UdsServer
from can_explorer.core.uds import UdsClient, UdsError, UdsNegativeResponse, describe_payload, describe_response
from can_explorer.core.uds_secured import (
    MacProvider,
    SecuredDataError,
    SecuredOptions,
    SecuredServerConfig,
    SecuredTransport,
    SessionContext,
    SessionKeyIssuer,
    open_message,
    parse,
    reference_session_key,
    reference_session_provider,
    secured_request,
    session_provider,
    wrap,
)
from tests.unit.core.test_uds_auth import Loopback, pki  # noqa: F401, F811  (pki: that module's fixture)
from tests.unit.core.test_uds_services import ServerTransport

KEY = bytes(range(16))


class XorProvider:
    """A test provider with 'encryption' (XOR with the counter's low byte) and an HMAC signature."""

    def signature_length(self, header):
        return 8

    def sign(self, header, data):
        return hmac.new(KEY, data, hashlib.sha256).digest()[:8]

    def encrypt(self, header, plaintext):
        return bytes(byte ^ (header.counter & 0xFF) ^ 0x5A for byte in plaintext)

    decrypt = encrypt


# The envelope (ISO 14229-1:2020; flag positions as Wireshark's packet-uds.c and the py-uds tables)


def test_plain_request_layout_and_description():
    envelope = wrap(bytes([0x22, 0xF1, 0x90]), counter=5)
    # SID, admin 0x0001 (request message), algorithm, signature length 0, counter 5, internal message
    assert envelope == bytes.fromhex('84 0001 00 0000 0005 22F190')
    assert (
        describe_payload(envelope)
        == 'SecuredDataTransmission: plain, counter 5, algorithm 0x00: ReadDataByIdentifier F1 90'
    )
    message = parse(envelope)
    assert message.header.request and not message.header.signed and message.internal == bytes([0x22, 0xF1, 0x90])


def test_flag_bits():
    admin = uds_secured.administrative_parameter(True, signed=True, encrypted=True, signature_on_response=True)
    assert admin == 0x0071
    assert uds_secured.administrative_parameter(False, pre_established_key=True) == 0x0008
    header = uds_secured.SecuredHeader(admin | 0x0008, 0x01, 16, 1)
    assert header.flags() == ['signed', 'encrypted', 'pre-established key', 'signed response requested']
    assert uds_secured.algorithm_range(0x10) == 'vehicle manufacturer specific'
    assert uds_secured.algorithm_range(0x85) == 'system supplier specific'
    assert uds_secured.algorithm_range(0x90) == 'reserved'


def test_signed_message_with_reference_scheme():
    provider = MacProvider(KEY, 'hmac-sha256', length=16)
    envelope = wrap(bytes([0x22, 0xF1, 0x90]), 7, provider=provider, sign=True, algorithm=0x01)
    data = envelope[:-16]
    assert envelope[1:3] == b'\x00\x21' and envelope[4:6] == (16).to_bytes(2, 'big')
    assert envelope[-16:] == hmac.new(KEY, data, hashlib.sha256).digest()[:16]  # MAC over everything before it
    opened = open_message(envelope, provider)
    assert opened.verified and opened.internal == bytes([0x22, 0xF1, 0x90])
    assert 'signed (16 bytes), counter 7, algorithm 0x01: ReadDataByIdentifier' in describe_payload(envelope)

    tampered = envelope[:-1] + bytes([envelope[-1] ^ 1])
    with pytest.raises(SecuredDataError, match='verification failed'):
        open_message(tampered, provider)
    with pytest.raises(SecuredDataError, match='needs a provider'):
        open_message(envelope)


def test_aes_cmac_reference_scheme():
    from can_explorer.core.secoc import cmac_aes

    provider = MacProvider(KEY, 'aes-cmac')
    envelope = wrap(b'\x3e\x00', 1, provider=provider, sign=True)
    assert envelope[-16:] == cmac_aes(KEY, envelope[:-16])
    with pytest.raises(SecuredDataError, match='16, 24 or 32'):
        MacProvider(b'short', 'aes-cmac')
    with pytest.raises(SecuredDataError, match='1 to 32'):
        MacProvider(KEY, 'hmac-sha256', length=33)
    assert KEY.hex() not in repr(provider)


def test_encrypted_message_is_not_described_but_opened_with_the_provider():
    provider = XorProvider()
    envelope = wrap(bytes([0x22, 0xF1, 0x90]), 3, provider=provider, sign=True, encrypt=True)
    assert 'ReadDataByIdentifier' not in describe_payload(envelope)
    assert describe_payload(envelope).endswith('3 encrypted bytes')
    opened = open_message(envelope, provider)
    assert opened.internal == bytes([0x22, 0xF1, 0x90])
    assert opened.describe() == 'ReadDataByIdentifier F1 90 (secured: counter 3, signature verified, decrypted)'


def test_response_and_iso_2013_records_are_labelled():
    response = wrap(bytes([0x62, 0xF1, 0x90]) + b'VIN', 9, request=False)
    assert describe_response(response).startswith('Positive response to SecuredDataTransmission: plain, counter 9')
    assert 'Positive response to ReadDataByIdentifier' in describe_response(response)
    # Not the 2020 layout: kept raw and labelled
    for payload in (bytes([0x84, 0xAA, 0xBB]), bytes([0x84, 0xFF, 0xFF, 0, 0, 0, 0, 0, 0x22])):
        assert 'not decoded: ISO 14229-1:2013 or malformed' in describe_payload(payload)
    # A response with the request bit, or a signature longer than the message, does not fit either
    with pytest.raises(SecuredDataError, match='request message bit'):
        parse(bytes.fromhex('C4 0001 00 0000 0001 62'))
    with pytest.raises(SecuredDataError, match='exceeds'):
        parse(bytes.fromhex('84 0021 00 0010 0001 22'))
    with pytest.raises(SecuredDataError, match='signed bit'):
        parse(bytes.fromhex('84 0001 00 0001 0001 22 FF'))


def test_wrap_needs_a_provider_for_protection():
    with pytest.raises(SecuredDataError, match='needs a provider'):
        wrap(b'\x22\xf1\x90', 0, sign=True)
    with pytest.raises(SecuredDataError, match='needs a provider'):
        wrap(b'\x22\xf1\x90', 0, provider=MacProvider(KEY), encrypt=True)
    with pytest.raises(SecuredDataError, match='empty'):
        wrap(b'', 0)
    with pytest.raises(SecuredDataError, match='need a key'):
        SecuredTransport(None, None, SecuredOptions(sign=True))


# Client and simulated ECU


def secured_server(**kwargs):
    config = SecuredServerConfig(provider=kwargs.pop('provider', MacProvider(KEY)), **kwargs)
    return UdsServer(secured=config), config


async def test_every_service_runs_through_the_secured_transport():
    server, config = secured_server(secured_dids=(0xF190,))
    plain = UdsClient(ServerTransport(server))
    with pytest.raises(UdsNegativeResponse, match='secureDataTransmissionRequired'):
        await plain.read_data_by_identifier(0xF190)

    transport = SecuredTransport(
        ServerTransport(server), MacProvider(KEY), SecuredOptions(signature_on_response=True), counter=100
    )
    client = UdsClient(transport)
    assert await client.read_data_by_identifier(0xF190) == b'WDB1234567890ABCD'
    assert transport.last.opened.verified and transport.last.opened.message.header.counter == 100
    assert transport.last.describe() == 'secured: counter 100, response signature verified'
    await client.diagnostic_session_control(0x03)
    await client.tester_present(suppress_response=True)  # Suppressed inside the envelope: no response
    assert transport.next_counter == 103 and config.last_counter == 102
    assert server.session == 0x03
    # An inner negative response travels in the envelope too
    with pytest.raises(UdsNegativeResponse, match='requestOutOfRange'):
        await client.read_data_by_identifier(0x1234)


async def test_encrypted_requests_and_responses():
    server, _ = secured_server(provider=XorProvider())
    transport = SecuredTransport(ServerTransport(server), XorProvider(), SecuredOptions(encrypt=True))
    assert await UdsClient(transport).read_data_by_identifier(0xF18C) == b'SN-0001'
    response = transport.last.response
    assert response[1:3] == b'\x00\x10' and b'SN-0001' not in response  # Encrypted, unsigned (not requested)
    assert server.requests[-1] == bytes([0x22, 0xF1, 0x8C])


async def test_ecu_rejects_replays_bad_signatures_and_unsigned_requests():
    server, config = secured_server()
    transport = ServerTransport(server)
    good = wrap(b'\x22\xf1\x90', 5, provider=MacProvider(KEY), sign=True)
    assert server.handle(good)[0][0] == 0xC4
    assert server.handle(good) == [bytes([0x7F, 0x84, 0x3A])]  # The same counter again: a replay
    other_key = wrap(b'\x22\xf1\x90', 6, provider=MacProvider(bytes(16)), sign=True)
    assert server.handle(other_key) == [bytes([0x7F, 0x84, 0x3A])]
    assert server.handle(wrap(b'\x22\xf1\x90', 7)) == [bytes([0x7F, 0x84, 0x3A])]  # Unsigned
    assert server.handle(bytes([0x84, 0x01])) == [bytes([0x7F, 0x84, 0x13])]
    nested = wrap(wrap(b'\x3e\x00', 9), 8, provider=MacProvider(KEY), sign=True)
    assert open_message(server.handle(nested)[0], None).internal == bytes([0x7F, 0x84, 0x39])
    assert describe_response(bytes([0x7F, 0x84, 0x3A])).endswith('secureDataVerificationFailed')
    assert UdsServer().handle(good) == [bytes([0x7F, 0x84, 0x11])]  # Not configured

    # Through the transport, an envelope NRC is reported for the wrapped request
    client = UdsClient(SecuredTransport(transport, MacProvider(KEY), counter=5))  # Counter 5 is used up
    with pytest.raises(UdsNegativeResponse, match='ReadDataByIdentifier: secureDataVerificationFailed'):
        await client.read_data_by_identifier(0xF190)
    assert config.last_counter == 8  # The nested envelope's outer counter was accepted
    assert config.fresh(9) and not config.fresh(8) and not config.fresh(8 + 0x8000)


class FixedResponses:
    """A transport answering every request with the given responses."""

    def __init__(self, *responses):
        self.responses = list(responses)

    async def send(self, payload):
        pass

    async def receive(self, timeout=None):
        if not self.responses:
            raise TimeoutError('No response')
        return self.responses.pop(0)


async def test_client_refuses_unsigned_or_forged_responses():
    answer = bytes([0x62, 0xF1, 0x90]) + b'VIN'
    forged = wrap(answer, 0, request=False, provider=MacProvider(bytes(16)), sign=True)  # Another key
    client = UdsClient(SecuredTransport(FixedResponses(forged), MacProvider(KEY)))
    with pytest.raises(UdsError, match='Signature verification failed'):
        await client.read_data_by_identifier(0xF190)

    unsigned = wrap(answer, 0, request=False)
    options = SecuredOptions(signature_on_response=True)
    client = UdsClient(SecuredTransport(FixedResponses(unsigned), MacProvider(KEY), options))
    with pytest.raises(UdsError, match='unsigned response'):
        await client.read_data_by_identifier(0xF190)
    # Without asking for a signed response, an unsigned one is accepted
    client = UdsClient(SecuredTransport(FixedResponses(unsigned), MacProvider(KEY)))
    assert await client.read_data_by_identifier(0xF190) == b'VIN'
    # A plain (unwrapped) answer is passed through as it is
    client = UdsClient(SecuredTransport(FixedResponses(answer), MacProvider(KEY)))
    assert await client.read_data_by_identifier(0xF190) == b'VIN'


async def test_secured_request_result():
    server, _ = secured_server()
    result = await secured_request(
        ServerTransport(server),
        bytes([0x22, 0xF1, 0x90]),
        MacProvider(KEY),
        SecuredOptions(signature_on_response=True),
        3,
    )
    data = result.as_dict()
    assert data['request'] == '22 F1 90' and data['response'].startswith('62 F1 90')
    assert data['secured_request'].startswith('84 00 61 00 00 20 00 03 22 F1 90')
    assert data['counter'] == 3 and data['response_counter'] == 3 and data['response_signature_verified']
    assert data['error'] is None


# Providers from files and the key store


def test_provider_file(tmp_path):
    path = tmp_path / 'oem.py'
    path.write_text(
        'def signature_length(header):\n    return 4\n'
        'def sign(header, data):\n    return bytes([sum(data) & 0xFF, header.counter & 0xFF, 0, 1])\n'
    )
    provider = uds_secured.load_provider(str(path))
    envelope = wrap(b'\x3e\x00', 2, provider=provider, sign=True)
    assert envelope[-4:] == bytes([sum(envelope[:-4]) & 0xFF, 2, 0, 1])
    assert open_message(envelope, provider).verified  # No verify(): the signature is made again and compared
    assert uds_secured.load_provider('') is None

    (tmp_path / 'bad.py').write_text('def sign(header, data):\n    return b""\n')
    with pytest.raises(SecuredDataError, match='signature_length'):
        uds_secured.load_provider(str(tmp_path / 'bad.py'))
    (tmp_path / 'empty.py').write_text('X = 1\n')
    with pytest.raises(SecuredDataError, match='neither'):
        uds_secured.load_provider(str(tmp_path / 'empty.py'))


def test_key_store_entry(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=2**4)
    store.create('pw')
    store.add_secured_data_key('ecu', KEY, 'aes-cmac')
    provider = uds_secured.load_provider('keystore:ecu', store, length=8)
    assert isinstance(provider, MacProvider) and provider.mac == 'aes-cmac' and provider.length == 8
    with pytest.raises(SecuredDataError, match='not a secured data key'):
        uds_secured.load_provider('keystore:missing', store)
    with pytest.raises(KeyStoreError, match='16, 24 or 32'):
        store.add_secured_data_key('bad', b'123', 'aes-cmac')


# Session keys from Authentication (0x29)

NONCE = bytes(range(0xA0, 0xB0))


def test_reference_session_key_derivation():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    context = SessionContext('challenge-response', 0x01, NONCE, b'S' * 32, b'C' * 32)
    expected = HKDF(
        hashes.SHA256(), 16, salt=b'S' * 32 + b'C' * 32, info=b'CANExplorer UDS session key\x01' + NONCE
    ).derive(KEY)
    assert reference_session_key(KEY, context, 'aes-cmac') == expected
    assert len(reference_session_key(KEY, context)) == 32
    assert reference_session_key(KEY, context) != reference_session_key(
        KEY, SessionContext('x', 0x02, NONCE, b'S' * 32)
    )
    with pytest.raises(SecuredDataError, match='no session key information'):
        reference_session_key(KEY, SessionContext('challenge-response', 0x00, b''))
    assert 'session key info 16 bytes (communicationConfiguration 0x01' in context.describe()


def session_ecu(**secured):
    issuer = SessionKeyIssuer(KEY, 'aes-cmac', random=lambda n: NONCE[:n])
    authentication = uds_auth.AuthenticationServer(
        [], challenge_response=uds_auth.symmetric_challenge_response(KEY, 'aes-cmac'), session_keys=issuer
    )
    config = SecuredServerConfig(require_session_key=True, secured_dids=(0xF190,), **secured)
    return UdsServer(authentication=authentication, secured=config)


async def test_challenge_response_session_key_protects_secured_requests():
    server = session_ecu()
    client = UdsClient(Loopback(server))
    prove = uds_auth.symmetric_prover(KEY, 'aes-cmac')
    result = await client.authenticate_challenge_response(prove, True, communication_configuration=0x01)
    assert result.authenticated and result.session_key_info == NONCE
    assert result.description.endswith('session key info 16 bytes')
    context = SessionContext.from_result(result)
    assert context.method == 'challenge-response' and len(context.client_challenge) == 32
    provider = session_provider(context, lambda ctx: reference_session_provider(KEY, ctx, 'aes-cmac'))
    assert provider.key == server.authentication.session_provider.key  # Both sides derived the same session key

    options = SecuredOptions(pre_established_key=True, signature_on_response=True)
    secured = SecuredTransport(Loopback(server), provider, options)
    assert await UdsClient(secured).read_data_by_identifier(0xF190) == b'WDB1234567890ABCD'
    assert secured.last.opened.message.header.pre_established_key

    # The ECU wants the session key: the long-term key alone, or a stale session key, is refused
    plain_key = SecuredTransport(Loopback(server), MacProvider(KEY, 'aes-cmac'), counter=10)
    with pytest.raises(UdsNegativeResponse, match='secureDataVerificationFailed'):
        await UdsClient(plain_key).read_data_by_identifier(0xF190)
    await client.deauthenticate()
    assert server.authentication.session_provider is None
    with pytest.raises(UdsNegativeResponse, match='secureDataVerificationFailed'):
        await UdsClient(SecuredTransport(Loopback(server), provider, options, counter=20)).read_data_by_identifier(
            0xF190
        )


async def test_communication_configuration_values():
    server = session_ecu()
    client = UdsClient(Loopback(server))
    prove = uds_auth.symmetric_prover(KEY, 'aes-cmac')
    result = await client.authenticate_challenge_response(prove)  # communicationConfiguration 0: no session key
    assert result.authenticated and not result.session_key_info and server.authentication.session_provider is None
    with pytest.raises(SecuredDataError, match='no session key information'):
        reference_session_provider(KEY, SessionContext.from_result(result))
    with pytest.raises(UdsNegativeResponse, match='requestOutOfRange'):  # Not one the ECU accepts
        await client.authenticate_challenge_response(prove, communication_configuration=0x02)


async def test_certificate_exchange_session_key_by_key_agreement(pki):  # noqa: F811
    """A manufacturer-style scheme through the hooks: ECDH of ephemeral keys (the ECU's public key as sessionKeyInfo),
    HKDF of the shared secret."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    def point(key):
        return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def derive(private_key, peer: bytes) -> bytes:
        shared = private_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer))
        return HKDF(hashes.SHA256(), 16, salt=None, info=b'test').derive(shared)

    def issue(context):
        ecu_ephemeral = ec.generate_private_key(ec.SECP256R1())
        key = derive(ecu_ephemeral, context.client_ephemeral_public_key)
        return point(ecu_ephemeral), MacProvider(key, 'aes-cmac')

    authentication = uds_auth.AuthenticationServer(
        [pki['ca']], 'ecdsa-sha256', pki['ecu'], pki['ecu_key'], session_keys=SessionKeyIssuer(issue=issue)
    )
    server = UdsServer(authentication=authentication, secured=SecuredServerConfig(require_session_key=True))
    tester_ephemeral = ec.generate_private_key(ec.SECP256R1())
    result = await UdsClient(Loopback(server)).authenticate(
        pki['tester'],
        pki['tester_key'],
        'ecdsa-sha256',
        communication_configuration=0x01,
        ephemeral_public_key=lambda _ecu_key: point(tester_ephemeral),
    )
    assert result.authenticated and result.method == 'certificate' and len(result.session_key_info) == 65
    context = SessionContext.from_result(result)
    assert context.client_ephemeral_public_key == point(tester_ephemeral)
    provider = session_provider(
        context, lambda ctx: MacProvider(derive(tester_ephemeral, ctx.session_key_info), 'aes-cmac')
    )
    secured = SecuredTransport(Loopback(server), provider, SecuredOptions(pre_established_key=True))
    assert await UdsClient(secured).read_data_by_identifier(0xF18C) == b'SN-0001'


def test_session_provider_sources(tmp_path):
    context = SessionContext('challenge-response', 0x01, NONCE, b'S' * 32)
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=2**4)
    store.create('pw')
    store.add_auth_key('Shared', KEY, 'aes-cmac')
    provider = session_provider(context, 'Shared', store, length=8)
    assert provider.key == reference_session_key(KEY, context, 'aes-cmac') and provider.length == 8
    with pytest.raises(SecuredDataError, match='neither'):
        session_provider(context, 'missing', store)

    class Profile:
        name = 'OEM'

        def session_provider(self, ctx):
            return MacProvider(ctx.session_key_info, 'aes-cmac')  # The "unwrapped" key

    registry = security_profiles.registry()
    registry.add(security_profiles.LoadedProfile(Profile(), 'test'))
    try:
        assert 'session_provider' in security_profiles.capabilities(Profile())
        assert session_provider(context, 'profile:OEM').key == NONCE
    finally:
        registry.remove('OEM')
    with pytest.raises(SecuredDataError, match='no session key'):
        session_provider(context, lambda ctx: None)
    with pytest.raises(SecuredDataError, match='returned no provider'):
        session_provider(context, lambda ctx: b'key')
