import asyncio
import os
import struct
import uuid

import can
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PyQt6.QtCore import QUrl

from can_explorer.core import keystore
from can_explorer.core.keystore import KeyStore
from can_explorer.core.secoc_check import SecOcConfig, SecOcVerifier, StaticSecOcKeys
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.gui.doip import SIMULATED, DoipController
from can_explorer.gui.ethernet import EthernetController
from can_explorer.gui.security import SecurityController

KEY = bytes(range(16))
SECURED_STATUS = SecOcConfig('SecuredStatus', 18, 2, 28, 32, 4, 'CMAC_AES128')


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def security(qapp, tmp_path):
    return SecurityController(KeyStore(tmp_path / 'keys.json', scrypt_cost=16))


@pytest.fixture
def manager(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv(keystore.PATH_VARIABLE, str(tmp_path / 'manager-keys.json'))
    manager = ConnectionManager()
    manager.security.store._scrypt_cost = 16
    yield manager
    manager.close_all()


def test_create_unlock_and_lock(security):
    changes = []
    security.keys_changed.connect(lambda: changes.append(1))
    assert not security.exists and not security.unlocked and security.entries == []
    assert security.create('a', 'b') == 'The passwords differ'
    assert security.create('pw', 'pw') == ''
    assert security.exists and security.unlocked and changes
    security.lock()
    assert not security.unlocked
    assert security.unlock('wrong') == 'Wrong password for the key store'
    assert security.unlock('pw') == '' and security.unlocked
    assert security.change_password('pw', 'new', 'other') == 'The new passwords differ'
    assert security.change_password('pw', 'new', 'new') == ''
    security.lock()
    assert security.unlock('new') == ''


def test_unlocked_from_the_environment(tmp_path, qapp, monkeypatch):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    monkeypatch.setenv(keystore.PASSWORD_VARIABLE, 'pw')
    assert SecurityController(KeyStore(store.path, scrypt_cost=16)).unlocked
    monkeypatch.setenv(keystore.PASSWORD_VARIABLE, 'wrong')
    assert not SecurityController(KeyStore(store.path, scrypt_cost=16)).unlocked  # Logged, not raised


@pytest.fixture
def certificates(tmp_path):
    """A CA certificate, and a client certificate with its encrypted private key, as PEM files."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def issue(name, ca):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=10))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        return certificate.public_bytes(serialization.Encoding.PEM), key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b'key password'),
        )

    ca, _ = issue('Lab CA', True)
    client, client_key = issue('Tester', False)
    paths = {'ca': tmp_path / 'ca.pem', 'client': tmp_path / 'client.pem', 'key': tmp_path / 'client.key'}
    paths['ca'].write_bytes(ca)
    paths['client'].write_bytes(client)
    paths['key'].write_bytes(client_key)
    return {name: QUrl.fromLocalFile(str(path)) for name, path in paths.items()}


def test_entries(security, certificates):
    assert security.add_secoc_key('K', KEY.hex(), '*', '').endswith('yet; create one first')
    assert security.create('pw', 'pw') == ''
    security.lock()
    assert security.add_secoc_key('K', KEY.hex(), '*', '') == 'The key store is locked; unlock it with its password'
    assert security.unlock('pw') == ''
    assert (
        security.add_secoc_key('Powertrain', '00 01 02', 'EngineData', '')
        == 'A CMAC/AES key has 16, 24 or 32 bytes, not 3'
    )
    assert security.add_secoc_key('Powertrain', KEY.hex(), 'EngineData, 0x12', '') == ''
    assert security.add_tls_ca('Lab', certificates['ca']) == ''
    assert 'Cannot read the private key' in security.add_tls_client(
        'Tester', certificates['client'], certificates['key'], ''
    )
    assert security.add_tls_client('Tester', certificates['client'], certificates['key'], 'key password') == ''
    assert security.add_ipsec_sa('ESP', 'zz', 'aes-gcm', '', 'none', '') == 'The SPI is hexadecimal, e.g. 0x00001000'
    assert security.add_ipsec_sa('ESP', '0x1000', 'aes-gcm', (KEY + b'salt').hex(), 'none', '') == ''
    assert 'The SCI is 16 hex digits' in security.add_macsec_sa('Zone', KEY.hex(), 'nope')
    assert security.add_macsec_sa('Zone', KEY.hex(), '00:11:22:33:44:55:00:01') == ''
    rows = {row['name']: row for row in security.entries}
    assert [row['purpose'] for row in security.entries] == ['secoc', 'tls-client', 'tls-ca', 'ipsec', 'macsec']
    assert rows['Powertrain']['details'] == 'for EngineData, 0x12 · algorithm from the ARXML'
    assert rows['Tester']['kind'] == 'TLS client certificate' and rows['Tester']['details'].startswith(
        'CN=Tester · expires '
    )
    assert rows['ESP']['details'] == 'SPI 0x00001000 · aes-gcm'
    assert rows['Zone']['details'] == 'SCI 0011223344550001 · gcm-aes-128'
    assert not any(KEY.hex() in str(row) for row in security.entries)  # Secrets never reach QML
    assert security.remove('Zone') == '' and 'Zone' not in [row['name'] for row in security.entries]
    assert security.remove('Zone') == "No key store entry named 'Zone'"


def test_freshness_script(security, tmp_path):
    script = tmp_path / 'fv.py'
    script.write_text('def freshness(pdu, data_id, truncated, tx_bits, full_bits):\n    return truncated\n')
    bad = tmp_path / 'bad.py'
    bad.write_text('x = 1\n')
    from can_explorer.core.decoding import FrameDecoder

    decoder = FrameDecoder()
    security.attach(decoder)
    assert type(decoder.secoc.freshness).__name__ == 'CounterFreshness'
    assert 'does not define freshness' in security.set_freshness_script(QUrl.fromLocalFile(str(bad)))
    assert security.set_freshness_script(QUrl.fromLocalFile(str(script))) == ''
    assert security.freshness_script == str(script)
    assert type(decoder.secoc.freshness).__name__ == 'ScriptFreshness'  # Attached decoders switch at once
    assert security.set_freshness_script(QUrl()) == '' and security.freshness_script == ''
    assert type(decoder.secoc.freshness).__name__ == 'CounterFreshness'


async def test_secured_frames_are_checked_in_the_trace(manager, secoc_arxml):
    channel = f'security-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    connection.load_database(QUrl.fromLocalFile(str(secoc_arxml)))
    [row] = connection.secoc_protections
    assert row['message'] == 'SecuredStatus' and row['data_id'] == '0x12' and not row['checked']
    assert row['reason'] == 'no key for SecuredStatus (Data ID 0x12) in the key store'

    security = manager.security
    changes = []
    connection.secoc_changed.connect(lambda: changes.append(1))
    assert security.create('pw', 'pw') == ''
    assert security.add_secoc_key('Powertrain', KEY.hex(), '0x12', '') == ''
    assert changes and connection.secoc_protections[0] == {
        'message': 'SecuredStatus',
        'data_id': '0x12',
        'key': 'Powertrain',
        'checked': True,
        'reason': '',
    }

    sender = SecOcVerifier(StaticSecOcKeys({'*': (KEY, '')}))
    peer = can.Bus(interface='virtual', channel=channel)
    good = sender.secure(SECURED_STATUS, b'\x05\x00', 1)
    for data in (good, sender.secure(SECURED_STATUS, b'\x05\x00', 2), bytes([good[0] ^ 1]) + good[1:]):
        peer.send(can.Message(arbitration_id=0x300, is_extended_id=False, data=data))
    await wait_until(lambda: connection.session.trace.total == 3)
    peer.shutdown()
    trace = connection.session.trace
    infos = [trace.get(trace.first + index).info for index in range(3)]
    assert infos[0] == '[SecOC ok, FV 1]' and infos[1] == '[SecOC ok, FV 2]'
    assert infos[2].startswith('[SecOC MAC ERROR')

    security.lock()  # Locking takes the keys away from new frames
    assert connection.secoc_protections[0]['reason'] == 'the key store is locked'


def _esp_frame(spi: int, key: bytes) -> bytes:
    inner_udp = struct.pack('>HHHH', 50000, 30509, 24, 0) + struct.pack(
        '>IIIBBBB', (0x1234 << 16) | 0x45, 8, 0, 1, 1, 0, 0
    )
    inner = struct.pack('>BBHHHBBH', 0x45, 0, 20 + len(inner_udp), 0, 0, 64, 17, 0) + bytes([10, 0, 0, 1, 10, 0, 0, 2])
    plain = inner + inner_udp
    pad = (-(len(plain) + 2)) % 4
    plain += bytes(range(1, pad + 1)) + bytes([pad, 4])
    header, iv = struct.pack('>II', spi, 1), os.urandom(8)
    esp = header + iv + AESGCM(key[:-4]).encrypt(key[-4:] + iv, plain, header)
    outer = struct.pack('>BBHHHBBH', 0x45, 0, 20 + len(esp), 0, 0, 64, 50, 0) + bytes([1, 1, 1, 1, 2, 2, 2, 2])
    return bytes.fromhex('112233445566aabbccddeeff0800') + outer + esp


async def test_ethernet_capture_is_decrypted_once_unlocked(security, tmp_path):
    from can_explorer.core.ethernet import RawPacket, write_pcap

    key = KEY + b'salt'
    pcap = tmp_path / 'esp.pcap'
    write_pcap(pcap, [RawPacket(1.0, _esp_frame(0x1000, key))])
    assert security.create('pw', 'pw') == ''
    assert security.add_ipsec_sa('ESP', '1000', 'aes-gcm', key.hex(), 'none', '') == ''
    security.lock()

    controller = EthernetController(security=security)
    try:
        assert controller.decryption_text == ''
        assert controller.open_pcap(QUrl.fromLocalFile(str(pcap))) == ''
        await wait_until(lambda: not controller.busy and controller.count == 1)
        assert controller.packets[0]['protocol'] == 'IPsec ESP'
        assert security.unlock('pw') == ''  # Unlocking decrypts the loaded capture again
        await wait_until(lambda: not controller.busy and controller.packets[0]['protocol'] == 'SOME/IP')
        assert controller.decryption_text == 'Decrypting with 1 IPsec association'
        assert any(row['label'] == 'Decrypted' for row in controller.dissect(1))
    finally:
        controller.shutdown()


async def test_doip_tls_entries(security, qapp):
    doip = DoipController(security=security)
    try:
        options = {'host': '127.0.0.1', 'port': 1, 'tls': True, 'tls_trusted': 'Missing', 'tls_client': ''}
        assert security.create('pw', 'pw') == ''
        assert doip.connect_entity(options) == "No key store entry named 'Missing'"
        # The simulated entity never uses TLS, whatever is chosen
        assert doip.connect_entity({**options, 'host': SIMULATED}) == ''
        await wait_until(lambda: not doip.busy)
        assert doip.is_open, [entry['text'] for entry in doip.log]  # Why the connection failed
        assert 'TLS' not in doip.connection_text
    finally:
        doip.shutdown()
        await asyncio.sleep(0.05)


# Sending secured PDUs, statistics, seed/key algorithms and expiry notices


def open_secured_connection(manager, secoc_arxml):
    channel = f'security-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    connection.load_database(QUrl.fromLocalFile(str(secoc_arxml)))
    return connection, channel


async def receive(bus, count, identifier=0x300):
    frames = []
    while len(frames) < count:
        frame = await asyncio.to_thread(bus.recv, 1.0)
        assert frame is not None, 'Timed out'
        if frame.arbitration_id == identifier:
            frames.append(bytes(frame.data))
    return frames


async def test_transmit_and_restbus_secure_secured_pdus(manager, secoc_arxml):
    connection, channel = open_secured_connection(manager, secoc_arxml)
    ecu = can.Bus(interface='virtual', channel=channel)
    check = SecOcVerifier(StaticSecOcKeys({'*': (KEY, '')}))
    try:
        assert 'no key' in connection.send('300', '05 00', False, False, 0, False, False)  # Refused without a key
        assert manager.security.create('pw', 'pw') == ''
        assert manager.security.add_secoc_key('Powertrain', KEY.hex(), '*', '') == ''
        assert connection.secure_transmit
        assert connection.send('300', '05 00', False, False, 0, False, False) == ''
        [secured] = await receive(ecu, 1)
        assert check.check(SECURED_STATUS, secured).ok and secured[:2] == b'\x05\x00'
        assert 'software timing' in connection.send('300', '05 00', False, False, 10, True, False)

        connection.set_secure_transmit(False)  # As typed, e.g. to see the ECU reject it
        assert connection.send('300', '05 00 00 00 00 00', False, False, 0, False, False) == ''
        [raw] = await receive(ecu, 1)
        assert raw == b'\x05' + bytes(5) and not check.check(SECURED_STATUS, raw).ok
        connection.set_secure_transmit(True)

        restbus = connection.restbus
        row = next(row for row in restbus.messages if row['name'] == 'SecuredStatus')
        assert row['secured'] and row['problem'] == ''
        assert [signal['name'] for signal in restbus.message_signals('SecuredStatus')] == ['AliveCounter']
        restbus.set_all(False)
        restbus.set_enabled('SecuredStatus', True)
        assert restbus.start() == ''
        frames = await receive(ecu, 4)
        restbus.stop()
        assert all(check.check(SECURED_STATUS, data).ok for data in frames)

        statistics = connection.secoc_statistics['SecuredStatus']  # Own frames are checked in the trace too
        assert statistics['sent'] >= 5 and statistics['ok'] >= 1 and statistics['mac_errors'] >= 1

        manager.security.lock()
        assert restbus.start() == 'SecuredStatus: Cannot secure SecuredStatus: the key store is locked'
        restbus.stop()
        assert next(row for row in restbus.messages if row['name'] == 'SecuredStatus')['problem']
    finally:
        ecu.shutdown()


def test_seed_key_entries_in_diagnostics(security, tmp_path):
    script = tmp_path / 'oem.py'
    script.write_text('def compute_key(level, seed):\n    return bytes(b ^ 0xFF for b in seed)\n')
    assert security.create('pw', 'pw') == ''
    assert security.add_seed_key('OEM', QUrl.fromLocalFile(str(script))) == ''
    [row] = security.entries
    assert row['kind'] == 'Seed/key algorithm' and row['details'] == 'compute_key from oem.py'
    bad = tmp_path / 'bad.py'
    bad.write_text('x = 1\n')
    assert 'does not define compute_key' in security.add_seed_key('Bad', QUrl.fromLocalFile(str(bad)))


async def test_diagnostics_unlock_with_a_key_store_algorithm(manager, tmp_path):
    from can_explorer.core.session import BusSession
    from can_explorer.core.simulation import UdsEcuSimulator
    from can_explorer.core.sources import BusConfig, LiveSource

    channel = f'security-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    simulator = UdsEcuSimulator(ecu_session)
    simulator.start()
    script = tmp_path / 'oem.py'
    script.write_text('def compute_key(level, seed):\n    return bytes(b ^ 0xFF for b in seed)\n')
    security = manager.security
    assert security.create('pw', 'pw') == ''
    assert security.add_seed_key('Simulator', QUrl.fromLocalFile(str(script))) == ''
    diagnostics = connection.diagnostics
    assert diagnostics.raw_request('10 03') == ''  # The simulated ECU grants security access in this session
    await wait_until(lambda: not diagnostics.busy)
    diagnostics.set_key_entry('Simulator')
    assert diagnostics.key_file == 'keystore:Simulator'
    assert diagnostics.security_access(1) == ''
    await wait_until(lambda: not diagnostics.busy)
    assert diagnostics.history[-1]['description'] == 'Security level 1 unlocked'
    security.lock()
    assert "Seed/key algorithm 'Simulator'" in diagnostics.security_access(1)
    simulator.stop()
    ecu_session.stop()


def test_expiry_notices(security, certificates, tmp_path):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    notices = []
    security.notice.connect(notices.append)
    assert security.create('pw', 'pw') == ''
    assert security.add_tls_ca('Lab', certificates['ca']) == ''  # Valid for 10 days: announced
    assert notices == [notices[0]] and notices[0].startswith("Trusted TLS certificates 'Lab' expires in ")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Old')])
    now = datetime.datetime.now(datetime.timezone.utc)
    old = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=30))
        .not_valid_after(now - datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / 'old.pem'
    path.write_bytes(old.public_bytes(serialization.Encoding.PEM))
    assert security.add_tls_ca('Old', QUrl.fromLocalFile(str(path))) == ''
    assert notices[-1].startswith("Trusted TLS certificates 'Old' has expired (")
    count = len(notices)
    assert security.check_expiry() == [] and len(notices) == count  # Each announced once


async def test_mcp_secoc_status(manager, secoc_arxml):
    from can_explorer.mcp.service import CanService

    connection, _channel = open_secured_connection(manager, secoc_arxml)
    service = CanService.attach(connection.session)
    [pdu] = service.secoc_status()['secured_pdus']
    assert pdu['pdu'] == 'SecuredStatus' and pdu['data_id'] == 18 and not pdu['checked']
    assert manager.security.create('pw', 'pw') == ''
    assert manager.security.add_secoc_key('Powertrain', KEY.hex(), '*', '') == ''
    [pdu] = service.secoc_status()['secured_pdus']
    assert pdu['checked'] and pdu['key'] == 'Powertrain' and pdu['mac_bits'] == 28
    assert KEY.hex() not in str(service.secoc_status())  # Results only, never keys


async def test_ethernet_tls_key_log(qapp, tmp_path):
    from can_explorer.core.ethernet import RawPacket, write_pcap

    controller = EthernetController()
    try:
        keylog = tmp_path / 'keys.log'
        keylog.write_text('CLIENT_RANDOM ' + 'aa' * 32 + ' ' + 'bb' * 48 + '\n')
        bad = tmp_path / 'bad.log'
        bad.write_text('nonsense\n')
        assert 'Line 1' in controller.load_tls_keylog(QUrl.fromLocalFile(str(bad)))
        pcap = tmp_path / 'one.pcap'
        write_pcap(pcap, [RawPacket(1.0, _esp_frame(0x1000, KEY + b'salt'))])
        assert controller.open_pcap(QUrl.fromLocalFile(str(pcap))) == ''
        await wait_until(lambda: not controller.busy and controller.count == 1)
        assert controller.load_tls_keylog(QUrl.fromLocalFile(str(keylog))) == ''
        assert controller.tls_keylog == str(keylog)
        assert controller.decryption_text == 'Decrypting with a TLS key log of 1 session'
        await wait_until(lambda: not controller.busy)
        assert controller.load_tls_keylog(QUrl()) == '' and controller.tls_keylog == ''
    finally:
        controller.shutdown()


async def test_diagnostics_authenticate_with_a_key_store_certificate(manager, certificates, tmp_path):
    """Authentication (0x29) from the Diagnostics view, with a client certificate of the Security Manager."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from can_explorer.core.session import BusSession
    from can_explorer.core.simulation import UdsEcuSimulator, UdsServer
    from can_explorer.core.sources import BusConfig, LiveSource
    from can_explorer.core.uds_auth import AuthenticationServer

    def issue(common_name, key, issuer=None, issuer_key=None, ca=False):
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer.subject if issuer else subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(issuer_key or key, hashes.SHA256())
        )

    ca_key, tester_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = issue('OEM CA', ca_key, ca=True)
    tester = issue('Workshop tester', tester_key, ca, ca_key)
    (tmp_path / 'tester.pem').write_bytes(tester.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'tester.key').write_bytes(
        tester_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )

    channel = f'security-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    server = UdsServer(authentication=AuthenticationServer([ca], 'ecdsa-sha256'), authentication_dids=(0xF198,))
    simulator = UdsEcuSimulator(ecu_session, server=server)
    simulator.start()
    security = manager.security
    diagnostics = connection.diagnostics
    try:
        assert "No client certificate named 'Tester'" in diagnostics.authenticate({'certificate': 'Tester'})
        assert security.create('pw', 'pw') == ''
        assert (
            security.add_tls_client(
                'Tester',
                QUrl.fromLocalFile(str(tmp_path / 'tester.pem')),
                QUrl.fromLocalFile(str(tmp_path / 'tester.key')),
                '',
            )
            == ''
        )
        assert [scheme['value'] for scheme in diagnostics.signature_schemes][0] == 'ecdsa-sha256'
        assert 'does not fit the signature scheme ed25519' in diagnostics.authenticate(
            {'certificate': 'Tester', 'scheme': 'ed25519'}
        )
        assert diagnostics.authenticate({'certificate': 'Tester', 'scheme': 'ecdsa-sha256'}) == ''
        await wait_until(lambda: not diagnostics.busy)
        entry = diagnostics.history[-1]
        assert entry['description'].endswith('proofOfOwnership: ownership verified, authentication complete'), entry
        assert diagnostics.authenticated and server.authentication.authenticated

        assert diagnostics.deauthenticate() == ''
        await wait_until(lambda: not diagnostics.busy)
        assert not diagnostics.authenticated and diagnostics.history[-1]['description'] == (
            'deAuthenticate: de-authentication successful'
        )
        # An ECU that does not trust the tester's issuer refuses the certificate
        server.authentication.trusted = [certificates_ca(certificates)]
        assert diagnostics.authenticate({'certificate': 'Tester', 'scheme': 'ecdsa-sha256'}) == ''
        await wait_until(lambda: not diagnostics.busy)
        assert 'certificateVerificationFailed-InvalidChainOfTrust' in diagnostics.history[-1]['description']
        assert not diagnostics.authenticated
    finally:
        simulator.stop()
        ecu_session.stop()


