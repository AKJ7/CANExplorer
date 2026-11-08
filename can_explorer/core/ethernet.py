"""Automotive Ethernet capture and decoding: read/write pcap files and decode frames down the stack (Ethernet,
VLAN, IPv4/IPv6, ARP, UDP/TCP) and the automotive protocols on top — SOME/IP (reusing someip.py), DoIP, gPTP
(IEEE 802.1AS), AVTP (IEEE 1722) and LLDP. Qt-free, so it is tested directly and reused by the GUI.

Both the classic libpcap format and pcapng (Wireshark's default) are read, for the Ethernet link type. The
stateless `decode()` dissects one frame; `StreamDecoder` decodes a sequence, reassembling SOME/IP carried over TCP
(across segments) and SOME/IP-TP (across datagrams) so a message split across packets is decoded in full.
"""

import socket
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from can_explorer.core import avtp_acf, doip, someip
from can_explorer.core import gptp as ptp  # Aliased so it does not clash with the EthernetPacket.gptp field
from can_explorer.core.protocols import nm

# EtherTypes
ETH_IPV4 = 0x0800
ETH_ARP = 0x0806
ETH_VLAN = 0x8100
ETH_IPV6 = 0x86DD
ETH_PTP = 0x88F7  # gPTP / IEEE 802.1AS
ETH_AVTP = 0x22F0  # IEEE 1722
ETH_LLDP = 0x88CC
ETH_MACSEC = 0x88E5  # IEEE 802.1AE (MACsec)
ETH_RTAG = 0xF1C1  # IEEE 802.1CB redundancy tag (FRER)
ETH_MVRP = 0x88F5  # Multiple VLAN Registration Protocol (SRP/TSN)
ETH_MMRP = 0x88F6  # Multiple MAC Registration Protocol
ETH_MSRP = 0x22EA  # Multiple Stream Reservation Protocol (SRP, IEEE 802.1Qat)

ETHERTYPE_NAMES = {
    ETH_ARP: 'ARP',
    ETH_PTP: 'gPTP',
    ETH_AVTP: 'AVTP',
    ETH_LLDP: 'LLDP',
    ETH_MACSEC: 'MACsec',
    ETH_MVRP: 'MVRP',
    ETH_MMRP: 'MMRP',
    ETH_MSRP: 'MSRP',
}

PTP_MESSAGE_TYPES = {
    0x0: 'Sync',
    0x1: 'Delay_Req',
    0x2: 'Pdelay_Req',
    0x3: 'Pdelay_Resp',
    0x8: 'Follow_Up',
    0x9: 'Delay_Resp',
    0xA: 'Pdelay_Resp_Follow_Up',
    0xB: 'Announce',
    0xC: 'Signaling',
    0xD: 'Management',
}

AVTP_SUBTYPES = {
    0x00: '61883/IIDC',
    0x02: 'AAF (audio)',
    0x03: 'CVF (compressed video)',
    0x04: 'CRF (clock reference)',
    0x05: 'TSCF (control)',
    0x6E: 'AEF (continuous)',
    0x82: 'NTSCF (control)',
    0xFB: 'AECP',
    0xFC: 'ACMP',
    0xFE: 'MAAP',
}

DOIP_PAYLOAD_TYPES = {
    0x0000: 'generic NACK',
    0x0001: 'vehicle identification request',
    0x0004: 'vehicle announcement',
    0x0005: 'routing activation request',
    0x0006: 'routing activation response',
    0x0007: 'alive check request',
    0x0008: 'alive check response',
    0x4001: 'entity status request',
    0x4002: 'entity status response',
    0x8001: 'diagnostic message',
    0x8002: 'diagnostic positive ack',
    0x8003: 'diagnostic negative ack',
}

IGMP_TYPES = {0x11: 'membership query', 0x12: 'v1 report', 0x16: 'v2 report', 0x17: 'leave', 0x22: 'v3 report'}
DNS_TYPES = {1: 'A', 2: 'NS', 5: 'CNAME', 12: 'PTR', 15: 'MX', 16: 'TXT', 28: 'AAAA', 33: 'SRV', 255: 'ANY'}
DHCP_PORTS = {67, 68}
DNS_PORT = 53
MDNS_PORT = 5353


class PcapError(ValueError):
    pass


@dataclass
class RawPacket:
    timestamp: float
    data: bytes


@dataclass
class AvtpStreamRef:
    """The stream identity of one AVTP stream frame (AAF/CVF/CRF/61883), for per-stream health tracking."""

    subtype: int
    stream_id: str  # 8-byte stream id, hex
    sequence: int  # 8-bit AVTP sequence number


@dataclass
class EthernetPacket:
    """A decoded Ethernet frame, flattened for a packet list."""

    number: int = 0
    timestamp: float = 0.0
    length: int = 0
    src_mac: str = ''
    dst_mac: str = ''
    vlan: Optional[int] = None
    l3: str = ''  # 'IPv4', 'IPv6', 'ARP', '' …
    src: str = ''  # L3 address (IP), or '' for non-IP
    dst: str = ''
    l4: str = ''  # 'UDP', 'TCP', 'ICMP', ''
    src_port: Optional[int] = None
    dst_port: Optional[int] = None
    protocol: str = ''  # The highest recognised layer: 'SOME/IP', 'DoIP', 'gPTP', 'AVTP', 'ARP', 'UDP', …
    info: str = ''
    gptp: Optional[ptp.GptpMessage] = None  # The parsed gPTP message, for the time-sync summary
    can_frames: List[avtp_acf.AcfCanFrame] = field(default_factory=list)  # CAN tunnelled in AVTP ACF (TSCF/NTSCF)
    avtp: Optional[AvtpStreamRef] = None  # The stream id/sequence of an AVTP stream frame, for stream health


# --- pcap file reading and writing (classic libpcap, Ethernet link type) ---

