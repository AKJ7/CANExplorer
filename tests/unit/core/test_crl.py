"""Certificate revocation lists: key store entries, UDS Authentication (0x29) and DoIP TLS."""

import datetime
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from can_explorer.core import uds_auth
from can_explorer.core.doip import DoipClient, DoipEntitySimulator, DoipError
from can_explorer.core.keystore import KeyStore, KeyStoreError
from can_explorer.core.simulation import UdsServer
from can_explorer.core.test_pki import create_test_pki
from can_explorer.core.tls import client_context
from can_explorer.core.uds import UdsClient, UdsNegativeResponse


@pytest.fixture
def store(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    return store


def make_crl(ca, ca_key, serials, signer_key=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = x509.CertificateRevocationListBuilder().issuer_name(ca.subject).last_update(now)
    builder = builder.next_update(now + datetime.timedelta(days=7))
    for serial in serials:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(serial).revocation_date(now).build()
        )
    return builder.sign(signer_key or ca_key, hashes.SHA256())


@pytest.fixture
def pki(store, tmp_path):
    from can_explorer.core.test_pki import generate_key, issue

    pki = create_test_pki(store, 'Lab', directory=tmp_path / 'pem')
    # create_test_pki does not keep the CA key: make a CA we can sign revocation lists with
    ca_key = generate_key('ec-p256')
    ca = issue(x509.Name.from_rfc4514_string('CN=Lab CRL CA'), ca_key, None, ca_key, 30, ca=True)
    tester_key = generate_key('ec-p256')
    tester = issue(x509.Name.from_rfc4514_string('CN=Revoked tester'), tester_key, ca, ca_key, 30)
    return {'pki': pki, 'ca': ca, 'ca_key': ca_key, 'tester': tester, 'tester_key': tester_key}


def test_crl_entries(store, pki):
    crl = make_crl(pki['ca'], pki['ca_key'], [pki['tester'].serial_number, 42])
    info = store.add_crl('Lab CRL', crl.public_bytes(serialization.Encoding.DER))
    assert info.purpose == 'crl' and info.params['issuer'] == 'CN=Lab CRL CA' and info.params['revoked'] == '2'
    assert info.secret_names == ()
    store.lock()
    [stored] = store.revocation_lists()  # Readable while locked
    assert sorted(entry.serial_number for entry in stored) == sorted(entry.serial_number for entry in crl)
    store.unlock('pw')
    with pytest.raises(KeyStoreError, match='Cannot read the revocation list'):
        store.add_crl('Bad', b'not a crl')


def test_revoked_certificates_are_refused(pki):
    trusted = [pki['ca']]
    crl = make_crl(pki['ca'], pki['ca_key'], [pki['tester'].serial_number])
    assert uds_auth.certificate_problem(pki['tester'], trusted) is None
    assert uds_auth.certificate_problem(pki['tester'], trusted, crls=[crl]) == uds_auth.INVALID_CERTIFICATE
    other = make_crl(pki['ca'], pki['ca_key'], [7])
    assert uds_auth.certificate_problem(pki['tester'], trusted, crls=[other]) is None
    forged = make_crl(pki['ca'], pki['ca_key'], [pki['tester'].serial_number], signer_key=pki['tester_key'])
    assert uds_auth.certificate_problem(pki['tester'], trusted, crls=[forged]) is None  # Not signed by the CA


class Loopback:
    def __init__(self, server):
        self.server, self.responses = server, []

    async def send(self, payload):
        self.responses += self.server.handle(bytes(payload))

    async def receive(self, timeout=None):
        return self.responses.pop(0)


async def test_authentication_with_revocation_lists(pki):
    crl = make_crl(pki['ca'], pki['ca_key'], [pki['tester'].serial_number])
    server = UdsServer(authentication=uds_auth.AuthenticationServer([pki['ca']], 'ecdsa-sha256', crls=[crl]))
    with pytest.raises(UdsNegativeResponse) as refused:
        await UdsClient(Loopback(server)).authenticate(pki['tester'], pki['tester_key'], 'ecdsa-sha256')
    assert uds_auth.NEGATIVE_RESPONSE_CODES[refused.value.code] == 'certificateVerificationFailed-InvalidCertificate'
    # The tester checks the ECU's certificate against revocation lists too
    lab = pki['pki']
    ecu = UdsServer(authentication=uds_auth.AuthenticationServer([lab.ca], 'ecdsa-sha256', lab.ecu, lab.ecu_key))
    revoked_ecu = make_crl(pki['ca'], pki['ca_key'], [])  # A list of another CA does not apply
    result = await UdsClient(Loopback(ecu)).authenticate(
        lab.tester, lab.tester_key, 'ecdsa-sha256', True, [lab.ca], crls=[revoked_ecu]
    )
    assert result.authenticated


async def test_doip_tls_refuses_a_revoked_entity(store, tmp_path):
    from can_explorer.core.test_pki import generate_key, issue

    ca_key = generate_key('ec-p256')
    ca = issue(x509.Name.from_rfc4514_string('CN=DoIP CA'), ca_key, None, ca_key, 30, ca=True)
    entity_key = generate_key('ec-p256')
    entity = issue(x509.Name.from_rfc4514_string('CN=Entity'), entity_key, ca, ca_key, 30, addresses=['127.0.0.1'])
    (tmp_path / 'entity.pem').write_bytes(entity.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'entity.key').write_bytes(
        entity_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(tmp_path / 'entity.pem', tmp_path / 'entity.key')
    store.add_tls_ca('DoIP CA', ca.public_bytes(serialization.Encoding.PEM))

    async def connect():
        simulator = DoipEntitySimulator(tls=server_context)
        await simulator.start()
        client = DoipClient(simulator.host, simulator.port, tls=client_context(store, trusted='DoIP CA'), timeout=2)
        try:
            return await client.connect()
        finally:
            await client.close()
            await simulator.stop()

    store.add_crl('Clean', make_crl(ca, ca_key, [12345]).public_bytes(serialization.Encoding.PEM))
    assert (await connect()).entity_address == 0x1000
    store.add_crl('Revoked', make_crl(ca, ca_key, [entity.serial_number]).public_bytes(serialization.Encoding.PEM))
    store.remove('Clean')
    with pytest.raises(DoipError):
        await connect()