def certificates_ca(certificates):
    from cryptography import x509

    return x509.load_pem_x509_certificate(open(certificates['ca'].toLocalFile(), 'rb').read())


def test_keychain_unlocks_at_start(qapp, tmp_path):
    from can_explorer.core import keychain

    path = tmp_path / 'keys.json'
    first = SecurityController(KeyStore(path, scrypt_cost=16))
    assert first.keychain_available and not first.remembered
    assert first.create('pw', 'pw') == ''
    first.lock()
    assert first.unlock_and_remember('pw', True) == ''
    assert first.remembered and keychain.recall(path) == 'pw'
    assert SecurityController(KeyStore(path, scrypt_cost=16)).unlocked  # The next start unlocks by itself
    assert first.change_password('pw', 'new', 'new') == ''
    assert keychain.recall(path) == 'new'  # The kept password follows the change
    first.forget_password()
    assert not first.remembered and not SecurityController(KeyStore(path, scrypt_cost=16)).unlocked
    keychain.remember(path, 'stale')  # E.g. the password was changed on another computer
    assert not SecurityController(KeyStore(path, scrypt_cost=16)).unlocked
    assert keychain.recall(path) is None  # A password that does not unlock is forgotten


def test_export_import_and_test_certificates(security, tmp_path):
    assert security.create('pw', 'pw') == ''
    assert (
        security.create_test_pki({'prefix': 'Bench', 'key_type': 'ed25519', 'days': '30', 'addresses': '10.0.0.5'})
        == ''
    )
    assert [row['name'] for row in security.entries] == ['Bench ECU', 'Bench tester', 'Bench CA']
    assert "entry named 'Bench CA' already" in security.create_test_pki({'prefix': 'Bench'})
    bundle = QUrl.fromLocalFile(str(tmp_path / 'bench.ckb'))
    assert security.export_entries(bundle, ['Bench CA', 'Bench tester'], 'a', 'b') == 'The passphrases differ'
    assert security.export_entries(bundle, ['Bench CA', 'Bench tester'], 'bundle', 'bundle') == ''
    other = SecurityController(KeyStore(tmp_path / 'other.json', scrypt_cost=16))
    assert other.create('pw2', 'pw2') == ''
    assert other.import_entries(bundle, 'wrong', False)['error'] == 'Wrong passphrase for the key bundle'
    result = other.import_entries(bundle, 'bundle', False)
    assert result == {'error': '', 'imported': ['Bench CA', 'Bench tester'], 'skipped': []}
    assert other.import_entries(bundle, 'bundle', False)['skipped'] == ['Bench CA', 'Bench tester']