_PCAP_MAGICS = {
    0xA1B2C3D4: ('>', 1e-6),  # Big endian, microseconds
    0xD4C3B2A1: ('<', 1e-6),  # Little endian, microseconds
    0xA1B23C4D: ('>', 1e-9),  # Big endian, nanoseconds
    0x4D3CB24D: ('<', 1e-9),
    0x4D3CB2A1: ('<', 1e-9),  # Little endian, nanoseconds
}
LINKTYPE_ETHERNET = 1


_PCAPNG_MAGIC = b'\x0a\x0d\x0d\x0a'  # Section Header Block type (also the file's first bytes)


def read_pcap(path: str | Path) -> Iterator[RawPacket]:
    """Yield the packets of a pcap or pcapng file (Ethernet link type). Raises PcapError on an unsupported file."""
    data = Path(path).read_bytes()
    if len(data) < 24:
        raise PcapError('File too short to be a capture')
    if data[:4] == _PCAPNG_MAGIC:
        yield from _read_pcapng(data)
        return
    magic = struct.unpack('>I', data[:4])[0]
    if magic not in _PCAP_MAGICS:
        raise PcapError('Not a pcap file (bad magic number)')
    endian, resolution = _PCAP_MAGICS[magic]
    link_type = struct.unpack(endian + 'I', data[20:24])[0]
    if link_type != LINKTYPE_ETHERNET:
        raise PcapError(f'Unsupported link type {link_type} (only Ethernet is supported)')
    record = struct.Struct(endian + 'IIII')
    offset = 24
    while offset + record.size <= len(data):
        ts_sec, ts_frac, incl_len, _orig_len = record.unpack_from(data, offset)
        offset += record.size
        if offset + incl_len > len(data):
            break
        yield RawPacket(ts_sec + ts_frac * resolution, data[offset : offset + incl_len])
        offset += incl_len


def _pcapng_tsresol(options: bytes, endian: str) -> float:
    """The if_tsresol option (code 9) of an Interface Description Block: the seconds-per-timestamp-unit. Default
    1e-6 (microseconds)."""
    offset = 0
    while offset + 4 <= len(options):
        code, length = struct.unpack_from(endian + 'HH', options, offset)
        value = options[offset + 4 : offset + 4 + length]
        offset += 4 + length + (-length % 4)  # Options are padded to 32 bits
        if code == 0:  # opt_endofopt
            break
        if code == 9 and value:  # if_tsresol
            raw = value[0]
            return 2.0 ** -(raw & 0x7F) if raw & 0x80 else 10.0 ** -(raw & 0x7F)
    return 1e-6


def _read_pcapng(data: bytes) -> Iterator[RawPacket]:
    """Yield the Ethernet packets of a pcapng file. Interfaces that are not Ethernet are skipped; a file with no
    Ethernet interface raises PcapError."""
    offset = 0
    endian = '<'
    interfaces: List[float] = []  # tsresol per interface id, in the order their IDBs appear
    linktypes: List[int] = []
    saw_ethernet = False
    while offset + 12 <= len(data):
        block_type = struct.unpack_from(endian + 'I', data, offset)[0] if offset else 0x0A0D0D0A
        if offset == 0 or block_type == 0x0A0D0D0A:  # Section Header Block: sets the byte order for the section
            byte_order_magic = struct.unpack_from('<I', data, offset + 8)[0]
            endian = '<' if byte_order_magic == 0x1A2B3C4D else '>'
            interfaces, linktypes = [], []
        total_length = struct.unpack_from(endian + 'I', data, offset + 4)[0]
        if total_length < 12 or offset + total_length > len(data):
            break
        body = data[offset + 8 : offset + total_length - 4]
        block_type = struct.unpack_from(endian + 'I', data, offset)[0]
        if block_type == 0x00000001:  # Interface Description Block
            link_type = struct.unpack_from(endian + 'H', body, 0)[0]
            linktypes.append(link_type)
            interfaces.append(_pcapng_tsresol(body[8:], endian))
            saw_ethernet = saw_ethernet or link_type == LINKTYPE_ETHERNET
        elif block_type == 0x00000006 and len(body) >= 20:  # Enhanced Packet Block
            iface, ts_high, ts_low, incl_len = struct.unpack_from(endian + 'IIII', body, 0)
            if iface < len(linktypes) and linktypes[iface] == LINKTYPE_ETHERNET:
                resolution = interfaces[iface]
                timestamp = ((ts_high << 32) | ts_low) * resolution
                yield RawPacket(timestamp, bytes(body[20 : 20 + incl_len]))
        elif block_type == 0x00000003 and len(body) >= 4:  # Simple Packet Block (uses interface 0, no timestamp)
            if linktypes and linktypes[0] == LINKTYPE_ETHERNET:
                orig_len = struct.unpack_from(endian + 'I', body, 0)[0]
                yield RawPacket(0.0, bytes(body[4 : 4 + orig_len]))
        offset += total_length
    if not saw_ethernet:
        raise PcapError('No Ethernet interface in the pcapng file (only Ethernet is supported)')


def write_pcap(path: str | Path, packets: Sequence[RawPacket]) -> None:
    """Write packets to a classic pcap file (little endian, microseconds, Ethernet)."""
    with Path(path).open('wb') as file:
        file.write(struct.pack('<IHHIIII', 0xA1B2C3D4, 2, 4, 0, 0, 262144, LINKTYPE_ETHERNET))
        for packet in packets:
            seconds = int(packet.timestamp)
            micros = int(round((packet.timestamp - seconds) * 1_000_000))
            file.write(struct.pack('<IIII', seconds, micros, len(packet.data), len(packet.data)))
            file.write(packet.data)


# --- decoding ---


def _mac(data: bytes) -> str:
    return ':'.join(f'{byte:02x}' for byte in data)


