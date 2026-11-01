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
    assert gptp.protocol == 'gPTP' and 'Announce' in gptp.info and gptp.gptp is not None

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


def _pcapng(frames, tsresol=6):
    """A minimal little-endian pcapng with one Ethernet interface and the given (timestamp, data) packets."""

    def block(block_type, body):
        total = 12 + len(body) + (-len(body) % 4)
        return struct.pack('<II', block_type, total) + body + bytes(-len(body) % 4) + struct.pack('<I', total)

    shb = block(0x0A0D0D0A, struct.pack('<IHHq', 0x1A2B3C4D, 1, 0, -1))
    options = struct.pack('<HH', 9, 1) + bytes([tsresol, 0, 0, 0]) + struct.pack('<HH', 0, 0)  # if_tsresol + endofopt
    idb = block(0x00000001, struct.pack('<HHI', ethernet.LINKTYPE_ETHERNET, 0, 65535) + options)
    out = shb + idb
    for timestamp, data in frames:
        units = int(round(timestamp / (10.0**-tsresol)))
        epb = struct.pack('<IIIII', 0, units >> 32, units & 0xFFFFFFFF, len(data), len(data)) + data
        out += block(0x00000006, epb)
    return out


def test_read_pcapng(tmp_path):
    frames = [(1000.0 + i * 0.25, udp_frame('10.0.0.1', '10.0.0.2', 1000 + i, 30509, SOMEIP)) for i in range(3)]
    path = tmp_path / 'capture.pcapng'
    path.write_bytes(_pcapng(frames))
    read = list(ethernet.read_pcap(path))
    assert len(read) == 3
    assert read[0].data == frames[0][1] and abs(read[1].timestamp - 1000.25) < 1e-6
    assert ethernet.decode(read[2].data).protocol == 'SOME/IP'


def test_read_pcapng_without_ethernet_is_rejected(tmp_path):
    path = tmp_path / 'x.pcapng'
    path.write_bytes(b'\x0a\x0d\x0d\x0a' + bytes(60))  # No Section Header body / no interface
    with pytest.raises(ethernet.PcapError):
        list(ethernet.read_pcap(path))


def test_decode_lldp():
    tlv = lambda t, v: struct.pack('>H', (t << 9) | len(v)) + v  # noqa: E731
    lldp = (
        tlv(1, bytes([4]) + bytes.fromhex('001122334455'))  # Chassis id: MAC
        + tlv(2, bytes([5]) + b'eth0')  # Port id: interface name
        + tlv(3, struct.pack('>H', 120))  # TTL
        + tlv(5, b'switch-1')  # System name
        + tlv(0, b'')  # End
    )
    packet = ethernet.decode(eth('01:80:c2:00:00:0e', '00:11:22:33:44:55', ethernet.ETH_LLDP, lldp))
    assert packet.protocol == 'LLDP'
    assert 'switch-1' in packet.info and '00:11:22:33:44:55'[:5] in packet.info or 'eth0' in packet.info
    assert 'TTL 120s' in packet.info


def tcp_segment(sport, dport, seq, payload):
    offset_flags = (5 << 12) | 0x18  # 20-byte header, PSH+ACK
    return struct.pack('>HHIIHHHH', sport, dport, seq, 0, offset_flags, 65535, 0, 0) + payload


def tcp_frame(src_ip, dst_ip, sport, dport, seq, payload):
    segment = tcp_segment(sport, dport, seq, payload)
    return eth('11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_IPV4, ipv4(src_ip, dst_ip, 6, segment) + segment)


def someip_msg(service, method, message_type, payload=b''):
    return (
        struct.pack('>IIIBBBB', (service << 16) | method, 8 + len(payload), 0x00010001, 1, 1, message_type, 0) + payload
    )


def test_stream_decoder_reassembles_someip_over_tcp():
    message = someip_msg(0x1234, 0x8001, 0x02, bytes(20))  # 16-byte header + 20-byte payload
    half = len(message) // 2
    decoder = ethernet.StreamDecoder()
    first = decoder.decode(tcp_frame('10.0.0.1', '10.0.0.2', 40000, 30500, 1000, message[:half]))
    assert first.protocol == 'SOME/IP' and 'segment' in first.info  # Incomplete so far
    second = decoder.decode(tcp_frame('10.0.0.1', '10.0.0.2', 40000, 30500, 1000 + half, message[half:]))
    assert second.protocol == 'SOME/IP' and '1234' in second.info and '8001' in second.info


def test_stream_decoder_ignores_non_someip_tcp():
    decoder = ethernet.StreamDecoder()
    packet = decoder.decode(tcp_frame('10.0.0.1', '10.0.0.2', 1234, 80, 1, b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'))
    assert packet.protocol == 'TCP'  # Not mistaken for SOME/IP


def someip_tp(service, method, offset_units, more, segment):
    tp_header = ((offset_units << 4) | (1 if more else 0)).to_bytes(4, 'big')
    return someip_msg(service, method, 0x22, tp_header + segment)  # 0x22 = NOTIFICATION with the TP flag


def test_stream_decoder_reassembles_someip_tp_over_udp():
    decoder = ethernet.StreamDecoder()
    first = decoder.decode(
        udp_frame('10.0.0.1', '10.0.0.2', 30500, 30501, someip_tp(0x1234, 0x8003, 0, True, b'A' * 16))
    )
    assert first.protocol == 'SOME/IP' and 'TP segment' in first.info
    second = decoder.decode(
        udp_frame('10.0.0.1', '10.0.0.2', 30500, 30501, someip_tp(0x1234, 0x8003, 1, False, b'B' * 4))
    )
    assert second.protocol == 'SOME/IP' and '1234' in second.info and '8003' in second.info


def test_stream_decoder_tracks_services():
    option_body = b'\x00' + socket.inet_aton('192.168.1.5') + b'\x00' + bytes([0x11]) + (30509).to_bytes(2, 'big')
    option = len(option_body).to_bytes(2, 'big') + bytes([0x04]) + option_body
    entry = bytes([0x01, 0, 0, 1 << 4]) + (0x1234).to_bytes(2, 'big') + (0x0001).to_bytes(2, 'big')
    entry += bytes([2]) + (3).to_bytes(3, 'big') + (7).to_bytes(4, 'big')  # major 2, TTL 3, minor 7
    sd = bytes([0xC0, 0, 0, 0]) + len(entry).to_bytes(4, 'big') + entry + len(option).to_bytes(4, 'big') + option
    decoder = ethernet.StreamDecoder()
    packet = decoder.decode(udp_frame('192.168.1.5', '224.0.0.1', 30490, 30490, someip_msg(0xFFFF, 0x8100, 0x02, sd)))
    assert packet.protocol == 'SOME/IP' and 'OfferService' in packet.info
    assert (0x1234, 0x0001) in decoder.services
    service = decoder.services[(0x1234, 0x0001)]
    assert service.major_version == 2 and service.minor_version == 7
    assert '192.168.1.5:30509/UDP' in service.endpoints