def test_trip_reset_freshness_is_kept_and_applied(qapp, tmp_path):
    from PyQt6.QtCore import QSettings

    from can_explorer.core.decoding import FrameDecoder
    from can_explorer.core.secoc_check import CounterFreshness, TripResetFreshness

    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    security = SecurityController(KeyStore(tmp_path / 'keys.json', scrypt_cost=16), settings)
    decoder = FrameDecoder()
    security.attach(decoder)
    assert security.freshness_mode == 'counter' and security.trip_reset['trip_bits'] == 16
    assert 'at most 64 bits' in security.set_trip_reset({'trip_bits': 24, 'reset_bits': 24, 'message_bits': 48})
    assert security.set_trip_reset({'trip_bits': '8', 'reset_bits': '8', 'sync_pdu': 'TripResetSyncMsg'}) == ''
    assert security.freshness_mode == 'trip-reset' and isinstance(decoder.secoc.freshness, TripResetFreshness)
    assert decoder.secoc.freshness.config.sync_pdu == 'TripResetSyncMsg'
    restored = SecurityController(KeyStore(tmp_path / 'keys.json', scrypt_cost=16), settings)
    assert restored.freshness_mode == 'trip-reset' and restored.trip_reset['reset_bits'] == 8
    assert security.set_freshness_script(QUrl()) == ''  # Back to counters
    assert security.freshness_mode == 'counter' and isinstance(decoder.secoc.freshness, CounterFreshness)
    assert SecurityController(KeyStore(tmp_path / 'keys.json', scrypt_cost=16), settings).freshness_mode == 'counter'