def decode(data: bytes, timestamp: float = 0.0, number: int = 0, nm_port: Optional[int] = None) -> EthernetPacket:
    """Decode one Ethernet frame into an EthernetPacket (never raises; unknown layers are left blank). When
    `nm_port` is given, UDP frames on that port are decoded as AUTOSAR UDP-NM (which has no standard port)."""
    packet = EthernetPacket(number=number, timestamp=timestamp, length=len(data))
    if len(data) < 14:
        packet.protocol = 'short'
        return packet
    packet.dst_mac, packet.src_mac = _mac(data[0:6]), _mac(data[6:12])
    ethertype = struct.unpack('>H', data[12:14])[0]
    offset = 14
    rtag_sequence = None
    # Peel 802.1Q VLAN tags and the 802.1CB redundancy tag (FRER) to reach the real EtherType
    while True:
        if ethertype == ETH_VLAN and len(data) >= offset + 4:
            packet.vlan = struct.unpack('>H', data[offset : offset + 2])[0] & 0x0FFF
            ethertype = struct.unpack('>H', data[offset + 2 : offset + 4])[0]
            offset += 4
        elif ethertype == ETH_RTAG and len(data) >= offset + 6:
            rtag_sequence = struct.unpack('>H', data[offset + 2 : offset + 4])[0]  # reserved(2) + sequence(2)
            ethertype = struct.unpack('>H', data[offset + 4 : offset + 6])[0]
            offset += 6
        else:
            break
    try:
        _decode_l3(packet, ethertype, data, offset, nm_port)
    except (struct.error, IndexError, ValueError):
        pass
    if rtag_sequence is not None:
        packet.info = f'R-TAG {rtag_sequence}' + (f' · {packet.info}' if packet.info else '')
    if not packet.protocol:
        packet.protocol = packet.l4 or packet.l3 or ETHERTYPE_NAMES.get(ethertype, f'0x{ethertype:04X}')
    return packet


def _decode_l3(packet: EthernetPacket, ethertype: int, data: bytes, offset: int, nm_port: Optional[int] = None) -> None:
    if ethertype == ETH_PTP:
        packet.l3 = packet.protocol = 'gPTP'
        message = ptp.parse(data[offset:])
        if message is not None:
            message.timestamp = packet.timestamp
            packet.gptp = message
            packet.info = ptp.describe(message)
        else:
            message_type = data[offset] & 0x0F if len(data) > offset else 0
            packet.info = PTP_MESSAGE_TYPES.get(message_type, f'type {message_type}')
        return
    if ethertype == ETH_AVTP:
        packet.l3 = packet.protocol = 'AVTP'
        _decode_avtp(packet, data[offset:])
        return
    if ethertype == ETH_LLDP:
        packet.l3 = packet.protocol = 'LLDP'
        packet.info = _lldp_info(data[offset:])
        return
    if ethertype == ETH_MACSEC:
        packet.l3 = packet.protocol = 'MACsec'
        if len(data) >= offset + 6:
            tci = data[offset]  # V, ES, SC, SCB, E, C, then 2-bit association number
            packet_number = struct.unpack('>I', data[offset + 2 : offset + 6])[0]
            encrypted = ' encrypted' if tci & 0x08 else ''  # E bit
            packet.info = f'AN {tci & 0x03}, PN {packet_number}{encrypted}'
        return
    if ethertype in (ETH_MVRP, ETH_MMRP, ETH_MSRP):  # IEEE 802.1 (M)SRP / TSN stream reservation control
        packet.l3 = packet.protocol = ETHERTYPE_NAMES[ethertype]
        packet.info = 'stream reservation' if ethertype == ETH_MSRP else 'registration protocol'
        return
    if ethertype == ETH_ARP:
        packet.l3 = packet.protocol = 'ARP'
        if len(data) >= offset + 28:
            opcode = struct.unpack('>H', data[offset + 6 : offset + 8])[0]
            sender_ip = socket.inet_ntoa(data[offset + 14 : offset + 18])
            target_ip = socket.inet_ntoa(data[offset + 24 : offset + 28])
            packet.src, packet.dst = sender_ip, target_ip
            packet.info = (
                f'who has {target_ip}? tell {sender_ip}'
                if opcode == 1
                else f'{sender_ip} is at {_mac(data[offset + 8 : offset + 14])}'
            )
        return
    if ethertype == ETH_IPV4:
        _decode_ipv4(packet, data, offset, nm_port)
    elif ethertype == ETH_IPV6:
        _decode_ipv6(packet, data, offset, nm_port)


def _decode_ipv4(packet: EthernetPacket, data: bytes, offset: int, nm_port: Optional[int] = None) -> None:
    if len(data) < offset + 20:
        return
    packet.l3 = 'IPv4'
    ihl = (data[offset] & 0x0F) * 4
    protocol = data[offset + 9]
    packet.src = socket.inet_ntoa(data[offset + 12 : offset + 16])
    packet.dst = socket.inet_ntoa(data[offset + 16 : offset + 20])
    _decode_l4(packet, protocol, data, offset + ihl, nm_port)


def _decode_ipv6(packet: EthernetPacket, data: bytes, offset: int, nm_port: Optional[int] = None) -> None:
    if len(data) < offset + 40:
        return
    packet.l3 = 'IPv6'
    next_header = data[offset + 6]
    packet.src = socket.inet_ntop(socket.AF_INET6, data[offset + 8 : offset + 24])
    packet.dst = socket.inet_ntop(socket.AF_INET6, data[offset + 24 : offset + 40])
    _decode_l4(packet, next_header, data, offset + 40, nm_port)


