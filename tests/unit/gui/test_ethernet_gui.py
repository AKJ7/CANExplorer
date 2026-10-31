import asyncio

import pytest
from PyQt6.QtCore import QUrl

from can_explorer.gui.ethernet import EthernetController


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out'
        await asyncio.sleep(0.01)


@pytest.fixture
def controller(qapp):
    controller = EthernetController()
    yield controller
    controller.shutdown()


async def test_open_pcap_decodes_and_filters(controller, data_dir, tmp_path):
    assert controller.open_pcap(QUrl.fromLocalFile(str(data_dir / 'automotive.pcap'))) == ''
    await wait_until(lambda: not controller.busy and controller.count > 0)
    assert controller.count == 6
    assert controller.file_name == 'automotive.pcap'

    protocols = {row['protocol'] for row in controller.protocols}
    assert {'SOME/IP', 'DoIP', 'gPTP', 'AVTP', 'ARP'} <= protocols
    assert any('routing activation' in row['info'] for row in controller.packets)

    controller.set_filter('DoIP')
    assert [row['protocol'] for row in controller.packets] == ['DoIP']
    controller.set_filter('192.168.0.20')  # Filter by address
    assert all('192.168.0.20' in row['source'] or '192.168.0.20' in row['destination'] for row in controller.packets)
    controller.set_filter('')
    assert controller.count == 6

    # Export round-trips through pcap
    out = tmp_path / 'out.pcap'
    assert 'Saved 6 packets' in controller.export_pcap(QUrl.fromLocalFile(str(out)))
    assert controller.open_pcap(QUrl.fromLocalFile(str(out))) == ''
    await wait_until(lambda: not controller.busy and controller.file_name == 'out.pcap')
    assert controller.count == 6


async def test_open_bad_file_reports_error(controller, tmp_path):
    bad = tmp_path / 'bad.pcap'
    bad.write_bytes(b'not a pcap file at all')
    assert controller.open_pcap(QUrl.fromLocalFile(str(bad))) == ''
    await wait_until(lambda: not controller.busy and controller.error != '')
    assert 'pcap' in controller.error.lower()
    assert controller.count == 0