async def test_connection_security_events(manager, tmp_path):
    channel = f'security-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    peer = can.Bus(interface='virtual', channel=channel)
    for _ in range(3):
        peer.send(
            can.Message(arbitration_id=0x7E8, is_extended_id=False, data=bytes([0x03, 0x7F, 0x27, 0x36, 0, 0, 0, 0]))
        )
    await wait_until(lambda: connection.session.trace.total == 3)
    peer.shutdown()
    [event] = connection.security_events
    assert event['kind'] == 'refused' and event['count'] == 3 and event['severity'] == 'high'
    assert connection.security_event_counts == {'events': 1, 'high': 1, 'frames': 3}
    assert (
        connection.export_security_events(QUrl.fromLocalFile(str(tmp_path / 'events')))
        == 'Exported 1 events to events.csv'
    )
    connection.clear_security_events()
    assert connection.security_events == []


def test_audit_log_of_the_security_manager(security, tmp_path):
    from can_explorer.core import audit

    assert audit.get_log() is security.audit_log
    assert security.unlock('wrong') != ''  # No store yet
    assert security.create('pw', 'pw') == ''
    assert security.add_secoc_key('Powertrain', KEY.hex(), '*', '') == ''
    assert security.export_entries(QUrl.fromLocalFile(str(tmp_path / 'b.ckb')), ['Powertrain'], 'x', 'x') == ''
    security.lock()
    assert security.unlock('nope') == 'Wrong password for the key store'
    assert security.unlock('pw') == ''
    actions = [row['action'] for row in reversed(security.audit_records)]
    assert actions == [
        'unlock failed',
        'create key store',
        'add entry',
        'entries exported',
        'locked',
        'unlock failed',
        'unlock',
    ]
    assert security.audit_records[-1]['signed'] is False  # Written before the store existed
    assert (
        security.verify_audit() == '7 records, chain intact (2 unsigned, written while the key store was locked)'
    )  # The failed unlocks
    assert (
        security.export_audit(QUrl.fromLocalFile(str(tmp_path / 'audit.jsonl')))
        == 'Exported the audit log to audit.jsonl'
    )
    assert len((tmp_path / 'audit.jsonl').read_text().splitlines()) == 7