def _decode_l4(packet: EthernetPacket, protocol: int, data: bytes, offset: int, nm_port: Optional[int] = None) -> None:
    if protocol == 17 and len(data) >= offset + 8:  # UDP
        packet.l4 = 'UDP'
        packet.src_port, packet.dst_port = struct.unpack('>HH', data[offset : offset + 4])
        _decode_application(packet, data[offset + 8 :], nm_port)
    elif protocol == 6 and len(data) >= offset + 20:  # TCP
        packet.l4 = 'TCP'
        packet.src_port, packet.dst_port = struct.unpack('>HH', data[offset : offset + 4])
        data_offset = (data[offset + 12] >> 4) * 4
        _decode_application(packet, data[offset + data_offset :], nm_port)
    elif protocol == 1:
        packet.l4 = packet.protocol = 'ICMP'
    elif protocol == 58:  # ICMPv6; the multicast-listener (MLD) types are the IPv6 equivalent of IGMP
        icmp_type = data[offset] if len(data) > offset else 0
        packet.l4 = packet.protocol = 'MLD' if icmp_type in (130, 131, 132, 143) else 'ICMPv6'
    elif protocol == 2:  # IGMP (IPv4 multicast group membership)
        packet.l4 = packet.protocol = 'IGMP'
        packet.info = IGMP_TYPES.get(data[offset] if len(data) > offset else 0, '')
    elif protocol in (50, 51) and len(data) >= offset + 8:  # IPsec ESP / AH
        packet.l4 = packet.protocol = 'IPsec ESP' if protocol == 50 else 'IPsec AH'
        spi = struct.unpack('>I', data[offset : offset + 4] if protocol == 50 else data[offset + 4 : offset + 8])[0]
        packet.info = f'SPI 0x{spi:08X}'


def _decode_application(packet: EthernetPacket, payload: bytes, nm_port: Optional[int] = None) -> None:
    ports = {packet.src_port, packet.dst_port}
    if nm_port is not None and nm_port in ports:  # AUTOSAR UDP-NM: no standard port, so it is user-configured
        summary = nm.describe(payload)
        if summary is not None:
            packet.protocol, packet.info = 'UDP-NM', summary
            return
    if doip.PORT in ports or doip.TLS_PORT in ports:
        try:
            _version, payload_type, _body = doip.decode_message(payload)
            packet.protocol = 'DoIP'
            packet.info = DOIP_PAYLOAD_TYPES.get(payload_type, f'payload type 0x{payload_type:04X}')
            return
        except (doip.DoipError, ValueError, IndexError):
            pass
    if someip.DEFAULT_SD_PORT in ports or _looks_like_someip(payload):
        messages = someip.parse_messages(payload)
        if messages:
            packet.protocol = 'SOME/IP'
            packet.info = ' | '.join(someip.describe(message) for message in messages[:3])
            return
    if ports & DHCP_PORTS and len(payload) >= 1:
        packet.protocol = 'DHCP'
        packet.info = {1: 'request', 2: 'reply'}.get(payload[0], '')
        return
    if DNS_PORT in ports or MDNS_PORT in ports:
        packet.protocol = 'mDNS' if MDNS_PORT in ports else 'DNS'
        packet.info = _dns_summary(payload)
        return


def _dns_summary(payload: bytes) -> str:
    """The first question of a DNS/mDNS message, e.g. 'query A host.local' or 'response'."""
    if len(payload) < 12:
        return ''
    flags, questions = struct.unpack('>H', payload[2:4])[0], struct.unpack('>H', payload[4:6])[0]
    kind = 'response' if flags & 0x8000 else 'query'
    if questions == 0:
        return kind
    name, position = _dns_name(payload, 12)
    if position + 2 > len(payload):
        return kind
    record_type = struct.unpack('>H', payload[position : position + 2])[0]
    return f'{kind} {DNS_TYPES.get(record_type, str(record_type))} {name}'.rstrip()


def _dns_name(payload: bytes, position: int) -> Tuple[str, int]:
    """A DNS name read from `position` (no compression, as in a question); returns (name, position after it)."""
    labels = []
    while position < len(payload):
        length = payload[position]
        if length == 0:
            return '.'.join(labels), position + 1
        if length & 0xC0:  # A compression pointer ends the name for our purposes
            return '.'.join(labels), position + 2
        labels.append(payload[position + 1 : position + 1 + length].decode('ascii', 'replace'))
        position += 1 + length
    return '.'.join(labels), position


_AVTP_STREAM_SUBTYPES = {0x00, 0x02, 0x03, 0x04}  # 61883/IIDC, AAF, CVF, CRF carry the common stream header
_AVTP_MAAP_SUBTYPE = 0xFE
MAAP_MESSAGE_TYPES = {1: 'probe', 2: 'defend', 3: 'announce'}  # IEEE 1722 MAAP dynamic address allocation


def _decode_avtp(packet: EthernetPacket, payload: bytes) -> None:
    """Fill in an AVTP (IEEE 1722) packet: the info summary, and for TSCF/NTSCF control frames the fieldbus frames
    tunnelled in their ACF messages. CAN frames are attached to the packet so the CAN views can use them."""
    packet.info = _avtp_info(payload)
    if payload and payload[0] in _AVTP_STREAM_SUBTYPES and len(payload) >= 12:
        packet.avtp = AvtpStreamRef(subtype=payload[0], stream_id=payload[4:12].hex(), sequence=payload[2])
    if not (payload and avtp_acf.is_control_subtype(payload[0])):
        return
    contents = avtp_acf.decode_acf(payload)
    if contents is None or not contents:
        return
    packet.can_frames = contents.can
    packet.protocol = _acf_protocol(contents)
    packet.info = f'{packet.info}: {_acf_summary(contents)}'


