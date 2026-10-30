"""SOME/IP (Scalable service-Oriented MiddlewarE over IP) parsing: the message header, and SOME/IP-SD service
discovery (entries and endpoint options).

SOME/IP is the service-oriented middleware of automotive Ethernet. A message is a 16 byte header and a payload,
carried over UDP or TCP; several messages may be concatenated in one UDP datagram. Service Discovery (SOME/IP-SD,
service 0xFFFF method 0x8100) announces which services are offered and on which endpoints.

This module decodes the headers and service discovery into a readable form; the payload is not deserialized into
signals (that needs an ARXML/FIBEX SOME/IP transformer and is left to a later step).
"""

import asyncio
import ipaddress
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field as dataclass_field
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

HEADER = struct.Struct('>IIIBBBB')  # Message ID, Length, Request ID, protocol, interface, message type, return code
HEADER_COVERED = 8  # Bytes of the header that the Length field counts (request id + the four 1-byte fields)

SD_SERVICE_ID = 0xFFFF
SD_METHOD_ID = 0x8100
EVENT_FLAG = 0x8000  # A method id with this bit set is an event (notification), not a callable method
TP_FLAG = 0x20  # Segmented (SOME/IP-TP) message types

MESSAGE_TYPES = {
    0x00: 'REQUEST',
    0x01: 'REQUEST_NO_RETURN',
    0x02: 'NOTIFICATION',
    0x80: 'RESPONSE',
    0x81: 'ERROR',
}
RETURN_CODES = {
    0x00: 'E_OK',
    0x01: 'E_NOT_OK',
    0x02: 'E_UNKNOWN_SERVICE',
    0x03: 'E_UNKNOWN_METHOD',
    0x04: 'E_NOT_READY',
    0x05: 'E_NOT_REACHABLE',
    0x06: 'E_TIMEOUT',
    0x07: 'E_WRONG_PROTOCOL_VERSION',
    0x08: 'E_WRONG_INTERFACE_VERSION',
    0x09: 'E_MALFORMED_MESSAGE',
    0x0A: 'E_WRONG_MESSAGE_TYPE',
}

SD_ENTRY_TYPES = {
    0x00: 'FindService',
    0x01: 'OfferService',
    0x06: 'SubscribeEventgroup',
    0x07: 'SubscribeEventgroupAck',
}
SD_OPTION_IPV4 = 0x04
SD_OPTION_IPV6 = 0x06
L4_PROTOCOLS = {0x06: 'TCP', 0x11: 'UDP'}


@dataclass(frozen=True, slots=True)
class SomeIpMessage:
    service_id: int
    method_id: int
    length: int
    client_id: int
    session_id: int
    protocol_version: int
    interface_version: int
    message_type: int
    return_code: int
    payload: bytes

    @property
    def is_sd(self) -> bool:
        return self.service_id == SD_SERVICE_ID and self.method_id == SD_METHOD_ID

    @property
    def is_event(self) -> bool:
        return bool(self.method_id & EVENT_FLAG) and not self.is_sd

    @property
    def message_type_name(self) -> str:
        base = self.message_type & ~TP_FLAG
        name = MESSAGE_TYPES.get(base, f'0x{self.message_type:02X}')
        return f'{name}_TP' if self.message_type & TP_FLAG else name

    @property
    def return_code_name(self) -> str:
        return RETURN_CODES.get(self.return_code, f'0x{self.return_code:02X}')

    @property
    def message_id(self) -> int:
        return (self.service_id << 16) | self.method_id

    @property
    def request_id(self) -> int:
        return (self.client_id << 16) | self.session_id


@dataclass(frozen=True, slots=True)
class Endpoint:
    address: str
    port: int
    protocol: str  # 'TCP' or 'UDP'

    def __str__(self) -> str:
        return f'{self.address}:{self.port}/{self.protocol}'


@dataclass(frozen=True, slots=True)
class SdEntry:
    type_id: int
    service_id: int
    instance_id: int
    major_version: int
    ttl: int
    minor_version: Optional[int] = None  # Service entries (Find/Offer)
    eventgroup_id: Optional[int] = None  # Eventgroup entries (Subscribe/SubscribeAck)
    endpoints: Tuple[Endpoint, ...] = ()

    @property
    def type_name(self) -> str:
        name = SD_ENTRY_TYPES.get(self.type_id, f'0x{self.type_id:02X}')
        if self.ttl == 0 and self.type_id in (0x01, 0x06):  # TTL 0 means "stop"
            return 'StopOfferService' if self.type_id == 0x01 else 'StopSubscribeEventgroup'
        return name