PROFILE = """
class Profile:
    name = "Bench"
    description = "Seed/key and challenge-response for the bench"

    def compute_key(self, level, seed):
        return bytes(b ^ 0xFF for b in seed)

    def prove_challenge(self, challenge, indicator):
        import hashlib, hmac
        return hmac.new(b"bench", challenge, hashlib.sha256).digest()

profile = Profile()
"""


def test_security_profiles_are_loaded_kept_and_removed(qapp, tmp_path):
    from PyQt6.QtCore import QSettings

    path = tmp_path / 'bench.py'
    path.write_text(PROFILE)
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    security = SecurityController(KeyStore(tmp_path / 'keys.json', scrypt_cost=16), settings)
    bad = tmp_path / 'bad.py'
    bad.write_text('x = 1\n')
    assert 'defines neither profile nor create_profile' in security.load_profile(QUrl.fromLocalFile(str(bad)))
    assert security.load_profile(QUrl.fromLocalFile(str(path))) == ''
    [profile] = security.security_profiles
    assert profile['name'] == 'Bench' and profile['capabilities'] == ['compute_key', 'prove_challenge']
    # Loaded again at the next start
    again = SecurityController(KeyStore(tmp_path / 'keys.json', scrypt_cost=16), settings)
    assert [loaded['name'] for loaded in again.security_profiles] == ['Bench']
    again.remove_profile('Bench')
    assert again.security_profiles == []
    assert SecurityController(KeyStore(tmp_path / 'keys.json', scrypt_cost=16), settings).security_profiles == []


