import asyncio
import struct

import pytest
from PyQt6.QtCore import QSettings, QUrl

from can_explorer.core import someip
from can_explorer.core.ethernet import RawPacket, write_pcap
from can_explorer.core.someip_e2e import SomeIpE2eConfig, protected_data
from can_explorer.gui.ethernet import EthernetController
from can_explorer.gui.someip import SomeIpController

CONFIG = SomeIpE2eConfig(0x1234, 0x8001, 4, 0x0A0B0C0D)


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)


def message(counter: int) -> bytes:
    header = struct.pack('>HHIHHBBBB', 0x1234, 0x8001, 8 + 14, 0, 1, 1, 1, 0x02, 0)
    parsed = someip.parse_message(header + bytes(14))
    protected = CONFIG.profile_checker().protect(protected_data(parsed), counter)
    return header + protected[8:]


def udp_frame(payload: bytes) -> bytes:
    datagram = struct.pack('>HHHH', 50000, 30509, 8 + len(payload), 0) + payload
    ip = struct.pack('>BBHHHBBH', 0x45, 0, 20 + len(datagram), 0, 0, 64, 17, 0) + bytes([10, 0, 0, 1, 10, 0, 0, 2])
    return bytes.fromhex('112233445566aabbccddeeff0800') + ip + datagram


def test_protections_are_added_saved_and_restored(qapp, settings, tmp_path):
    controller = SomeIpController(settings)
    changes = []
    controller.e2e_changed.connect(lambda: changes.append(1))
    assert controller.e2e_protections == [] and controller.e2e_checker() is None
    assert controller.e2e_profiles == [4, 6, 7]
    options = {'service_id': '1234', 'method_id': '8001', 'data_id': 'a0b0c0d', 'profile': '4', 'offset': '64'}
    assert controller.add_e2e({**options, 'name': 'Speed'}) == ''
    assert controller.add_e2e({**options, 'profile': '5'}).startswith('Profile 5 is not supported')
    assert controller.add_e2e({**options, 'method_id': '8002', 'profile': '6', 'data_id': '1234'}) == ''
    assert changes and [row['id'] for row in controller.e2e_protections] == ['1234.8001', '1234.8002']
    assert controller.e2e_protections[0] == {
        'id': '1234.8001',
        'name': 'Speed',
        'profile': 4,
        'data_id': '0xA0B0C0D',
        'offset': 64,
        'max_delta_counter': 1,
    }
    assert controller.add_e2e({**options, 'max_delta_counter': '3'}) == ''  # The same method: replaced
    assert len(controller.e2e_protections) == 2 and controller.e2e_protections[0]['max_delta_counter'] == 3

    restored = SomeIpController(settings)  # Saved in the settings
    assert restored.e2e_protections == controller.e2e_protections

    path = tmp_path / 'e2e.json'
    assert controller.export_e2e(QUrl.fromLocalFile(str(path))) == ''
    controller.remove_e2e(0)
    controller.remove_e2e(0)
    assert controller.e2e_protections == []
    assert controller.import_e2e(QUrl.fromLocalFile(str(path))) == ''
    assert len(controller.e2e_protections) == 2
    path.write_text('nonsense')
    assert controller.import_e2e(QUrl.fromLocalFile(str(path))).startswith('Cannot read')


def test_bad_saved_protections_are_ignored(qapp, settings):
    settings.setValue('someip/e2e', '[{"service_id": 1}]')
    assert SomeIpController(settings).e2e_protections == []


def test_message_rows_show_the_check(qapp, settings):
    controller = SomeIpController(settings)
    assert controller.add_e2e({'service_id': '1234', 'method_id': '8001', 'data_id': 'a0b0c0d', 'profile': '4'}) == ''
    controller._monitor = someip.SomeIpMonitor()
    controller._monitor.e2e = controller.e2e_checker()
    for counter in (1, 1):
        someip._ingest(controller._monitor, message(counter))
    rows = controller.messages
    assert rows[0]['description'].endswith('[E2E P04 ok, counter 1]') and not rows[0]['e2e_failed']
    assert rows[1]['description'].endswith('[E2E P04 REPEATED, counter 1]') and rows[1]['e2e_failed']
    controller._monitor = None


async def test_ethernet_capture_is_checked_again_when_protections_change(qapp, settings, tmp_path):
    someip_controller = SomeIpController(settings)
    controller = EthernetController(someip=someip_controller)
    try:
        pcap = tmp_path / 'someip.pcap'
        write_pcap(pcap, [RawPacket(1.0, udp_frame(message(1))), RawPacket(2.0, udp_frame(message(3)))])
        assert controller.open_pcap(QUrl.fromLocalFile(str(pcap))) == ''
        await wait_until(lambda: not controller.busy and controller.count == 2)
        assert 'E2E' not in controller.packets[0]['info']
        assert (
            someip_controller.add_e2e({'service_id': '1234', 'method_id': '8001', 'data_id': 'a0b0c0d', 'profile': '4'})
            == ''
        )
        await wait_until(lambda: not controller.busy and 'E2E' in controller.packets[0]['info'])
        assert controller.packets[0]['info'].endswith('[E2E P04 ok, counter 1]')
        assert controller.packets[1]['info'].endswith('[E2E P04 WRONG SEQUENCE, counter 3 (after 1)]')
    finally:
        controller.shutdown()