def _acf_protocol(contents) -> str:
    """The protocol label for a control frame: the specific bus when it carries only one, else generic ACF."""
    kinds = [bool(contents.can), bool(contents.lin), bool(contents.flexray), bool(contents.other_types)]
    if sum(kinds) == 1:
        return (
            'ACF-CAN'
            if contents.can
            else 'ACF-LIN'
            if contents.lin
            else 'ACF-FlexRay'
            if contents.flexray
            else 'AVTP ACF'
        )
    return 'AVTP ACF'


def _acf_summary(contents) -> str:
    """A short description of the frames tunnelled in one control frame."""
    parts = []
    for frame in contents.can[:4]:
        identifier = f'{frame.can_id:08X}' if frame.extended else f'{frame.can_id:03X}'
        kind = ' RTR' if frame.remote else (' FD' if frame.fd else '')
        parts.append(f'CAN bus {frame.bus_id} {identifier}#{frame.data.hex().upper()}{kind}')
    for frame in contents.lin[:4]:
        parts.append(f'LIN bus {frame.bus_id} id {frame.frame_id:02X}#{frame.data.hex().upper()}')
    for frame in contents.flexray[:4]:
        parts.append(f'FlexRay bus {frame.bus_id} id {frame.frame_id} cyc {frame.cycle}#{frame.data.hex().upper()}')
    parts += [f'ACF {avtp_acf.acf_type_name(acf_type)}' for acf_type in contents.other_types[:4]]
    return ', '.join(parts)


def _avtp_info(payload: bytes) -> str:
    """Summarise an AVTP (IEEE 1722) frame: the subtype, and for stream formats the stream id and sequence number
    (so lost stream packets show up as sequence gaps)."""
    if not payload:
        return ''
    subtype = payload[0]
    name = AVTP_SUBTYPES.get(subtype, f'subtype 0x{subtype:02X}')
    if subtype in _AVTP_STREAM_SUBTYPES and len(payload) >= 12:
        summary = f'{name}, stream {payload[4:12].hex()}, seq {payload[2]}'
        detail = avtp_acf.describe_stream(payload)
        return f'{summary} ({detail})' if detail else summary
    if subtype == _AVTP_MAAP_SUBTYPE and len(payload) >= 2:
        message_type = MAAP_MESSAGE_TYPES.get(payload[1] & 0x0F)
        return f'{name} {message_type}' if message_type else name
    return name


def _lldp_identifier(value: bytes) -> str:
    """An LLDP chassis/port id TLV value (a subtype byte then the id) as a MAC, text or hex."""
    subtype, body = value[0], value[1:]
    if subtype in (3, 4) and len(body) == 6:  # MAC address
        return _mac(body)
    text = body.decode('ascii', 'replace')
    return text if text.isprintable() else body.hex()


def _lldp_info(payload: bytes) -> str:
    """Summarise an LLDP frame from its mandatory TLVs (chassis id, port id, TTL) and the system name."""
    chassis = port = system = ''
    ttl: Optional[int] = None
    offset = 0
    while offset + 2 <= len(payload):
        header = int.from_bytes(payload[offset : offset + 2], 'big')
        tlv_type, length = header >> 9, header & 0x1FF
        value = payload[offset + 2 : offset + 2 + length]
        offset += 2 + length
        if tlv_type == 0:  # End of LLDPDU
            break
        if tlv_type == 1 and value:  # Chassis id
            chassis = _lldp_identifier(value)
        elif tlv_type == 2 and value:  # Port id
            port = _lldp_identifier(value)
        elif tlv_type == 3 and len(value) >= 2:  # Time to live
            ttl = int.from_bytes(value[:2], 'big')
        elif tlv_type == 5:  # System name
            system = value.decode('ascii', 'replace')
    parts = [part for part in (system, f'chassis {chassis}' if chassis else '', f'port {port}' if port else '') if part]
    if ttl is not None:
        parts.append(f'TTL {ttl}s')
    return 'LLDP: ' + (', '.join(parts) if parts else 'neighbor')


def _looks_like_someip(payload: bytes) -> bool:
    """A conservative check that a self-framed (UDP) payload begins with a plausible SOME/IP message: protocol
    version 1 and a length field consistent with the data present."""
    if len(payload) < someip.HEADER.size:
        return False
    _message_id, length, _request_id, protocol_version = struct.unpack_from('>IIIB', payload)
    return protocol_version == 0x01 and someip.HEADER_COVERED <= length <= len(payload)


_MAX_SOMEIP_LENGTH = 64 * 1024 * 1024


def _starts_like_someip(data: bytes) -> bool:
    """Like `_looks_like_someip`, but for a TCP stream where the rest of the message is still to come: the length
    field need only be sane, not already satisfied by the bytes in hand."""
    if len(data) < someip.HEADER.size:
        return False
    _message_id, length, _request_id, protocol_version = struct.unpack_from('>IIIB', data)
    return protocol_version == 0x01 and someip.HEADER_COVERED <= length <= _MAX_SOMEIP_LENGTH


# --- stream reassembly (SOME/IP over TCP and SOME/IP-TP) ---


