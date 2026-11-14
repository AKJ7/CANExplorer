import datetime
import ipaddress
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from can_explorer.core.doip import DoipClient, DoipEntitySimulator, DoipError
from can_explorer.core.keystore import KeyStore, KeyStoreError
from can_explorer.core.tls import client_context


def _name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def issue(common_name, issuer=None, ca=False, ip=None):
    """A certificate and key, signed by `issuer` (certificate, key) or self-signed."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(issuer[0].subject if issuer else _name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if ip:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), False)
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True
        )
    return builder.sign(issuer[1] if issuer else key, hashes.SHA256()), key


def pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM)


def key_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture
def pki(tmp_path):
    """A CA, an entity certificate for 127.0.0.1 and a tester certificate, plus a key store holding the tester's
    certificate and the CA."""
    ca = issue('Test CA', ca=True)
    entity = issue('DoIP entity', ca, ip='127.0.0.1')
    tester = issue('Tester', ca)
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    store.add_tls_ca('CA', pem(ca[0]))
    store.add_tls_client('Tester', pem(tester[0]), key_pem(tester[1]))
    (tmp_path / 'entity.pem').write_bytes(pem(entity[0]))
    (tmp_path / 'entity.key').write_bytes(key_pem(entity[1]))
    return {'store': store, 'ca': ca, 'dir': tmp_path}


def server_context(pki, require_client=True) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pki['dir'] / 'entity.pem', pki['dir'] / 'entity.key')
    if require_client:
        context.load_verify_locations(cadata=pem(pki['ca'][0]).decode())
        context.verify_mode = ssl.CERT_REQUIRED
    return context


async def connect(context, server_tls):
    simulator = DoipEntitySimulator(tls=server_tls)
    await simulator.start()
    client = DoipClient(simulator.host, simulator.port, tls=context, timeout=2.0)
    try:
        return await client.connect()
    finally:
        await client.close()
        await simulator.stop()


async def test_mutual_tls_doip_session(pki):
    context = client_context(pki['store'], trusted='CA', client='Tester', check_hostname=True)
    activation = await connect(context, server_context(pki))
    assert activation.entity_address == 0x1000


async def test_entity_with_an_untrusted_certificate_is_refused(pki, tmp_path):
    other = KeyStore(tmp_path / 'other.json', scrypt_cost=16)
    other.create('pw')
    other.add_tls_ca('Someone else', pem(issue('Other CA', ca=True)[0]))
    with pytest.raises(DoipError):
        await connect(client_context(other, trusted='Someone else'), server_context(pki, require_client=False))


async def test_entity_demanding_a_client_certificate_refuses_a_tester_without_one(pki):
    with pytest.raises(DoipError):
        await connect(client_context(pki['store'], trusted='CA'), server_context(pki))


async def test_without_trusted_certificates_the_entity_is_not_verified(pki):
    context = client_context()
    assert context.verify_mode == ssl.CERT_NONE and not context.check_hostname
    activation = await connect(context, server_context(pki, require_client=False))
    assert activation.entity_address == 0x1000


async def test_key_log(pki, tmp_path):
    keylog = tmp_path / 'keys.log'
    context = client_context(pki['store'], trusted='CA', client='Tester', keylog=keylog)
    await connect(context, server_context(pki))
    assert 'CLIENT_' in Path(keylog).read_text()  # NSS key log lines, e.g. CLIENT_TRAFFIC_SECRET_0


def test_entry_errors(pki):
    store = pki['store']
    with pytest.raises(KeyStoreError, match='No key store entry'):
        client_context(store, trusted='Missing')
    with pytest.raises(KeyStoreError, match='is a tls-ca, not a tls-client'):
        client_context(store, client='CA')
    with pytest.raises(KeyStoreError, match='No key store'):
        client_context(None, trusted='CA')
    store.lock()
    with pytest.raises(KeyStoreError, match='locked'):
        client_context(store, client='Tester')
    assert client_context(store, trusted='CA').verify_mode == ssl.CERT_REQUIRED  # Certificates are public
