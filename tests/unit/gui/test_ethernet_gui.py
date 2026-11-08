import asyncio
import socket
import struct

import can
import pytest
from PyQt6.QtCore import QUrl

from can_explorer.core import ethernet, pcap_live
from can_explorer.gui.ethernet import EthernetController


def _acf_can_frame(can_id, data):
    """An Ethernet frame carrying one ACF_CAN message (classic, 11-bit id) over a version-0 TSCF AVTPDU."""
    pad = (4 - (len(data) % 4)) % 4  # ACF messages are quadlet-aligned
    acf = bytearray(16 + len(data) + pad)
    length_quadlets = len(acf) // 4
    acf[0], acf[1] = (0x01 << 1) | (length_quadlets >> 8), length_quadlets & 0xFF
    acf[2] = pad << 6
    struct.pack_into('>I', acf, 12, can_id)
    acf[16 : 16 + len(data)] = data
    tscf = bytearray(24)
    tscf[0] = ethernet.avtp_acf.SUBTYPE_TSCF
    struct.pack_into('>H', tscf, 20, len(acf))
    body = bytes(tscf) + bytes(acf)
    return bytes.fromhex('112233445566') + bytes.fromhex('aabbccddeeff') + struct.pack('>H', ethernet.ETH_AVTP) + body


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
    assert controller.count == 8
    assert controller.file_name == 'automotive.pcap'

    protocols = {row['protocol'] for row in controller.protocols}
    assert {'SOME/IP', 'DoIP', 'gPTP', 'AVTP', 'ARP'} <= protocols
    assert any('routing activation' in row['info'] for row in controller.packets)

    # gPTP time-sync summary
    assert controller.has_gptp
    summary = controller.gptp
    assert summary['grandmaster'] == 'aa:bb:cc:ff:fe:11:22:33' and summary['gm_clock_class'] == 6
    assert summary['sync_interval_ms'] == 125.0 and summary['sync_rate_hz'] == 8.0

    controller.set_filter('DoIP')
    assert [row['protocol'] for row in controller.packets] == ['DoIP']
    controller.set_filter('192.168.0.20')  # Filter by address
    assert all('192.168.0.20' in row['source'] or '192.168.0.20' in row['destination'] for row in controller.packets)
    controller.set_filter('')
    assert controller.count == 8

    # Export round-trips through pcap
    out = tmp_path / 'out.pcap'
    assert 'Saved 8 packets' in controller.export_pcap(QUrl.fromLocalFile(str(out)))
    assert controller.open_pcap(QUrl.fromLocalFile(str(out))) == ''
    await wait_until(lambda: not controller.busy and controller.file_name == 'out.pcap')
    assert controller.count == 8


async def test_export_tunnelled_can_log(controller, tmp_path):
    # A capture of two ACF-CAN frames, fed through the real decoder.
    controller._decoded = [
        controller._decoder.decode(_acf_can_frame(0x123, bytes(range(8))), timestamp=1.0, number=1),
        controller._decoder.decode(_acf_can_frame(0x200, b'\xaa\xbb'), timestamp=1.5, number=2),
    ]
    assert controller.can_frame_count == 2

    out = tmp_path / 'tunnelled.log'
    message = controller.export_can_log(QUrl.fromLocalFile(str(out)))
    assert 'Saved 2 CAN frames' in message

    with can.LogReader(str(out)) as reader:
        messages = list(reader)
    assert [m.arbitration_id for m in messages] == [0x123, 0x200]
    assert bytes(messages[0].data) == bytes(range(8)) and bytes(messages[1].data) == b'\xaa\xbb'


async def test_export_can_log_without_frames(controller, tmp_path):
    assert 'No tunnelled CAN frames' in controller.export_can_log(QUrl.fromLocalFile(str(tmp_path / 'empty.log')))


async def test_dissect_packet(controller, data_dir):
    assert controller.open_pcap(QUrl.fromLocalFile(str(data_dir / 'automotive.pcap'))) == ''
    await wait_until(lambda: not controller.busy and controller.count > 0)
    rows = controller.dissect(1)
    assert rows and rows[0]['depth'] == 0 and rows[0]['label'] == 'Frame'
    assert any(row['label'] == 'Ethernet II' and row['depth'] == 1 for row in rows)
    assert controller.dissect(0) == [] and controller.dissect(9999) == []  # Out of range


async def test_set_nm_port_redecodes(controller, data_dir):
    assert controller.open_pcap(QUrl.fromLocalFile(str(data_dir / 'automotive.pcap'))) == ''
    await wait_until(lambda: not controller.busy and controller.count > 0)
    controller.set_nm_port(49152)
    assert controller.nm_port == 49152
    await wait_until(lambda: not controller.busy)
    assert controller.count == 8  # Re-decode keeps every packet
    controller.set_nm_port(0)
    assert controller.nm_port == 0


async def test_avtp_stream_health(controller):
    sid = bytes.fromhex('0011223344556677')

    def frame(sequence):
        payload = bytes([0x02, 0, sequence, 0]) + sid + bytes(12)  # AAF common stream header
        return b'\x91\xe0\xf0\x00\x00\x01\xaa\xbb\xcc\xdd\xee\xff' + struct.pack('>H', ethernet.ETH_AVTP) + payload

    controller._decoded = [
        controller._decoder.decode(frame(seq), timestamp=index * 0.1, number=index + 1)
        for index, seq in enumerate([0, 1, 2, 5])  # 3 and 4 missing
    ]
    assert controller.has_avtp_streams
    (row,) = controller.avtp_streams
    assert row['stream'] == sid.hex() and row['packets'] == 4 and row['lost'] == 2
    assert row['format'].startswith('AAF')


async def test_interfaces_is_a_list(controller):
    assert isinstance(controller.interfaces, list)


async def test_start_capture_reports_error_for_bad_interface(controller):
    # start_capture must always return an error here and not enter the capturing state. When a capture backend is
    # available (libpcap/Npcap or AF_PACKET) it fails on the interface and names it; with no backend at all (e.g. a
    # Windows CI runner without Npcap) the error is the "install libpcap/Npcap" message instead.
    error = controller.start_capture('no-such-interface-zzz')
    assert error and not controller.capturing
    if pcap_live.available() or hasattr(socket, 'AF_PACKET'):
        assert 'no-such-interface-zzz' in error


async def test_open_bad_file_reports_error(controller, tmp_path):
    bad = tmp_path / 'bad.pcap'
    bad.write_bytes(b'not a pcap file at all, but long enough to reach the magic-number check')
    assert controller.open_pcap(QUrl.fromLocalFile(str(bad))) == ''
    await wait_until(lambda: not controller.busy and controller.error != '')
    assert 'pcap' in controller.error.lower()
    assert controller.count == 0
