"""Decrypting TLS in captures with a key log, against real sessions made by OpenSSL (Python's ssl module)."""

import datetime
import socket
import ssl
import struct

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from can_explorer.core import doip, ethernet
from can_explorer.core.tls_decrypt import KeyLog, KeyLogError, TlsDecryptor, hkdf_expand_label, prf12

CLIENT = ('10.0.0.1', 50000)
SERVER = ('10.0.0.2', doip.TLS_PORT)
# DoIP messages carried over TLS: a routing activation request and a diagnostic message (UDS ReadDataByIdentifier)
ACTIVATION = doip.encode_message(0x0005, bytes.fromhex('0E80') + bytes(5))
DIAGNOSTIC = doip.encode_message(0x8001, bytes.fromhex('0E80 1001 22F190'))


@pytest.fixture(scope='module')
def certificate(tmp_path_factory):
    directory = tmp_path_factory.mktemp('tls')
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'entity')])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (directory / 'entity.pem').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / 'entity.key').write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return directory


def run_session(certificate, tmp_path, version, ciphers=None):
    """A real TLS session between two in-memory endpoints: the bytes each side sent, in order, and the key log."""
    keylog = tmp_path / 'keys.log'
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate / 'entity.pem', certificate / 'entity.key')
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    client_context.keylog_filename = str(keylog)
    for context in (server_context, client_context):
        context.minimum_version = context.maximum_version = version
        if ciphers:
            context.set_ciphers(ciphers)
    buffers = [ssl.MemoryBIO() for _ in range(4)]
    client = client_context.wrap_bio(buffers[0], buffers[1])
    server = server_context.wrap_bio(buffers[2], buffers[3], server_side=True)
    sent = []

    def pump():
        moved = True
        while moved:
            moved = False
            for sender, outgoing, incoming in (('client', buffers[1], buffers[2]), ('server', buffers[3], buffers[0])):
                data = outgoing.read()
                if data:
                    sent.append((sender, data))
                    incoming.write(data)
                    moved = True

    for _ in range(6):
        for end in (client, server):
            try:
                end.do_handshake()
            except ssl.SSLWantReadError:
                pass
        pump()
    for writer, reader, data in ((client, server, ACTIVATION), (server, client, DIAGNOSTIC)):
        writer.write(data)
        pump()
        assert reader.read(4096) == data
        pump()
    return sent, keylog, client.version(), client.cipher()[0]


def segments(sent):
    """(flow, seq, payload) per TCP segment, with consecutive sequence numbers per direction."""
    seq = {'client': 1000, 'server': 90000}
    for sender, data in sent:
        source, destination = (CLIENT, SERVER) if sender == 'client' else (SERVER, CLIENT)
        yield (source[0], source[1], destination[0], destination[1]), seq[sender], data
        seq[sender] += len(data)


SESSIONS = [
    (ssl.TLSVersion.TLSv1_3, None),
    (ssl.TLSVersion.TLSv1_2, 'ECDHE-ECDSA-AES128-GCM-SHA256'),
    (ssl.TLSVersion.TLSv1_2, 'ECDHE-ECDSA-AES256-GCM-SHA384'),
    (ssl.TLSVersion.TLSv1_2, 'ECDHE-ECDSA-CHACHA20-POLY1305'),
]


@pytest.mark.parametrize('version, ciphers', SESSIONS)
def test_real_sessions_are_decrypted(certificate, tmp_path, version, ciphers):
    sent, keylog, negotiated, suite = run_session(certificate, tmp_path, version, ciphers)
    decryptor = TlsDecryptor(KeyLog.load(keylog))
    records = [record for segment in segments(sent) for record in decryptor.feed(*segment)]
    assert not [record.describe() for record in records if record.error], (negotiated, suite)
    application = [(record.sender, record.plaintext) for record in records if record.content_type == 23]
    assert application[:2] == [('client', ACTIVATION), ('server', DIAGNOSTIC)], (negotiated, suite)


def test_segments_out_of_order_and_retransmitted(certificate, tmp_path):
    sent, keylog, *_ = run_session(certificate, tmp_path, ssl.TLSVersion.TLSv1_3)
    pieces = []
    for flow, seq, data in segments(sent):  # Split every segment in two, send the halves swapped, repeat one
        half = len(data) // 2
        pieces += [(flow, seq + half, data[half:]), (flow, seq, data[:half]), (flow, seq, data[:half])]
    decryptor = TlsDecryptor(KeyLog.load(keylog))
    records = [record for piece in pieces for record in decryptor.feed(*piece)]
    assert [record.plaintext for record in records if record.content_type == 23][:2] == [ACTIVATION, DIAGNOSTIC]


