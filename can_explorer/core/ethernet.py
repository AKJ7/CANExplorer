"""Automotive Ethernet capture and decoding: read/write pcap files and decode frames down the stack (Ethernet,
VLAN, IPv4/IPv6, ARP, UDP/TCP) and the automotive protocols on top — SOME/IP (reusing someip.py), DoIP, gPTP
(IEEE 802.1AS) and AVTP (IEEE 1722). Qt-free, so it is tested directly and reused by the GUI.

pcap is the classic libpcap format (Ethernet link type); pcapng is detected and rejected with a clear message.
"""

import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from can_explorer.core import doip, someip

# EtherTypes
ETH_IPV4 = 0x0800
ETH_ARP = 0x0806
ETH_VLAN = 0x8100
ETH_IPV6 = 0x86DD
ETH_PTP = 0x88F7  # gPTP / IEEE 802.1AS
ETH_AVTP = 0x22F0  # IEEE 1722
ETH_LLDP = 0x88CC

ETHERTYPE_NAMES = {ETH_ARP: 'ARP', ETH_PTP: 'gPTP', ETH_AVTP: 'AVTP', ETH_LLDP: 'LLDP'}

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
    0x6E: 'NTSCF',
    0x82: 'ADP (discovery)',
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


class PcapError(ValueError):
    pass


@dataclass
class RawPacket:
    timestamp: float
    data: bytes


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


# --- pcap file reading and writing (classic libpcap, Ethernet link type) ---

_PCAP_MAGICS = {
    0xA1B2C3D4: ('>', 1e-6),  # Big endian, microseconds
    0xD4C3B2A1: ('<', 1e-6),  # Little endian, microseconds
    0xA1B23C4D: ('>', 1e-9),  # Big endian, nanoseconds
    0x4D3CB24D: ('<', 1e-9),
    0x4D3CB2A1: ('<', 1e-9),  # Little endian, nanoseconds
}
LINKTYPE_ETHERNET = 1


def read_pcap(path: str | Path) -> Iterator[RawPacket]:
    """Yield the packets of a classic pcap file (Ethernet). Raises PcapError on an unsupported file."""
    data = Path(path).read_bytes()
    if len(data) < 24:
        raise PcapError('File too short to be a pcap')
    if data[:4] == b'\x0a\x0d\x0d\x0a':
        raise PcapError('This is a pcapng file; save it as "Wireshark/tcpdump pcap" and open that')
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


def decode(data: bytes, timestamp: float = 0.0, number: int = 0) -> EthernetPacket:
    """Decode one Ethernet frame into an EthernetPacket (never raises; unknown layers are left blank)."""
    packet = EthernetPacket(number=number, timestamp=timestamp, length=len(data))
    if len(data) < 14:
        packet.protocol = 'short'
        return packet
    packet.dst_mac, packet.src_mac = _mac(data[0:6]), _mac(data[6:12])
    ethertype = struct.unpack('>H', data[12:14])[0]
    offset = 14
    if ethertype == ETH_VLAN and len(data) >= 18:
        packet.vlan = struct.unpack('>H', data[14:16])[0] & 0x0FFF
        ethertype = struct.unpack('>H', data[16:18])[0]
        offset = 18
    try:
        _decode_l3(packet, ethertype, data, offset)
    except (struct.error, IndexError, ValueError):
        pass
    if not packet.protocol:
        packet.protocol = packet.l4 or packet.l3 or ETHERTYPE_NAMES.get(ethertype, f'0x{ethertype:04X}')
    return packet


def _decode_l3(packet: EthernetPacket, ethertype: int, data: bytes, offset: int) -> None:
    if ethertype == ETH_PTP:
        packet.l3 = 'gPTP'
        message_type = data[offset] & 0x0F if len(data) > offset else 0
        packet.protocol = 'gPTP'
        packet.info = PTP_MESSAGE_TYPES.get(message_type, f'type {message_type}')
        return
    if ethertype == ETH_AVTP:
        packet.l3 = 'AVTP'
        subtype = data[offset] if len(data) > offset else 0
        packet.protocol = 'AVTP'
        name = AVTP_SUBTYPES.get(subtype, f'subtype 0x{subtype:02X}')
        stream = data[offset + 4 : offset + 12] if len(data) >= offset + 12 else b''
        packet.info = f'{name}' + (f', stream {stream.hex()}' if stream and subtype in (0x00, 0x02, 0x03) else '')
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
        _decode_ipv4(packet, data, offset)
    elif ethertype == ETH_IPV6:
        _decode_ipv6(packet, data, offset)


def _decode_ipv4(packet: EthernetPacket, data: bytes, offset: int) -> None:
    if len(data) < offset + 20:
        return
    packet.l3 = 'IPv4'
    ihl = (data[offset] & 0x0F) * 4
    protocol = data[offset + 9]
    packet.src = socket.inet_ntoa(data[offset + 12 : offset + 16])
    packet.dst = socket.inet_ntoa(data[offset + 16 : offset + 20])
    _decode_l4(packet, protocol, data, offset + ihl)


def _decode_ipv6(packet: EthernetPacket, data: bytes, offset: int) -> None:
    if len(data) < offset + 40:
        return
    packet.l3 = 'IPv6'
    next_header = data[offset + 6]
    packet.src = socket.inet_ntop(socket.AF_INET6, data[offset + 8 : offset + 24])
    packet.dst = socket.inet_ntop(socket.AF_INET6, data[offset + 24 : offset + 40])
    _decode_l4(packet, next_header, data, offset + 40)


def _decode_l4(packet: EthernetPacket, protocol: int, data: bytes, offset: int) -> None:
    if protocol == 17 and len(data) >= offset + 8:  # UDP
        packet.l4 = 'UDP'
        packet.src_port, packet.dst_port = struct.unpack('>HH', data[offset : offset + 4])
        _decode_application(packet, data[offset + 8 :])
    elif protocol == 6 and len(data) >= offset + 20:  # TCP
        packet.l4 = 'TCP'
        packet.src_port, packet.dst_port = struct.unpack('>HH', data[offset : offset + 4])
        data_offset = (data[offset + 12] >> 4) * 4
        _decode_application(packet, data[offset + data_offset :])
    elif protocol in (1, 58):
        packet.l4 = packet.protocol = 'ICMPv6' if protocol == 58 else 'ICMP'


def _decode_application(packet: EthernetPacket, payload: bytes) -> None:
    ports = {packet.src_port, packet.dst_port}
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


def _looks_like_someip(payload: bytes) -> bool:
    """A conservative check that a UDP/TCP payload begins with a plausible SOME/IP message (protocol version 1 and
    a length field consistent with the data)."""
    if len(payload) < someip.HEADER.size:
        return False
    _message_id, length, _request_id, protocol_version = struct.unpack_from('>IIIB', payload)
    return protocol_version == 0x01 and someip.HEADER_COVERED <= length <= len(payload)


def interfaces() -> List[str]:
    """The network interface names on this machine (for live capture)."""
    try:
        return [name for _index, name in socket.if_nameindex()]
    except (OSError, AttributeError):
        return []