def _l4_stream(data: bytes) -> Optional[Tuple[str, Tuple, Optional[int], bytes]]:
    """(l4, flow, seq, payload) for a TCP/UDP frame, or None. `flow` identifies one direction of a TCP connection;
    `seq` is the TCP sequence number (None for UDP). The payload is bounded by the IP length, not the frame, so
    Ethernet padding is excluded."""
    if len(data) < 14:
        return None
    ethertype = int.from_bytes(data[12:14], 'big')
    offset = 14
    if ethertype == ETH_VLAN and len(data) >= 18:
        ethertype = int.from_bytes(data[16:18], 'big')
        offset = 18
    if ethertype == ETH_IPV4 and len(data) >= offset + 20:
        ihl = (data[offset] & 0x0F) * 4
        protocol = data[offset + 9]
        src, dst = data[offset + 12 : offset + 16], data[offset + 16 : offset + 20]
        ip_end = offset + int.from_bytes(data[offset + 2 : offset + 4], 'big')
        l4 = offset + ihl
    elif ethertype == ETH_IPV6 and len(data) >= offset + 40:
        protocol = data[offset + 6]
        src, dst = data[offset + 8 : offset + 24], data[offset + 24 : offset + 40]
        ip_end = offset + 40 + int.from_bytes(data[offset + 4 : offset + 6], 'big')
        l4 = offset + 40
    else:
        return None
    ip_end = min(ip_end, len(data))
    if protocol == 17 and l4 + 8 <= len(data):  # UDP
        src_port, dst_port = struct.unpack_from('>HH', data, l4)
        end = min(l4 + int.from_bytes(data[l4 + 4 : l4 + 6], 'big'), ip_end)
        return ('UDP', (src, src_port, dst, dst_port), None, data[l4 + 8 : end])
    if protocol == 6 and l4 + 20 <= len(data):  # TCP
        src_port, dst_port = struct.unpack_from('>HH', data, l4)
        seq = struct.unpack_from('>I', data, l4 + 4)[0]
        start = l4 + (data[l4 + 12] >> 4) * 4
        return ('TCP', (src, src_port, dst, dst_port), seq, data[start:ip_end] if start <= ip_end else b'')
    return None


class _TcpStream:
    """One direction of a TCP connection, reassembled into a byte stream for SOME/IP framing. Sequence numbers are
    treated as plain integers (a 4 GiB wrap within one flow is not handled)."""

    MAX_BUFFER = 8 * 1024 * 1024

    def __init__(self) -> None:
        self.next_seq: Optional[int] = None
        self.buffer = bytearray()
        self.pending: Dict[int, bytes] = {}  # Out-of-order segments, by sequence number
        self.is_someip: Optional[bool] = None  # Decided once the first header's worth of bytes is contiguous

    def add(self, seq: int, payload: bytes) -> None:
        if not payload:
            return
        if self.next_seq is None:
            self.next_seq = seq
        if seq == self.next_seq:
            self.buffer += payload
            self.next_seq += len(payload)
        elif seq < self.next_seq:  # Overlap or retransmission: keep only bytes past next_seq
            tail = payload[self.next_seq - seq :]
            self.buffer += tail
            self.next_seq += len(tail)
        elif len(self.pending) < 64:  # Gap (out of order or loss): stash for later, bounded
            self.pending[seq] = payload
        while self.next_seq in self.pending:
            segment = self.pending.pop(self.next_seq)
            self.buffer += segment
            self.next_seq += len(segment)
        if len(self.buffer) > self.MAX_BUFFER:
            del self.buffer[: -self.MAX_BUFFER]


class StreamDecoder:
    """Decodes a sequence of frames like `decode()`, but reassembles SOME/IP carried over TCP (across segments) and
    SOME/IP-TP (across datagrams), so a message split across packets is decoded on the packet that completes it and
    partial packets are marked as segments. Feed frames in capture order; it also tracks the SOME/IP services
    announced by Service Discovery (`services`)."""

    def __init__(self, nm_port: Optional[int] = None) -> None:
        self._tcp: Dict[Tuple, _TcpStream] = {}
        self._tp = someip.SomeIpReassembler()
        self.services: Dict[Tuple[int, int], someip.Service] = {}
        self.nm_port = nm_port

    def decode(self, data: bytes, timestamp: float = 0.0, number: int = 0) -> EthernetPacket:
        packet = decode(data, timestamp, number, self.nm_port)
        stream = _l4_stream(data)
        if stream is not None:
            try:
                self._reassemble(packet, *stream)
            except (struct.error, IndexError, ValueError):
                pass
        return packet

    def _reassemble(self, packet: EthernetPacket, l4: str, flow: Tuple, seq: Optional[int], payload: bytes) -> None:
        if l4 == 'UDP':
            if payload and (
                someip.DEFAULT_SD_PORT in {packet.src_port, packet.dst_port} or _looks_like_someip(payload)
            ):
                self._surface(packet, someip.parse_messages(payload))  # A datagram is self-framed
            return
        stream = self._tcp.get(flow)
        if stream is None:
            stream = self._tcp[flow] = _TcpStream()
        if stream.is_someip is False:
            return
        stream.add(seq, payload)
        if stream.is_someip is None:
            if len(stream.buffer) < someip.HEADER.size:
                return  # Wait for a header's worth to decide whether this flow carries SOME/IP
            stream.is_someip = _starts_like_someip(bytes(stream.buffer))
            if not stream.is_someip:
                stream.buffer = bytearray()  # Not SOME/IP: stop buffering this flow
                return
        messages, consumed = someip.parse_stream(bytes(stream.buffer))
        del stream.buffer[:consumed]
        if messages:
            self._surface(packet, messages)
        elif payload:
            packet.protocol, packet.info = 'SOME/IP', '[SOME/IP segment]'

    def _surface(self, packet: EthernetPacket, messages: List[someip.SomeIpMessage]) -> None:
        if not messages:
            return
        completed = []
        for message in messages:
            reassembled = self._tp.feed(message)  # SOME/IP-TP: returns None until the last segment
            if reassembled is None:
                continue
            completed.append(reassembled)
            if reassembled.is_sd:
                someip.update_services(self.services, reassembled)
        packet.protocol = 'SOME/IP'
        if completed:
            packet.info = ' | '.join(someip.describe(message) for message in completed[:3])
        else:
            packet.info = '[SOME/IP-TP segment]'


# --- AVTP stream health (per-stream sequence-gap loss, from the 8-bit AVTP sequence number) ---


