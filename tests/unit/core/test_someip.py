import asyncio
import socket
import struct


from can_explorer.core.someip import (
    Endpoint,
    SomeIpMonitor,
    _ingest,
    describe,
    parse_message,
    parse_messages,
    parse_sd,
)


def someip(service, method, message_type, payload=b'', client=0x0001, session=0x0001, return_code=0x00, interface=1):
    length = 8 + len(payload)
    return (
        struct.pack(
            '>IIIBBBB',
            (service << 16) | method,
            length,
            (client << 16) | session,
            1,
            interface,
            message_type,
            return_code,
        )
        + payload
    )


def test_parse_message_header():
    data = someip(0x1234, 0x0045, 0x00, payload=b'\xde\xad\xbe\xef', client=0x0A0B, session=0x0203)
    message = parse_message(data)
    assert message.service_id == 0x1234 and message.method_id == 0x0045
    assert message.message_type_name == 'REQUEST' and not message.is_event and not message.is_sd
    assert message.client_id == 0x0A0B and message.session_id == 0x0203
    assert message.request_id == 0x0A0B0203 and message.message_id == 0x12340045
    assert message.payload == b'\xde\xad\xbe\xef' and message.length == 12
    assert describe(message) == 'SOME/IP 1234.0045 REQUEST (method), 4 byte payload'


def test_message_types_events_and_errors():
    assert parse_message(someip(0x1234, 0x8001, 0x02)).is_event  # Event id (bit 15) + NOTIFICATION
    assert parse_message(someip(0x1234, 0x8001, 0x02)).message_type_name == 'NOTIFICATION'
    error = parse_message(someip(0x1234, 0x0045, 0x81, return_code=0x03))
    assert error.message_type_name == 'ERROR' and error.return_code_name == 'E_UNKNOWN_METHOD'
    assert 'E_UNKNOWN_METHOD' in describe(error)
    assert parse_message(someip(0x1234, 0x0045, 0x20)).message_type_name == 'REQUEST_TP'  # Segmented


def test_several_messages_in_one_datagram():
    data = someip(0x1111, 0x0001, 0x00, b'\x01') + someip(0x2222, 0x8002, 0x02, b'\x02\x03')
    messages = parse_messages(data)
    assert [m.service_id for m in messages] == [0x1111, 0x2222]
    assert messages[1].payload == b'\x02\x03'
    # A trailing truncated message is skipped
    assert len(parse_messages(data + b'\x00\x00')) == 2


def _ipv4_option(address, port, protocol=0x11):
    body = b'\x00' + socket.inet_aton(address) + b'\x00' + bytes([protocol]) + port.to_bytes(2, 'big')
    return (len(body)).to_bytes(2, 'big') + bytes([0x04]) + body


def _sd_payload(entries: bytes, options: bytes, flags=0xC0):
    return (
        bytes([flags, 0, 0, 0]) + len(entries).to_bytes(4, 'big') + entries + len(options).to_bytes(4, 'big') + options
    )


def _offer_entry(service, instance, major=1, ttl=3, minor=0, index=0, num=1):
    return (
        bytes([0x01, index, 0, (num << 4)])
        + service.to_bytes(2, 'big')
        + instance.to_bytes(2, 'big')
        + bytes([major])
        + ttl.to_bytes(3, 'big')
        + minor.to_bytes(4, 'big')
    )


def test_service_discovery_offer_with_endpoint():
    option = _ipv4_option('192.168.0.10', 30509, protocol=0x11)
    entry = _offer_entry(0x1234, 0x0001, major=2, ttl=3, minor=5)
    payload = _sd_payload(entry, option)
    flags, entries = parse_sd(payload)
    assert flags == 0xC0 and len(entries) == 1
    offer = entries[0]
    assert offer.type_name == 'OfferService' and (offer.service_id, offer.instance_id) == (0x1234, 0x0001)
    assert offer.major_version == 2 and offer.minor_version == 5 and offer.ttl == 3
    assert offer.endpoints == (Endpoint('192.168.0.10', 30509, 'UDP'),)

    message = parse_message(someip(0xFFFF, 0x8100, 0x02, payload))
    assert message.is_sd
    assert describe(message) == 'SOME/IP-SD: OfferService 1234:0001 v2 @ 192.168.0.10:30509/UDP (TTL 3)'


def test_stop_offer_is_recognised():
    entry = _offer_entry(0x1234, 0x0001, ttl=0)  # TTL 0 = stop offering
    _, entries = parse_sd(_sd_payload(entry, b''))
    assert entries[0].type_name == 'StopOfferService'


async def test_monitor_tracks_services_and_messages():
    monitor = SomeIpMonitor()
    received = []
    monitor.add_listener(received.append)

    # Offer a service, then an event, then stop the offer
    sd = _sd_payload(_offer_entry(0x1234, 0x0001, minor=7), _ipv4_option('10.0.0.5', 30001))
    _ingest(monitor, someip(0xFFFF, 0x8100, 0x02, sd), ('10.0.0.5', 30490))
    _ingest(monitor, someip(0x1234, 0x8001, 0x02, b'\x01\x02'), ('10.0.0.5', 30001), local_port=30001)

    assert monitor.total == 2 and len(monitor.messages) == 2
    assert [r.message.service_id for r in received] == [0xFFFF, 0x1234]
    service = monitor.services[(0x1234, 0x0001)]
    assert service.minor_version == 7 and 'UDP' in str(next(iter(service.endpoints.values())))
    assert received[-1].source == '10.0.0.5:30001'

    _ingest(
        monitor,
        someip(0xFFFF, 0x8100, 0x02, _sd_payload(_offer_entry(0x1234, 0x0001, ttl=0), b'')),
        ('10.0.0.5', 30490),
    )
    assert (0x1234, 0x0001) not in monitor.services  # Stopped

    monitor.clear()
    assert monitor.total == 0 and not monitor.services


async def test_monitor_receives_real_udp_on_loopback():
    monitor = SomeIpMonitor()
    received = []
    monitor.add_listener(received.append)
    # Bind to an ephemeral port on all interfaces, no multicast
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    await monitor.open([port], multicast=None)
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(someip(0x4321, 0x0001, 0x00, b'\xaa'), ('127.0.0.1', port))
        for _ in range(200):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received and received[0].message.service_id == 0x4321
        assert received[0].local_port == port
        sender.close()
    finally:
        monitor.close()
        assert not monitor.is_open
