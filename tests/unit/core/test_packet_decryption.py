import hashlib
import hmac
import os
import socket
import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from can_explorer.core import ethernet
from can_explorer.core.keystore import KeyStore
from can_explorer.core.packet_decryption import EspSa, MacsecSa, PacketDecryptor

DST, SRC = '11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff'
SOMEIP = struct.pack('>IIIBBBB', (0x1234 << 16) | 0x0045, 8, 0, 0x01, 0x01, 0x00, 0x00)  # Request, no payload


def eth(dst, src, ethertype, payload=b''):
    return (
        bytes.fromhex(dst.replace(':', ''))
        + bytes.fromhex(src.replace(':', ''))
        + struct.pack('>H', ethertype)
        + payload
    )


def ipv4(src_ip, dst_ip, protocol, l4):
    return (
        struct.pack('>BBHHHBBH', 0x45, 0, 20 + len(l4), 0, 0, 64, protocol, 0)
        + socket.inet_aton(src_ip)
        + socket.inet_aton(dst_ip)
    )


def udp(sport, dport, payload):
    return struct.pack('>HHHH', sport, dport, 8 + len(payload), 0) + payload


def ipv6_frame(src_ip, dst_ip, next_header, payload):
    header = (
        struct.pack('>IHBB', 0x60000000, len(payload), next_header, 64)
        + socket.inet_pton(socket.AF_INET6, src_ip)
        + socket.inet_pton(socket.AF_INET6, dst_ip)
    )
    return eth(DST, SRC, ethernet.ETH_IPV6, header + payload)


GCM_KEY = bytes(range(16)) + b'SALT'  # Key and the RFC 4106 salt
CBC_KEY, AUTH_KEY = bytes(range(16, 32)), bytes(range(32))


def esp_trailer(payload: bytes, next_header: int, block: int) -> bytes:
    pad = (-(len(payload) + 2)) % block
    return payload + bytes(range(1, pad + 1)) + bytes([pad, next_header])


def esp_gcm(spi: int, seq: int, payload: bytes, next_header: int, key=GCM_KEY) -> bytes:
    header, iv = struct.pack('>II', spi, seq), os.urandom(8)
    return header + iv + AESGCM(key[:-4]).encrypt(key[-4:] + iv, esp_trailer(payload, next_header, 4), header)


def esp_cbc(spi: int, seq: int, payload: bytes, next_header: int, digest=hashlib.sha256, icv=16) -> bytes:
    header, iv = struct.pack('>II', spi, seq), os.urandom(16)
    encryptor = Cipher(algorithms.AES(CBC_KEY), modes.CBC(iv)).encryptor()
    body = header + iv + encryptor.update(esp_trailer(payload, next_header, 16)) + encryptor.finalize()
    return body + hmac.new(AUTH_KEY, body, digest).digest()[:icv]


def esp_null(spi: int, seq: int, payload: bytes, next_header: int) -> bytes:
    body = struct.pack('>II', spi, seq) + esp_trailer(payload, next_header, 4)
    return body + hmac.new(AUTH_KEY, body, hashlib.sha1).digest()[:12]


def outer_ipv4(esp: bytes) -> bytes:
    return eth(DST, SRC, ethernet.ETH_IPV4, ipv4('10.0.0.1', '10.0.0.2', 50, esp) + esp)


SOMEIP_DATAGRAM = udp(50000, 30509, SOMEIP)
INNER_IPV4 = ipv4('192.168.0.10', '192.168.0.20', 17, SOMEIP_DATAGRAM) + SOMEIP_DATAGRAM

DECRYPTOR = PacketDecryptor(
    esp=[
        EspSa('gcm', 0x1000, 'aes-gcm', GCM_KEY),
        EspSa('cbc', 0x2000, 'aes-cbc', CBC_KEY, 'hmac-sha256-128', AUTH_KEY),
        EspSa('null', 0x3000, 'null', b'', 'hmac-sha1-96', AUTH_KEY),
    ]
)


def test_esp_gcm_tunnel_mode_is_decoded_as_what_it_carries():
    frame = outer_ipv4(esp_gcm(0x1000, 1, INNER_IPV4, 4))
    assert ethernet.decode(frame).protocol == 'IPsec ESP'
    result = DECRYPTOR.decrypt(frame)
    assert result.label == 'ESP SPI 0x00001000 (gcm)' and result.frame == eth(DST, SRC, ethernet.ETH_IPV4, INNER_IPV4)
    packet = ethernet.StreamDecoder(decryptor=DECRYPTOR).decode(frame, number=1)
    assert packet.protocol == 'SOME/IP' and packet.src == '192.168.0.10' and packet.length == len(frame)
    assert packet.decrypted == 'ESP SPI 0x00001000 (gcm)'
    assert packet.info.startswith('[ESP SPI 0x00001000 (gcm) decrypted] ')