def parse_messages(data: bytes) -> List[SomeIpMessage]:
    """Every SOME/IP message in a datagram (several may be concatenated), skipping trailing bytes that are too
    short to be a header."""
    messages = []
    offset = 0
    while offset + HEADER.size <= len(data):
        message_id, length, request_id, protocol, interface, message_type, return_code = HEADER.unpack_from(
            data, offset
        )
        if length < HEADER_COVERED:
            break
        payload_length = length - HEADER_COVERED
        start = offset + HEADER.size
        end = start + payload_length
        if end > len(data):
            break  # Truncated
        messages.append(
            SomeIpMessage(
                service_id=message_id >> 16,
                method_id=message_id & 0xFFFF,
                length=length,
                client_id=request_id >> 16,
                session_id=request_id & 0xFFFF,
                protocol_version=protocol,
                interface_version=interface,
                message_type=message_type,
                return_code=return_code,
                payload=bytes(data[start:end]),
            )
        )
        offset = end
    return messages


def parse_message(data: bytes) -> Optional[SomeIpMessage]:
    messages = parse_messages(data)
    return messages[0] if messages else None


def _parse_options(data: bytes) -> List[Optional[Endpoint]]:
    """The options array into endpoints (None for options that are not an IPv4/IPv6 endpoint)."""
    options: List[Optional[Endpoint]] = []
    offset = 0
    while offset + 3 <= len(data):
        length = int.from_bytes(data[offset : offset + 2], 'big')
        option_type = data[offset + 2]
        body = data[offset + 3 : offset + 3 + length]
        offset += 3 + length
        if option_type == SD_OPTION_IPV4 and len(body) >= 9:
            address = str(ipaddress.IPv4Address(body[1:5]))
            protocol = L4_PROTOCOLS.get(body[6], f'0x{body[6]:02X}')
            port = int.from_bytes(body[7:9], 'big')
            options.append(Endpoint(address, port, protocol))
        elif option_type == SD_OPTION_IPV6 and len(body) >= 21:
            address = str(ipaddress.IPv6Address(body[1:17]))
            protocol = L4_PROTOCOLS.get(body[18], f'0x{body[18]:02X}')
            port = int.from_bytes(body[19:21], 'big')
            options.append(Endpoint(address, port, protocol))
        else:
            options.append(None)
    return options


def parse_sd(payload: bytes) -> Tuple[int, List[SdEntry]]:
    """The Service Discovery payload into (flags, entries), each entry carrying its resolved endpoint options."""
    if len(payload) < 12:
        return 0, []
    flags = payload[0]
    entries_length = int.from_bytes(payload[4:8], 'big')
    entries_data = payload[8 : 8 + entries_length]
    rest = payload[8 + entries_length :]
    options: List[Optional[Endpoint]] = []
    if len(rest) >= 4:
        options_length = int.from_bytes(rest[0:4], 'big')
        options = _parse_options(rest[4 : 4 + options_length])

    entries = []
    for offset in range(0, len(entries_data) - 15, 16):
        entry = entries_data[offset : offset + 16]
        type_id = entry[0]
        index1, index2, counts = entry[1], entry[2], entry[3]
        num1, num2 = counts >> 4, counts & 0x0F
        service_id = int.from_bytes(entry[4:6], 'big')
        instance_id = int.from_bytes(entry[6:8], 'big')
        major_version = entry[8]
        ttl = int.from_bytes(entry[9:12], 'big')
        minor_version = eventgroup_id = None
        if type_id in (0x00, 0x01):  # Service entry
            minor_version = int.from_bytes(entry[12:16], 'big')
        else:  # Eventgroup entry
            eventgroup_id = int.from_bytes(entry[14:16], 'big')
        referenced = [options[index1 + i] for i in range(num1) if index1 + i < len(options)]
        referenced += [options[index2 + i] for i in range(num2) if index2 + i < len(options)]
        endpoints = tuple(endpoint for endpoint in referenced if endpoint is not None)
        entries.append(
            SdEntry(type_id, service_id, instance_id, major_version, ttl, minor_version, eventgroup_id, endpoints)
        )
    return flags, entries


def describe(message: SomeIpMessage) -> str:
    """A one-line description of a SOME/IP message, expanding Service Discovery entries."""
    if message.is_sd:
        _, entries = parse_sd(message.payload)
        parts = []
        for entry in entries:
            text = f'{entry.type_name} {entry.service_id:04X}:{entry.instance_id:04X} v{entry.major_version}'
            if entry.eventgroup_id is not None:
                text += f' eventgroup {entry.eventgroup_id:04X}'
            if entry.endpoints:
                text += ' @ ' + ', '.join(str(endpoint) for endpoint in entry.endpoints)
            if entry.ttl and entry.type_name not in ('StopOfferService', 'StopSubscribeEventgroup'):
                text += f' (TTL {entry.ttl})'
            parts.append(text)
        return 'SOME/IP-SD: ' + ('; '.join(parts) if parts else 'no entries')
    kind = 'event' if message.is_event else 'method'
    text = f'SOME/IP {message.service_id:04X}.{message.method_id:04X} {message.message_type_name} ({kind})'
    if message.message_type & ~TP_FLAG in (0x80, 0x81) and message.return_code != 0:
        text += f' {message.return_code_name}'
    if message.payload:
        text += f', {len(message.payload)} byte payload'
    return text


