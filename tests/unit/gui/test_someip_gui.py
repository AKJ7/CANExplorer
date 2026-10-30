import asyncio
import socket
import struct

import pytest

from can_explorer.gui.someip import SomeIpController, _parse_ports


def someip(service, method, message_type, payload=b''):
    return (
        struct.pack('>IIIBBBB', (service << 16) | method, 8 + len(payload), 0x00010001, 1, 1, message_type, 0) + payload
    )


def sd_offer(service=0x1234, instance=0x0001):
    option_body = b'\x00' + socket.inet_aton('10.0.0.5') + b'\x00\x11' + (30001).to_bytes(2, 'big')
    option = (len(option_body)).to_bytes(2, 'big') + b'\x04' + option_body
    entry = (
        bytes([0x01, 0, 0, 0x10])
        + service.to_bytes(2, 'big')
        + instance.to_bytes(2, 'big')
        + b'\x01'
        + (3).to_bytes(3, 'big')
        + (9).to_bytes(4, 'big')
    )
    payload = b'\xc0\x00\x00\x00' + len(entry).to_bytes(4, 'big') + entry + len(option).to_bytes(4, 'big') + option
    return someip(0xFFFF, 0x8100, 0x02, payload)


def test_parse_ports():
    assert _parse_ports('30490, 30501 30502') == [30490, 30501, 30502]
    with pytest.raises(ValueError):
        _parse_ports('')
    with pytest.raises(ValueError):
        _parse_ports('99999')
    with pytest.raises(ValueError):
        _parse_ports('abc')


async def test_controller_listens_and_decodes(qapp):
    controller = SomeIpController()
    try:
        assert not controller.is_open and controller.default_ports == '30490'
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        assert controller.open(str(port), '') == ''  # No multicast for the loopback test
        for _ in range(200):
            if controller.is_open:
                break
            await asyncio.sleep(0.01)
        assert controller.is_open

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(sd_offer(), ('127.0.0.1', port))
        sender.sendto(someip(0x1234, 0x8001, 0x02, b'\xaa\xbb'), ('127.0.0.1', port))
        for _ in range(200):
            if controller.message_count >= 2:
                break
            await asyncio.sleep(0.01)
        sender.close()

        assert controller.message_count == 2
        services = controller.services
        assert len(services) == 1
        assert services[0] == {
            'service': '1234',
            'instance': '0001',
            'version': '1.9',
            'endpoints': '10.0.0.5:30001/UDP',
            'offers': 1,
        }
        messages = controller.messages
        assert messages[0]['sd'] and 'OfferService' in messages[0]['description']
        assert messages[1]['id'] == '1234.8001' and messages[1]['type'] == 'NOTIFICATION' and messages[1]['length'] == 2

        controller.clear()
        assert controller.message_count == 0 and controller.services == []
        controller.close()
        assert not controller.is_open
    finally:
        controller.shutdown()


async def test_open_reports_a_bad_port(qapp):
    controller = SomeIpController()
    assert controller.open('70000', '') == 'Port 70000 is out of range (1–65535)'
    assert not controller.is_open


async def test_controller_decodes_payloads_with_fibex(qapp, data_dir):
    from PyQt6.QtCore import QUrl

    controller = SomeIpController()
    try:
        assert controller.load_description(QUrl.fromLocalFile(str(data_dir / 'someip.fibex'))) == ''
        assert controller.description_file == 'someip.fibex'
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        assert controller.open(str(port), '') == ''
        for _ in range(200):
            if controller.is_open:
                break
            await asyncio.sleep(0.01)

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(someip(0x1234, 0x8001, 0x02, bytes([0x0F, 0xA0])), ('127.0.0.1', port))  # SpeedEvent
        for _ in range(200):
            if controller.message_count >= 1:
                break
            await asyncio.sleep(0.01)
        sender.close()

        row = controller.messages[0]
        assert row['description'] == 'speed=4000'  # Decoded, not the generic "N byte payload"
        assert row['service_name'] == 'EngineService'

        assert controller.load_description(QUrl.fromLocalFile(str(data_dir / 'example.arxml'))).startswith('No SOME/IP')
        controller.unload_description()
        assert controller.description_file == ''
    finally:
        controller.shutdown()
