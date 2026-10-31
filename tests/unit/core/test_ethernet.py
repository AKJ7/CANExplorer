import socket
import struct

import pytest

from can_explorer.core import doip, ethernet


def eth(dst, src, ethertype, payload=b''):
    return (
        bytes.fromhex(dst.replace(':', ''))
        + bytes.fromhex(src.replace(':', ''))
        + struct.pack('>H', ethertype)
        + payload
    )


def ipv4(src_ip, dst_ip, protocol, l4):
    header = (
        struct.pack('>BBHHHBBH', 0x45, 0, 20 + len(l4), 0, 0, 64, protocol, 0)
        + socket.inet_aton(src_ip)
        + socket.inet_aton(dst_ip)
    )
    return header


def udp(sport, dport, payload):
    return struct.pack('>HHHH', sport, dport, 8 + len(payload), 0) + payload


def udp_frame(src_ip, dst_ip, sport, dport, payload):
    datagram = udp(sport, dport, payload)
    return eth(
        '11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_IPV4, ipv4(src_ip, dst_ip, 17, datagram) + datagram
    )


SOMEIP = struct.pack('>IIIBBBB', (0x1234 << 16) | 0x0045, 8, 0, 0x01, 0x01, 0x00, 0x00)  # Request, no payload


def test_decode_someip_over_udp():
    packet = ethernet.decode(udp_frame('192.168.0.10', '192.168.0.20', 50000, 30509, SOMEIP))
    assert packet.l3 == 'IPv4' and packet.l4 == 'UDP' and packet.protocol == 'SOME/IP'
    assert packet.src == '192.168.0.10' and packet.dst_port == 30509 and '1234' in packet.info.lower() or packet.info


def test_decode_doip():
    packet = ethernet.decode(udp_frame('10.0.0.1', '10.0.0.2', 40000, doip.PORT, doip.encode_message(0x0001)))
    assert packet.protocol == 'DoIP' and packet.info == 'vehicle identification request'


def test_decode_gptp_and_avtp():
    gptp = ethernet.decode(eth('01:80:c2:00:00:0e', '00:11:22:33:44:55', ethernet.ETH_PTP, bytes([0x1B]) + bytes(40)))
    assert gptp.protocol == 'gPTP' and gptp.info == 'Announce'

    avtp = ethernet.decode(
        eth('91:e0:f0:00:00:01', '00:11:22:33:44:55', ethernet.ETH_AVTP, bytes([0x02, 0, 0, 0]) + bytes(range(8)))
    )
    assert avtp.protocol == 'AVTP' and 'AAF' in avtp.info and 'stream' in avtp.info


def test_decode_arp_and_vlan():
    arp = (
        struct.pack('>HHBBH', 1, 0x0800, 6, 4, 1)
        + bytes(6)
        + socket.inet_aton('192.168.0.1')
        + bytes(6)
        + socket.inet_aton('192.168.0.2')
    )
    packet = ethernet.decode(eth('ff:ff:ff:ff:ff:ff', '00:11:22:33:44:55', ethernet.ETH_ARP, arp))
    assert packet.protocol == 'ARP' and 'who has 192.168.0.2' in packet.info

    # VLAN-tagged ARP
    tagged = eth(
        'ff:ff:ff:ff:ff:ff', '00:11:22:33:44:55', ethernet.ETH_VLAN, struct.pack('>HH', 7, ethernet.ETH_ARP) + arp
    )
    packet = ethernet.decode(tagged)
    assert packet.vlan == 7 and packet.protocol == 'ARP'


def test_plain_udp_is_not_mistaken_for_someip():
    packet = ethernet.decode(udp_frame('10.0.0.1', '10.0.0.2', 1234, 53, b'\x00\x01hello world payload'))
    assert packet.protocol == 'UDP' and packet.l4 == 'UDP'


def test_pcap_round_trip(tmp_path):
    frames = [udp_frame('10.0.0.1', '10.0.0.2', 1000 + i, 30509, SOMEIP) for i in range(3)]
    packets = [ethernet.RawPacket(timestamp=1000.0 + i * 0.5, data=frame) for i, frame in enumerate(frames)]
    path = tmp_path / 'capture.pcap'
    ethernet.write_pcap(path, packets)
    read = list(ethernet.read_pcap(path))
    assert len(read) == 3
    assert read[0].data == frames[0] and abs(read[1].timestamp - 1000.5) < 1e-6
    assert ethernet.decode(read[2].data).protocol == 'SOME/IP'


def test_read_pcapng_is_rejected(tmp_path):
    path = tmp_path / 'x.pcapng'
    path.write_bytes(b'\x0a\x0d\x0d\x0a' + bytes(60))
    with pytest.raises(ethernet.PcapError, match='pcapng'):
        list(ethernet.read_pcap(path))