def test_missing_secrets_and_unsupported_suites(certificate, tmp_path):
    sent, keylog, *_ = run_session(certificate, tmp_path, ssl.TLSVersion.TLSv1_3)
    decryptor = TlsDecryptor(KeyLog())
    records = [record for segment in segments(sent) for record in decryptor.feed(*segment)]
    errors = {record.error for record in records if record.error}
    assert errors == {'no TLS 1.3 handshake secrets for this session in the key log'}

    sent, keylog, *_ = run_session(certificate, tmp_path, ssl.TLSVersion.TLSv1_2, 'ECDHE-ECDSA-AES128-SHA256')
    decryptor = TlsDecryptor(KeyLog.load(keylog))
    records = [record for segment in segments(sent) for record in decryptor.feed(*segment)]
    errors = {record.error for record in records if record.error}
    assert errors == {'cipher suite 0xC023 is not supported (only AEAD suites)'}

    sent, keylog, *_ = run_session(certificate, tmp_path, ssl.TLSVersion.TLSv1_2, 'ECDHE-ECDSA-AES128-GCM-SHA256')
    wrong = KeyLog({random: {'CLIENT_RANDOM': bytes(48)} for random in KeyLog.load(keylog).secrets})
    decryptor = TlsDecryptor(wrong)
    records = [record for segment in segments(sent) for record in decryptor.feed(*segment)]
    assert {record.error for record in records if record.error} == {'decryption failed (wrong secrets in the key log?)'}


def test_a_capture_starting_mid_session_is_left_alone(certificate, tmp_path):
    sent, keylog, *_ = run_session(certificate, tmp_path, ssl.TLSVersion.TLSv1_3)
    decryptor = TlsDecryptor(KeyLog.load(keylog))
    flow, seq, data = list(segments(sent))[-1]
    assert decryptor.feed(flow, seq + 3, data[3:]) == []  # Not at a record boundary: not TLS to this decoder


def test_key_log_parsing():
    random = 'aa' * 32
    log = KeyLog.parse(
        f'# comment\n\nCLIENT_RANDOM {random} {"bb" * 48}\nCLIENT_TRAFFIC_SECRET_0 {random} {"cc" * 32}\n'
    )
    assert len(log) == 1 and log.get(bytes.fromhex(random), 'CLIENT_RANDOM') == bytes.fromhex('bb' * 48)
    assert 'bb' not in repr(log)
    for text, message in (('CLIENT_RANDOM aa', 'is not'), (f'X {random} zz', 'hexadecimal'), ('X aa bb', '32 bytes')):
        with pytest.raises(KeyLogError, match=message):
            KeyLog.parse(text)
    with pytest.raises(KeyLogError, match='Cannot read'):
        KeyLog.load('/nonexistent/keys.log')


def test_key_derivation_vectors():
    # RFC 8448 (TLS 1.3 traces), simple 1-RTT handshake: the server handshake traffic secret gives these key and IV
    secret = bytes.fromhex('b67b7d690cc16c4e75e54213cb2d37b4e9c912bcded9105d42befd59d391ad38')
    assert hkdf_expand_label(secret, 'key', b'', 16, 'sha256').hex() == '3fce516009c21727d0f2e4e86ee403bc'
    assert hkdf_expand_label(secret, 'iv', b'', 12, 'sha256').hex() == '5d313eb2671276ee13000b30'
    # The TLS 1.2 PRF (P_SHA256) test vector published on the IETF TLS list
    output = prf12(
        bytes.fromhex('9bbe436ba940f017b17652849a71db35'),
        b'test label',
        bytes.fromhex('a0ba9f936cda311827a6f796ffd5198c'),
        100,
        'sha256',
    )
    assert output.hex().startswith('e3f229ba727be17b8d122620557cd453c2aab21d07c3d495329b52d4e61edb5a6b301791e90d35c9')


def frame(source, destination, seq, payload):
    tcp = struct.pack('>HHIIBBHHH', source[1], destination[1], seq, 0, 5 << 4, 0x18, 65535, 0, 0) + payload
    ip = struct.pack('>BBHHHBBH', 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0)
    ip += socket.inet_aton(source[0]) + socket.inet_aton(destination[0])
    return bytes.fromhex('112233445566aabbccddeeff0800') + ip + tcp


def test_ethernet_capture_decodes_doip_over_tls(certificate, tmp_path):
    sent, keylog, *_ = run_session(certificate, tmp_path, ssl.TLSVersion.TLSv1_3)
    frames = [frame((flow[0], flow[1]), (flow[2], flow[3]), seq, data) for flow, seq, data in segments(sent)]
    plain = ethernet.StreamDecoder()
    assert {plain.decode(data).protocol for data in frames} == {'TLS'}  # Recognised, not readable
    decoder = ethernet.StreamDecoder(tls=TlsDecryptor(KeyLog.load(keylog)))
    packets = [decoder.decode(data, number=index + 1) for index, data in enumerate(frames)]
    doip_packets = [packet for packet in packets if packet.protocol == 'DoIP']
    assert [packet.info for packet in doip_packets][:2] == [
        '[TLS decrypted] routing activation request',
        '[TLS decrypted] diagnostic message',
    ]
    assert doip_packets[0].tls_records[0].plaintext == ACTIVATION
    assert any(packet.info.startswith('TLS ') or 'handshake' in packet.info for packet in packets)
