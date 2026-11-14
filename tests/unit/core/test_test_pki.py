import ipaddress
import os
import stat
import sys

import pytest
from cryptography import x509

from can_explorer.core import test_pki, uds_auth
from can_explorer.core.keystore import KeyStore
from can_explorer.core.simulation import UdsServer
from can_explorer.core.test_pki import TestPkiError, create_test_pki
from can_explorer.core.uds import UdsClient


@pytest.fixture
def store(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    return store


@pytest.mark.parametrize('key_type', test_pki.KEY_TYPES)
def test_key_types(store, key_type):
    pki = create_test_pki(store, 'Lab', key_type, days=30)
    assert pki.entries == {'ca': 'Lab CA', 'tester': 'Lab tester', 'ecu': 'Lab ECU'}
    assert uds_auth.certificate_problem(pki.tester, [pki.ca]) is None
    assert uds_auth.certificate_problem(pki.ecu, [pki.ca]) is None
    scheme = uds_auth.scheme_for(pki.tester_key)
    proof = uds_auth.sign(pki.tester_key, b'challenge', scheme)
    assert uds_auth.verify(pki.tester.public_key(), proof, b'challenge', scheme)


def test_entries_and_files(store, tmp_path):
    pki = create_test_pki(
        store, 'Bench 1', days=10, ecu_addresses=['192.168.0.20', 'ecu.local'], directory=tmp_path / 'pem'
    )
    by_name = store.entries_by_name()
    assert by_name['Bench 1 CA'].purpose == 'tls-ca' and by_name['Bench 1 tester'].purpose == 'tls-client'
    certificate, key = uds_auth.credentials(store, 'Bench 1 ECU')
    assert certificate == pki.ecu
    names = pki.ecu.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address('192.168.0.20')]
    assert names.get_values_for_type(x509.DNSName) == ['ecu.local']
    assert sorted(path.name for path in pki.files.values()) == [
        'bench_1_ca.pem',
        'bench_1_ecu.key',
        'bench_1_ecu.pem',
        'bench_1_tester.key',
        'bench_1_tester.pem',
    ]
    if sys.platform != 'win32':
        assert stat.S_IMODE(os.stat(pki.files['bench_1_tester.key']).st_mode) == 0o600
    with pytest.raises(TestPkiError, match="entry named 'Bench 1 CA' already"):
        create_test_pki(store, 'Bench 1')
    assert create_test_pki(store, 'Bench 1', replace=True).ca != pki.ca
    with pytest.raises(TestPkiError, match='Unknown key type'):
        create_test_pki(store, 'X', 'dsa')
    with pytest.raises(TestPkiError, match='name prefix'):
        create_test_pki(store, ' ')


async def test_the_test_pki_authenticates_against_the_simulated_ecu(store):
    from can_explorer.core.uds_auth import AuthenticationServer

    pki = create_test_pki(store, 'Sim')
    server = UdsServer(authentication=AuthenticationServer([pki.ca], 'ecdsa-sha256', pki.ecu, pki.ecu_key))

    class Loopback:
        def __init__(self):
            self.responses = []

        async def send(self, payload):
            self.responses += server.handle(bytes(payload))

        async def receive(self, timeout=None):
            return self.responses.pop(0)

    result = await UdsClient(Loopback()).authenticate(pki.tester, pki.tester_key, 'ecdsa-sha256', True, [pki.ca])
    assert result.authenticated and result.server_certificate == pki.ecu