def test_key_servers_become_profiles_while_unlocked(security):
    assert security.create('pw', 'pw') == ''
    assert 'starts with https://' in security.add_key_server('Keys', 'keys.example.com', 'token', '')
    assert security.add_key_server('Keys', 'https://keys.example.com', 'bearer-s3cr3t', '') == ''
    [profile] = security.security_profiles
    assert profile['name'] == 'Keys' and profile['source'] == 'key server Keys'
    assert {'compute_key', 'sign', 'prove_challenge'} <= set(profile['capabilities'])
    [entry] = security.entries
    assert entry['kind'] == 'Key server' and entry['details'] == 'https://keys.example.com'
    security.lock()  # The token is a secret: no key server while locked
    assert security.security_profiles == []
    assert security.unlock('pw') == ''
    assert [loaded['name'] for loaded in security.security_profiles] == ['Keys']


def test_uds_authentication_key_entries(security):
    assert security.create('pw', 'pw') == ''
    assert security.auth_key_macs == ['hmac-sha256', 'aes-cmac']
    assert '16, 24 or 32 bytes' in security.add_auth_key('Shared', '0102', 'aes-cmac')
    assert security.add_auth_key('Shared', KEY.hex(), 'aes-cmac') == ''
    [entry] = security.entries
    assert entry['kind'] == 'UDS authentication key' and entry['details'] == 'challenge-response · aes-cmac'


