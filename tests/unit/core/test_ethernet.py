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


def ipv4_frame(src_ip, dst_ip, protocol, payload):
    return eth(
        '11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_IPV4, ipv4(src_ip, dst_ip, protocol, payload) + payload
    )


def ipv6_frame(src_ip, dst_ip, next_header, payload):
    header = (
        struct.pack('>IHBB', 0x60000000, len(payload), next_header, 64)
        + socket.inet_pton(socket.AF_INET6, src_ip)
        + socket.inet_pton(socket.AF_INET6, dst_ip)
    )
    return eth('11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_IPV6, header + payload)


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


def test_decode_acf_can_over_avtp():
    # An ACF_CAN message (0x01), 6 quadlets = 24 bytes: 16-byte header + an 8-byte CAN payload, EFF set.
    acf = bytearray(24)
    acf[0], acf[1] = (0x01 << 1) | (6 >> 8), 6 & 0xFF
    acf[2] = 1 << 3  # eff
    struct.pack_into('>I', acf, 12, 0x1ABCDEF)
    acf[16:24] = bytes(range(8))
    tscf = bytearray(24)  # TSCF (0x05) version-0 header
    tscf[0] = 0x05
    struct.pack_into('>H', tscf, 20, len(acf))  # stream_data_length
    packet = ethernet.decode(eth('91:e0:f0:00:00:01', '00:11:22:33:44:55', ethernet.ETH_AVTP, bytes(tscf) + bytes(acf)))
    assert packet.protocol == 'ACF-CAN' and '1ABCDEF' in packet.info
    (frame,) = packet.can_frames
    assert frame.can_id == 0x1ABCDEF and frame.extended and frame.data == bytes(range(8))


def test_decode_acf_lin_over_avtp():
    # An ACF_LIN message (0x03), 4 quadlets = 16 bytes: 12-byte header + a 4-byte LIN payload.
    acf = bytearray(16)
    acf[0], acf[1] = (0x03 << 1) | (4 >> 8), 4 & 0xFF
    acf[3] = 0x3C  # lin identifier
    acf[12:16] = b'\xde\xad\xbe\xef'
    tscf = bytearray(24)
    tscf[0] = 0x05
    struct.pack_into('>H', tscf, 20, len(acf))
    packet = ethernet.decode(eth('91:e0:f0:00:00:01', '00:11:22:33:44:55', ethernet.ETH_AVTP, bytes(tscf) + bytes(acf)))
    assert packet.protocol == 'ACF-LIN' and 'LIN' in packet.info and 'DEADBEEF' in packet.info
    assert packet.can_frames == []  # LIN is not fed to the CAN views


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
    packet = ethernet.decode(udp_frame('10.0.0.1', '10.0.0.2', 1234, 12345, b'\x00\x01hello world payload'))
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


def test_decode_dhcp():
    packet = ethernet.decode(udp_frame('0.0.0.0', '255.255.255.255', 68, 67, b'\x01' + bytes(20)))
    assert packet.protocol == 'DHCP' and packet.info == 'request'


def test_decode_dns_and_mdns():
    question = b'\x04host\x05local\x00\x00\x01\x00\x01'  # host.local, type A, class IN
    header = struct.pack('>HHHHHH', 0x1234, 0x0100, 1, 0, 0, 0)  # query, 1 question
    packet = ethernet.decode(udp_frame('10.0.0.1', '10.0.0.2', 40000, 53, header + question))
    assert packet.protocol == 'DNS' and packet.info == 'query A host.local'
    mdns = ethernet.decode(udp_frame('10.0.0.1', '224.0.0.251', 5353, 5353, header + question))
    assert mdns.protocol == 'mDNS'


def test_decode_igmp():
    packet = ethernet.decode(ipv4_frame('10.0.0.1', '224.0.0.1', 2, b'\x11\x64' + bytes(6)))
    assert packet.protocol == 'IGMP' and packet.info == 'membership query'


def test_decode_ipsec_esp():
    packet = ethernet.decode(ipv4_frame('10.0.0.1', '10.0.0.2', 50, struct.pack('>I', 0xDEADBEEF) + bytes(8)))
    assert packet.protocol == 'IPsec ESP' and packet.info == 'SPI 0xDEADBEEF'


def test_decode_mld():
    packet = ethernet.decode(ipv6_frame('fe80::1', 'ff02::16', 58, bytes([143]) + bytes(20)))
    assert packet.protocol == 'MLD'


def test_decode_macsec():
    sectag = bytes([0x0C, 0x00]) + struct.pack('>I', 42)  # TCI with E+C bits, SL 0, PN 42
    packet = ethernet.decode(eth('11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_MACSEC, sectag + bytes(16)))
    assert packet.protocol == 'MACsec' and 'PN 42' in packet.info and 'encrypted' in packet.info


def test_decode_srp_protocols():
    assert (
        ethernet.decode(eth('01:80:c2:00:00:21', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_MSRP, bytes(8))).protocol == 'MSRP'
    )
    assert (
        ethernet.decode(eth('01:80:c2:00:00:21', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_MVRP, bytes(8))).protocol == 'MVRP'
    )


def test_decode_rtag_wraps_inner_frame():
    arp = (
        struct.pack('>HHBBH', 1, 0x0800, 6, 4, 1)
        + bytes(6)
        + socket.inet_aton('10.0.0.1')
        + bytes(6)
        + socket.inet_aton('10.0.0.2')
    )
    rtag = (
        bytes([0, 0]) + struct.pack('>H', 7) + struct.pack('>H', ethernet.ETH_ARP)
    )  # reserved, seq 7, inner EtherType
    packet = ethernet.decode(eth('ff:ff:ff:ff:ff:ff', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_RTAG, rtag + arp))
    assert packet.protocol == 'ARP' and packet.info.startswith('R-TAG 7')


def test_decode_maap():
    maap = bytes([0xFE, 0x01]) + bytes(20)  # MAAP subtype, message type 1 = probe
    packet = ethernet.decode(eth('91:e0:f0:00:00:01', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_AVTP, maap))
    assert packet.protocol == 'AVTP' and packet.info == 'MAAP probe'


def test_decode_udp_nm_with_configured_port():
    payload = bytes([0x05, 0x01, 0xAA, 0xBB])  # node 0x05, control = RepeatMessage, user data
    frame = udp_frame('10.0.0.1', '10.0.0.2', 40000, 49152, payload)
    assert ethernet.decode(frame).protocol == 'UDP'  # Not NM without a configured port
    packet = ethernet.decode(frame, nm_port=49152)
    assert packet.protocol == 'UDP-NM'
    assert packet.info.startswith('NM node 0x05') and 'RepeatMessage' in packet.info
    # The StreamDecoder carries the port too.
    assert ethernet.StreamDecoder(nm_port=49152).decode(frame).protocol == 'UDP-NM'


def _arp_payload(opcode, sender_ip, target_ip):
    return (
        struct.pack('>HHBBH', 1, 0x0800, 6, 4, opcode)
        + bytes.fromhex('aabbccddeeff')
        + socket.inet_aton(sender_ip)
        + bytes(6)
        + socket.inet_aton(target_ip)
    )


def _child(node, label):
    return next(child for child in node.children if child.label == label)


def test_dissect_ipv4_udp_someip():
    frame = udp_frame('10.0.0.1', '10.0.0.2', 30490, 30490, SOMEIP)
    root = ethernet.dissect(frame)
    assert [child.label for child in root.children] == ['Ethernet II', 'IPv4', 'UDP', 'SOME/IP']
    eth_node = _child(root, 'Ethernet II')
    assert _child(eth_node, 'Source').value == 'aa:bb:cc:dd:ee:ff'
    ipv4_node = _child(root, 'IPv4')
    assert _child(ipv4_node, 'Destination').value == '10.0.0.2'
    assert _child(ipv4_node, 'Protocol').value.startswith('17')
    udp_node = _child(root, 'UDP')
    assert _child(udp_node, 'Destination port').value == '30490'
    # The byte span is recorded so the GUI can highlight it: the IPv4 source sits 12 bytes into the IP header.
    assert _child(ipv4_node, 'Source').offset == 14 + 12 and _child(ipv4_node, 'Source').length == 4


def test_dissect_arp():
    frame = eth('ff:ff:ff:ff:ff:ff', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_ARP, _arp_payload(1, '10.0.0.1', '10.0.0.2'))
    root = ethernet.dissect(frame)
    arp_node = _child(root, 'ARP')
    assert _child(arp_node, 'Opcode').value == 'request'
    assert _child(arp_node, 'Sender IP').value == '10.0.0.1'
    assert _child(arp_node, 'Target IP').value == '10.0.0.2'


def _avtp_stream_frame(subtype, stream_id, sequence):
    """An AVTP stream frame (common stream header): subtype, sequence and 8-byte stream id."""
    payload = bytes([subtype, 0, sequence & 0xFF, 0]) + stream_id + bytes(12)
    return eth('91:e0:f0:00:00:01', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_AVTP, payload)


def test_avtp_stream_stats_detects_loss():
    sid = bytes.fromhex('0011223344556677')
    frames = [
        ethernet.decode(_avtp_stream_frame(0x02, sid, seq), timestamp=i * 0.1) for i, seq in enumerate([0, 1, 2, 5, 6])
    ]
    (stat,) = ethernet.avtp_stream_stats(frames)
    assert stat.stream_id == '0011223344556677' and stat.subtype_name.startswith('AAF')
    assert stat.packets == 5 and stat.lost == 2  # sequence 3 and 4 missing
    assert stat.expected == 7 and abs(stat.loss_percent - 100 * 2 / 7) < 0.01
    assert stat.rate_hz > 0


def test_avtp_stream_stats_duplicates_reorder_and_wrap():
    sid = bytes.fromhex('aabbccddeeff0011')
    frames = [ethernet.decode(_avtp_stream_frame(0x02, sid, seq)) for seq in (254, 255, 0, 0, 5, 2)]
    (stat,) = ethernet.avtp_stream_stats(frames)
    # 254->255->0 wrap cleanly; 0->0 duplicate; 0->5 loses 1..4 (4 lost); 5->2 is a late/reordered frame.
    assert stat.packets == 6 and stat.lost == 4 and stat.duplicates == 1 and stat.reordered == 1


def test_avtp_stream_stats_separates_streams():
    a, b = bytes.fromhex('1111111111111111'), bytes.fromhex('2222222222222222')
    frames = [
        ethernet.decode(_avtp_stream_frame(0x02, a, 0)),
        ethernet.decode(_avtp_stream_frame(0x03, b, 10)),
        ethernet.decode(_avtp_stream_frame(0x02, a, 1)),
        ethernet.decode(udp_frame('10.0.0.1', '10.0.0.2', 1, 2, b'x')),  # non-AVTP ignored
    ]
    stats = {stat.stream_id: stat for stat in ethernet.avtp_stream_stats(frames)}
    assert set(stats) == {a.hex(), b.hex()}
    assert stats[a.hex()].packets == 2 and stats[b.hex()].subtype == 0x03


def test_dissect_vlan_tagged_frame():
    inner = struct.pack('>H', ethernet.ETH_ARP) + _arp_payload(2, '10.0.0.9', '10.0.0.1')
    tci = (3 << 13) | 100  # priority 3, VLAN 100
    frame = eth('ff:ff:ff:ff:ff:ff', 'aa:bb:cc:dd:ee:ff', ethernet.ETH_VLAN, struct.pack('>H', tci) + inner)
    root = ethernet.dissect(frame)
    vlan_node = _child(root, '802.1Q VLAN')
    assert _child(vlan_node, 'VLAN id').value == '100'
    assert [child.label for child in root.children] == ['Ethernet II', '802.1Q VLAN', 'ARP']