@dataclass
class Service:
    """A service seen offered in Service Discovery."""

    service_id: int
    instance_id: int
    major_version: int = 0
    minor_version: int = 0
    endpoints: Dict[str, Endpoint] = dataclass_field(default_factory=dict)  # By 'address:port/proto'
    offers: int = 0

    @property
    def key(self) -> Tuple[int, int]:
        return (self.service_id, self.instance_id)


DEFAULT_SD_MULTICAST = '224.244.224.245'  # A common SOME/IP-SD multicast group; the real one is project specific
DEFAULT_SD_PORT = 30490


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    timestamp: float
    source: str  # 'address:port' of the sender
    local_port: int  # The port it was received on
    message: SomeIpMessage


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, monitor: 'SomeIpMonitor', local_port: int):
        self._monitor = monitor
        self._local_port = local_port

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        self._monitor._on_datagram(data, addr, self._local_port)


class SomeIpMonitor:
    """Receives SOME/IP over UDP on one or more ports (and optionally a Service Discovery multicast group), decodes
    the messages and tracks the services announced by Service Discovery.

    A plain UDP socket sees the multicast it joins and the traffic addressed to this host; capturing all SOME/IP on
    the wire would need promiscuous packet capture, which is left to a later step.
    """

    def __init__(self, max_messages: int = 200_000):
        self.services: Dict[Tuple[int, int], Service] = {}
        self.messages: Deque[ReceivedMessage] = deque(maxlen=max_messages)
        self.total = 0
        self.error: Optional[str] = None
        self._transports: List[asyncio.BaseTransport] = []
        self._listeners: List[Callable[[ReceivedMessage], None]] = []

    @property
    def is_open(self) -> bool:
        return bool(self._transports)

    def add_listener(self, callback: Callable[[ReceivedMessage], None]) -> None:
        self._listeners.append(callback)

    async def open(self, ports: Iterable[int], multicast: Optional[str] = DEFAULT_SD_MULTICAST) -> None:
        """Listen on each UDP port, joining `multicast` (the SD group) on the first one. Raises OSError on failure."""
        loop = asyncio.get_running_loop()
        for index, port in enumerate(dict.fromkeys(ports)):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('', port))
                if multicast and index == 0:
                    group = socket.inet_aton(multicast) + socket.inet_aton('0.0.0.0')
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group)
            except OSError:
                sock.close()
                self.close()
                raise
            transport, _ = await loop.create_datagram_endpoint(lambda p=port: _Protocol(self, p), sock=sock)
            self._transports.append(transport)

    def _on_datagram(self, data: bytes, addr: Tuple[str, int], local_port: int) -> None:
        now = time.time()
        for message in parse_messages(data):
            received = ReceivedMessage(now, f'{addr[0]}:{addr[1]}', local_port, message)
            self.messages.append(received)
            self.total += 1
            if message.is_sd:
                self._update_services(message)
            for listener in self._listeners:
                listener(received)

    def _update_services(self, message: SomeIpMessage) -> None:
        _, entries = parse_sd(message.payload)
        for entry in entries:
            if entry.type_id != 0x01:  # Only OfferService / StopOfferService change the service landscape
                continue
            key = (entry.service_id, entry.instance_id)
            if entry.ttl == 0:
                self.services.pop(key, None)
                continue
            service = self.services.get(key)
            if service is None:
                service = self.services[key] = Service(entry.service_id, entry.instance_id)
            service.major_version = entry.major_version
            if entry.minor_version is not None:
                service.minor_version = entry.minor_version
            service.offers += 1
            for endpoint in entry.endpoints:
                service.endpoints[str(endpoint)] = endpoint

    def clear(self) -> None:
        self.messages.clear()
        self.services.clear()
        self.total = 0

    def close(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()


def _ingest(monitor: SomeIpMonitor, data: bytes, source: Tuple[str, int] = ('0.0.0.0', 0), local_port: int = 0) -> None:
    """Feed a datagram to a monitor directly (for tests and offline decoding of captured packets)."""
    monitor._on_datagram(data, source, local_port)