@dataclass
class AvtpStreamStat:
    """Health of one AVTP stream across a capture, from its sequence numbers."""

    stream_id: str
    subtype: int
    subtype_name: str
    packets: int
    lost: int  # Frames missing, inferred from gaps in the 8-bit sequence number
    duplicates: int  # Repeated sequence numbers
    reordered: int  # Frames arriving with an earlier sequence number (late/out of order)
    first_timestamp: float
    last_timestamp: float

    @property
    def expected(self) -> int:
        """How many frames should have been seen (those received plus those inferred lost)."""
        return self.packets + self.lost

    @property
    def loss_percent(self) -> float:
        return 100.0 * self.lost / self.expected if self.expected else 0.0

    @property
    def rate_hz(self) -> float:
        duration = self.last_timestamp - self.first_timestamp
        return (self.packets - 1) / duration if duration > 0 else 0.0


class _AvtpStreamAccumulator:
    def __init__(self, ref: AvtpStreamRef, timestamp: float):
        self.subtype = ref.subtype
        self.packets = 0
        self.lost = 0
        self.duplicates = 0
        self.reordered = 0
        self.last_seq: Optional[int] = None
        self.first_timestamp = timestamp
        self.last_timestamp = timestamp

    def add(self, sequence: int, timestamp: float) -> None:
        self.packets += 1
        self.last_timestamp = timestamp
        if self.last_seq is not None:
            delta = (sequence - self.last_seq) & 0xFF
            if delta == 0:
                self.duplicates += 1
            elif delta <= 128:  # A forward step: delta-1 frames were skipped (0 when consecutive)
                self.lost += delta - 1
            else:  # A large backward step: a late / reordered frame, not a loss
                self.reordered += 1
                return  # Keep the highest sequence as the reference
        self.last_seq = sequence


def avtp_stream_stats(packets: Sequence['EthernetPacket']) -> List[AvtpStreamStat]:
    """Per-stream AVTP health from a decoded capture: packets, inferred lost frames (gaps in the 8-bit sequence
    number), duplicates, reordered frames and the average rate. Streams are keyed by stream id and subtype."""
    accumulators: Dict[Tuple[str, int], _AvtpStreamAccumulator] = {}
    for packet in packets:
        if packet.avtp is None:
            continue
        key = (packet.avtp.stream_id, packet.avtp.subtype)
        accumulator = accumulators.get(key)
        if accumulator is None:
            accumulator = accumulators[key] = _AvtpStreamAccumulator(packet.avtp, packet.timestamp)
        accumulator.add(packet.avtp.sequence, packet.timestamp)
    stats = [
        AvtpStreamStat(
            stream_id=stream_id,
            subtype=subtype,
            subtype_name=AVTP_SUBTYPES.get(subtype, f'0x{subtype:02X}'),
            packets=accumulator.packets,
            lost=accumulator.lost,
            duplicates=accumulator.duplicates,
            reordered=accumulator.reordered,
            first_timestamp=accumulator.first_timestamp,
            last_timestamp=accumulator.last_timestamp,
        )
        for (stream_id, subtype), accumulator in accumulators.items()
    ]
    return sorted(stats, key=lambda stat: (-stat.lost, stat.stream_id))


# --- per-packet field dissection (a Wireshark-style tree for the details pane) ---

_IP_PROTO_NAMES = {1: 'ICMP', 2: 'IGMP', 6: 'TCP', 17: 'UDP', 50: 'IPsec ESP', 51: 'IPsec AH', 58: 'ICMPv6'}


@dataclass
class DissectionField:
    """One node in a packet's dissection tree: a labelled field, its rendered value, and the byte span it covers in
    the frame (offset/length; both 0 when it maps to no single range). `children` holds the sub-fields."""

    label: str
    value: str = ''
    offset: int = 0
    length: int = 0
    children: List['DissectionField'] = field(default_factory=list)

    def add(self, label: str, value: str = '', offset: int = 0, length: int = 0) -> 'DissectionField':
        child = DissectionField(label, value, offset, length)
        self.children.append(child)
        return child


def dissect(data: bytes, timestamp: float = 0.0, number: int = 0, nm_port: Optional[int] = None) -> DissectionField:
    """Dissect one Ethernet frame into a tree of fields, layer by layer (Ethernet, VLAN/R-TAG, IP, transport and
    the recognised application protocol). Never raises; dissection stops at the first layer that will not parse."""
    packet = decode(data, timestamp, number, nm_port)
    summary = f'{len(data)} bytes, {packet.protocol}' if packet.protocol else f'{len(data)} bytes'
    root = DissectionField('Frame', summary, 0, len(data))
    try:
        _dissect_ethernet(root, packet, data, nm_port)
    except (struct.error, IndexError, ValueError):
        pass
    return root


def _dissect_ethernet(root: DissectionField, packet: EthernetPacket, data: bytes, nm_port: Optional[int]) -> None:
    if len(data) < 14:
        return
    eth = root.add('Ethernet II', f'{packet.src_mac} → {packet.dst_mac}', 0, 14)
    eth.add('Destination', packet.dst_mac, 0, 6)
    eth.add('Source', packet.src_mac, 6, 6)
    ethertype = struct.unpack('>H', data[12:14])[0]
    offset = 14
    while True:
        if ethertype == ETH_VLAN and len(data) >= offset + 4:
            tci = struct.unpack('>H', data[offset : offset + 2])[0]
            vlan = root.add('802.1Q VLAN', f'id {tci & 0x0FFF}', offset, 4)
            vlan.add('Priority (PCP)', str((tci >> 13) & 0x07), offset, 2)
            vlan.add('VLAN id', str(tci & 0x0FFF), offset, 2)
            ethertype = struct.unpack('>H', data[offset + 2 : offset + 4])[0]
            offset += 4
        elif ethertype == ETH_RTAG and len(data) >= offset + 6:
            sequence = struct.unpack('>H', data[offset + 2 : offset + 4])[0]
            root.add('802.1CB R-TAG (FRER)', f'seq {sequence}', offset, 6)
            ethertype = struct.unpack('>H', data[offset + 4 : offset + 6])[0]
            offset += 6
        else:
            break
    eth.add('EtherType', f'0x{ethertype:04X} ({ETHERTYPE_NAMES.get(ethertype, packet.l3 or "?")})', 12, 2)
    _dissect_l3(root, packet, ethertype, data, offset, nm_port)