async def test_diagnostics_with_security_profiles_and_challenge_response(manager, tmp_path):
    """Seed/key from a security profile, and authentication by challenge-response with a shared key or a profile."""
    from can_explorer.core import uds_auth
    from can_explorer.core.session import BusSession
    from can_explorer.core.simulation import UdsEcuSimulator, UdsServer
    from can_explorer.core.sources import BusConfig, LiveSource

    channel = f'security-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    acr = uds_auth.symmetric_challenge_response(KEY, 'aes-cmac')
    server = UdsServer(authentication=uds_auth.AuthenticationServer([], challenge_response=acr))
    simulator = UdsEcuSimulator(ecu_session, server=server)
    simulator.start()
    security = manager.security
    diagnostics = connection.diagnostics
    try:
        path = tmp_path / 'bench.py'
        path.write_text(PROFILE)
        assert security.load_profile(QUrl.fromLocalFile(str(path))) == ''
        diagnostics.set_key_source('profile:Bench')
        assert diagnostics.key_file == 'profile:Bench'
        assert diagnostics.raw_request('10 03') == ''
        await wait_until(lambda: not diagnostics.busy)
        assert diagnostics.security_access(1) == ''
        await wait_until(lambda: not diagnostics.busy)
        assert diagnostics.history[-1]['description'] == 'Security level 1 unlocked'

        assert "No authentication key or client certificate named 'Shared'" in (
            diagnostics.authenticate_challenge_response({'source': 'Shared'})
        )
        assert security.create('pw', 'pw') == ''
        assert security.add_auth_key('Shared', KEY.hex(), 'aes-cmac') == ''
        assert 'hex or a dotted OID' in diagnostics.authenticate_challenge_response(
            {'source': 'Shared', 'algorithm': 'sha256'}
        )
        assert diagnostics.authenticate_challenge_response({'source': 'Shared', 'bidirectional': True}) == ''
        await wait_until(lambda: not diagnostics.busy)
        entry = diagnostics.history[-1]
        assert entry['description'].endswith("the ECU's proof verified (proof: shared key (aes-cmac))"), entry
        assert diagnostics.authenticated and server.authentication.authenticated

        # The profile proves with another key: the ECU refuses it
        assert diagnostics.authenticate_challenge_response({'source': 'profile:Bench'}) == ''
        await wait_until(lambda: not diagnostics.busy)
        assert 'ownershipVerificationFailed' in diagnostics.history[-1]['description']
        assert not diagnostics.authenticated
        server.authentication.challenge_response = uds_auth.symmetric_challenge_response(b'bench')
        assert diagnostics.authenticate_challenge_response({'source': 'profile:Bench'}) == ''
        await wait_until(lambda: not diagnostics.busy)
        assert diagnostics.authenticated, diagnostics.history[-1]
        # Certificate exchange needs a profile with a certificate
        assert 'has no certificate to authenticate with' in diagnostics.authenticate({'certificate': 'profile:Bench'})
    finally:
        simulator.stop()
        ecu_session.stop()