def test_esp_cbc_transport_mode_rebuilds_the_ip_header():
    frame = outer_ipv4(esp_cbc(0x2000, 7, SOMEIP_DATAGRAM, 17))
    result = DECRYPTOR.decrypt(frame)
    assert result.frame is not None
    packet = ethernet.decode(result.frame)
    assert packet.l4 == 'UDP' and packet.protocol == 'SOME/IP' and packet.src == '10.0.0.1'
    header = result.frame[14:34]
    assert header[9] == 17 and struct.unpack('>H', header[2:4])[0] == 20 + len(SOMEIP_DATAGRAM)
    total = sum(struct.unpack('>10H', header))
    assert (total & 0xFFFF) + (total >> 16) == 0xFFFF  # Valid header checksum


def test_esp_null_encryption_over_ipv6():
    frame = ipv6_frame('fd00::1', 'fd00::2', 50, esp_null(0x3000, 1, SOMEIP_DATAGRAM, 17))
    result = DECRYPTOR.decrypt(frame)
    packet = ethernet.decode(result.frame)
    assert packet.l3 == 'IPv6' and packet.protocol == 'SOME/IP'
    assert struct.unpack('>H', result.frame[18:20])[0] == len(SOMEIP_DATAGRAM)


def test_esp_failures_are_explained():
    unknown = DECRYPTOR.decrypt(outer_ipv4(esp_gcm(0x9999, 1, INNER_IPV4, 4)))
    assert unknown.frame is None and unknown.describe() == 'ESP SPI 0x00009999: no security association for this SPI'
    wrong_key = outer_ipv4(esp_gcm(0x1000, 1, INNER_IPV4, 4, key=bytes(16) + b'SALT'))
    assert 'integrity check failed' in DECRYPTOR.decrypt(wrong_key).error
    tampered = bytearray(outer_ipv4(esp_cbc(0x2000, 1, INNER_IPV4, 4)))
    tampered[-20] ^= 1
    assert 'integrity check failed' in DECRYPTOR.decrypt(bytes(tampered)).error
    # In the packet list a failure is shown next to what the frame is
    packet = ethernet.StreamDecoder(decryptor=DECRYPTOR).decode(wrong_key)
    assert packet.protocol == 'IPsec ESP' and packet.info.endswith(
        'integrity check failed (wrong key, or not AES-GCM with a 16-byte ICV)]'
    )
    assert DECRYPTOR.decrypt(outer_ipv4(esp_gcm(0x1000, 1, b'', 59))).error == 'dummy packet (no next header)'


def test_frames_without_protection_or_associations_are_left_alone():
    plain = eth(DST, SRC, ethernet.ETH_IPV4, INNER_IPV4)
    assert DECRYPTOR.decrypt(plain) is None
    assert PacketDecryptor().decrypt(outer_ipv4(esp_gcm(0x1000, 1, INNER_IPV4, 4))) is None
    assert not PacketDecryptor() and DECRYPTOR


# IEEE 802.1AE MACsec GCM-AES test vectors (54-byte packets)
IEEE_AUTH_KEY = bytes.fromhex('AD7A2BD03EAC835A6F620FDCB506B345')
IEEE_AUTH_FRAME = bytes.fromhex(
    'D609B1F056637A0D46DF998D88E5222AB2C2846512153524C0895E81'
    '08000F101112131415161718191A1B1C1D1E1F202122232425262728292A2B2C2D2E2F30313233340001'
    'F09478A9B09007D06F46E9B6A1DA25DD'
)
IEEE_ENCRYPT_KEY = bytes.fromhex('071B113B0CA743FECCCF3D051F737382')
IEEE_ENCRYPT_FRAME = bytes.fromhex(
    'E20106D7CD0DF0761E8DCD3D88E54C2A76D457ED'
    '13B4C72B389DC5018E72A171DD85A5D3752274D3A019FBCAED09A425CD9B2E1C9B72EEE7C9DE7D52B3F3'
    'D6A5284F4A6D3FE22A5D6C2B960494C3'
)