def _dissect_l3(
    root: DissectionField, packet: EthernetPacket, ethertype: int, data: bytes, offset: int, nm_port: Optional[int]
) -> None:
    if ethertype == ETH_ARP and len(data) >= offset + 28:
        arp = root.add('ARP', packet.info, offset, 28)
        opcode = struct.unpack('>H', data[offset + 6 : offset + 8])[0]
        arp.add('Opcode', {1: 'request', 2: 'reply'}.get(opcode, str(opcode)), offset + 6, 2)
        arp.add('Sender MAC', _mac(data[offset + 8 : offset + 14]), offset + 8, 6)
        arp.add('Sender IP', socket.inet_ntoa(data[offset + 14 : offset + 18]), offset + 14, 4)
        arp.add('Target MAC', _mac(data[offset + 18 : offset + 24]), offset + 18, 6)
        arp.add('Target IP', socket.inet_ntoa(data[offset + 24 : offset + 28]), offset + 24, 4)
        return
    if ethertype == ETH_IPV4 and len(data) >= offset + 20:
        ihl = (data[offset] & 0x0F) * 4
        protocol = data[offset + 9]
        node = root.add('IPv4', f'{packet.src} → {packet.dst}', offset, ihl)
        node.add('Version / header length', f'4 / {ihl} bytes', offset, 1)
        node.add('TTL', str(data[offset + 8]), offset + 8, 1)
        node.add('Protocol', f'{protocol} ({_IP_PROTO_NAMES.get(protocol, "?")})', offset + 9, 1)
        node.add('Source', packet.src, offset + 12, 4)
        node.add('Destination', packet.dst, offset + 16, 4)
        _dissect_l4(root, packet, protocol, data, offset + ihl, nm_port)
        return
    if ethertype == ETH_IPV6 and len(data) >= offset + 40:
        next_header = data[offset + 6]
        node = root.add('IPv6', f'{packet.src} → {packet.dst}', offset, 40)
        node.add('Next header', f'{next_header} ({_IP_PROTO_NAMES.get(next_header, "?")})', offset + 6, 1)
        node.add('Hop limit', str(data[offset + 7]), offset + 7, 1)
        node.add('Source', packet.src, offset + 8, 16)
        node.add('Destination', packet.dst, offset + 24, 16)
        _dissect_l4(root, packet, next_header, data, offset + 40, nm_port)
        return
    if packet.l3:  # Non-IP upper layer: gPTP, AVTP, LLDP, MACsec, (M)SRP, …
        leaf = root.add(packet.l3, packet.info, offset, len(data) - offset)
        _dissect_frames(leaf, packet)
        if packet.gptp is not None and len(data) > offset:
            leaf.add('Message type', PTP_MESSAGE_TYPES.get(data[offset] & 0x0F, '?'), offset, 1)


def _dissect_l4(
    root: DissectionField, packet: EthernetPacket, protocol: int, data: bytes, offset: int, nm_port: Optional[int]
) -> None:
    if protocol == 17 and len(data) >= offset + 8:  # UDP
        node = root.add('UDP', f'{packet.src_port} → {packet.dst_port}', offset, 8)
        node.add('Source port', str(packet.src_port), offset, 2)
        node.add('Destination port', str(packet.dst_port), offset + 2, 2)
        length = struct.unpack('>H', data[offset + 4 : offset + 6])[0]
        node.add('Length', str(length), offset + 4, 2)
        end = min(offset + length, len(data)) if length >= 8 else len(data)
        _dissect_app(root, packet, data[offset + 8 : end], nm_port)
        return
    if protocol == 6 and len(data) >= offset + 20:  # TCP
        data_offset = (data[offset + 12] >> 4) * 4
        node = root.add('TCP', f'{packet.src_port} → {packet.dst_port}', offset, data_offset)
        node.add('Source port', str(packet.src_port), offset, 2)
        node.add('Destination port', str(packet.dst_port), offset + 2, 2)
        node.add('Sequence number', str(struct.unpack('>I', data[offset + 4 : offset + 8])[0]), offset + 4, 4)
        _dissect_app(root, packet, data[offset + data_offset :], nm_port)
        return
    if packet.l4:  # ICMP, ICMPv6, IGMP, MLD, IPsec ESP/AH — summarised, no sub-fields
        root.add(packet.l4, packet.info, offset, max(0, len(data) - offset))


def _dissect_app(root: DissectionField, packet: EthernetPacket, payload: bytes, nm_port: Optional[int]) -> None:
    protocol = packet.protocol
    if not protocol or protocol in ('UDP', 'TCP'):
        if payload:
            root.add('Data', f'{len(payload)} bytes', 0, len(payload))
        return
    app = root.add(protocol, packet.info)
    if protocol == 'SOME/IP':
        for message in someip.parse_messages(payload)[:8]:
            app.add('Message', someip.describe(message))
    else:
        _dissect_frames(app, packet)


def _dissect_frames(node: DissectionField, packet: EthernetPacket) -> None:
    """List the CAN frames tunnelled in an AVTP ACF control frame, when present."""
    for frame in packet.can_frames[:16]:
        identifier = f'{frame.can_id:08X}' if frame.extended else f'{frame.can_id:03X}'
        node.add(f'CAN bus {frame.bus_id}', f'{identifier}#{frame.data.hex().upper()}')


def interfaces() -> List[str]:
    """The network interface names on this machine (for live capture)."""
    try:
        return [name for _index, name in socket.if_nameindex()]
    except (OSError, AttributeError):
        return []