def test_macsec_ieee_integrity_only_vector():
    result = PacketDecryptor(macsec=[MacsecSa('lab', IEEE_AUTH_KEY)]).decrypt(IEEE_AUTH_FRAME)
    assert result.label == 'MACsec AN 2 PN 2999092325 (lab)'
    assert result.frame == IEEE_AUTH_FRAME[:12] + IEEE_AUTH_FRAME[28:-16]
    tampered = IEEE_AUTH_FRAME[:40] + bytes([IEEE_AUTH_FRAME[40] ^ 1]) + IEEE_AUTH_FRAME[41:]
    assert 'integrity check failed' in PacketDecryptor(macsec=[MacsecSa('lab', IEEE_AUTH_KEY)]).decrypt(tampered).error


def test_macsec_ieee_encryption_vector_with_the_implicit_end_station_sci():
    result = PacketDecryptor(macsec=[MacsecSa('lab', IEEE_ENCRYPT_KEY)]).decrypt(IEEE_ENCRYPT_FRAME)
    expected = bytes.fromhex('08000F101112131415161718191A1B1C1D1E1F202122232425262728292A2B2C2D2E2F30313233340004')
    assert result.frame == IEEE_ENCRYPT_FRAME[:12] + expected
    assert ethernet.decode(result.frame).l3 == 'IPv4'


def test_macsec_sci_selection():
    other_channel = MacsecSa('other', IEEE_AUTH_KEY, sci=0x1)
    result = PacketDecryptor(macsec=[other_channel]).decrypt(IEEE_AUTH_FRAME)
    assert result.error == 'no secure association for SCI 12153524C0895E81'
    right = MacsecSa('right', IEEE_AUTH_KEY, sci=0x12153524C0895E81)
    assert PacketDecryptor(macsec=[other_channel, right]).decrypt(IEEE_AUTH_FRAME).label.endswith('(right)')
    # Neither an explicit SCI nor the ES bit: the SCI must be entered
    no_sci = IEEE_ENCRYPT_FRAME[:14] + bytes([IEEE_ENCRYPT_FRAME[14] & ~0x40]) + IEEE_ENCRYPT_FRAME[15:]
    assert 'enter the SCI' in PacketDecryptor(macsec=[MacsecSa('lab', IEEE_ENCRYPT_KEY)]).decrypt(no_sci).error


def test_dissection_gets_a_decrypted_branch():
    frame = outer_ipv4(esp_gcm(0x1000, 1, INNER_IPV4, 4))
    tree = ethernet.dissect(frame, decryptor=DECRYPTOR)
    labels = [child.label for child in tree.children]
    assert labels[-1] == 'Decrypted' and 'IPsec ESP' in labels
    branch = tree.children[-1]
    assert branch.value.startswith('ESP SPI 0x00001000 (gcm): ')
    assert [child.label for child in branch.children][:3] == ['Ethernet II', 'IPv4', 'UDP']
    failed = ethernet.dissect(outer_ipv4(esp_gcm(0x4242, 1, INNER_IPV4, 4)), decryptor=DECRYPTOR)
    assert failed.children[-1].label == 'Decryption'
    assert [child.label for child in ethernet.dissect(frame).children][-1] == 'IPsec ESP'  # Without a decryptor


@pytest.fixture
def unlocked_store(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    store.create('pw')
    return store


def test_associations_from_the_key_store(unlocked_store):
    store = unlocked_store
    store.add_ipsec_sa('gcm', 0x1000, 'aes-gcm', GCM_KEY)
    store.add_ipsec_sa('cbc', 0x2000, 'aes-cbc', CBC_KEY, 'hmac-sha256-128', AUTH_KEY)
    store.add_macsec_sa('lab', IEEE_AUTH_KEY, sci=0x12153524C0895E81)
    store.add_secoc_key('not an SA', bytes(16))
    decryptor = PacketDecryptor.from_keystore(store)
    assert sorted(decryptor.esp) == [0x1000, 0x2000] and len(decryptor.macsec) == 1
    assert decryptor.decrypt(outer_ipv4(esp_cbc(0x2000, 1, INNER_IPV4, 4))).frame is not None
    assert decryptor.decrypt(IEEE_AUTH_FRAME).frame is not None
    assert GCM_KEY.hex() not in repr(decryptor.esp) and IEEE_AUTH_KEY.hex() not in repr(decryptor.macsec)
    store.lock()
    assert not PacketDecryptor.from_keystore(store)
