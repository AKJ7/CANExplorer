import asyncio
import uuid

import can
import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QColor, QImage, QPainter

from can_explorer.core.simulation import LinSlaveSimulator, UdsEcuSimulator, UdsServer
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.gui.signal_plot import SignalPlot


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def manager(qapp):
    manager = ConnectionManager()
    yield manager
    manager.close_all()


@pytest.fixture
def channel():
    return f'features-{uuid.uuid4().hex}'


def open_connection(manager, channel, **options):
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel, **options})
    connection = manager.connections[-1]
    connection.open()
    return connection


async def test_bus_statistics(manager, channel):
    connection = open_connection(manager, channel)
    peer = can.Bus(interface='virtual', channel=channel)
    for _ in range(20):
        peer.send(can.Message(arbitration_id=1, is_extended_id=False, data=bytes(8)))
    await wait_until(lambda: connection.session.trace.total == 20)
    await asyncio.sleep(0.1)
    connection._sample_bus_statistics()
    peer.shutdown()
    assert connection.rx_rate > 0 and connection.tx_rate == 0
    assert 0 < connection.bus_load < 100
    assert connection.error_frames == 0


async def test_signal_tracking_and_plot(manager, channel, data_dir):
    connection = open_connection(manager, channel)
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    assert [info['key'] for info in connection.database_signals][0] == 'EngineData.EngineSpeed'
    connection.track_signal('EngineData', 'EngineSpeed', 'rpm')
    assert connection.tracked_signals == ['EngineData.EngineSpeed']
    updates = []
    connection.signals_updated.connect(lambda: updates.append(1))
    peer = can.Bus(interface='virtual', channel=channel)
    for i in range(50):
        peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([i, 0, 90, 0, 0, 0, 0, 0])))
    await wait_until(lambda: connection.session.trace.total == 50)
    peer.shutdown()
    connection._refresh()
    assert updates

    plot = SignalPlot()
    plot.connection = connection
    plot.signal_key = 'EngineData.EngineSpeed'
    plot.line_color = QColor('red')
    plot.setWidth(400)
    plot.setHeight(150)
    image = QImage(400, 150, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    plot.paint(painter)
    painter.end()
    red = sum(1 for x in range(400) for y in range(150) if QColor(image.pixel(x, y)).red() > 200)
    assert red > 50  # The line was drawn

    connection.unload_database()
    assert connection.tracked_signals == [] and connection.database_signals == []


async def test_export_selection(manager, channel, tmp_path):
    connection = open_connection(manager, channel)
    for i in range(4):
        connection.send(f'{i + 1:X}', '00', False, False, 0, False, False)
    connection._refresh()
    message = connection.export_trace(QUrl.fromLocalFile(str(tmp_path / 'out.asc')), None)
    assert message == 'Exported 4 frames to out.asc'
    assert [msg.arbitration_id for msg in can.LogReader(tmp_path / 'out.asc')] == [1, 2, 3, 4]


async def test_diagnostics_against_simulated_ecu(manager, channel, tmp_path):
    connection = open_connection(manager, channel)
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    simulator = UdsEcuSimulator(ecu_session)
    simulator.start()
    diagnostics = connection.diagnostics
    assert diagnostics.transport_name == 'ISO-TP'

    async def run(start):
        assert start() == ''
        assert diagnostics.busy
        await wait_until(lambda: not diagnostics.busy)
        return diagnostics.history[-1]

    entry = await run(lambda: diagnostics.raw_request('10 03'))
    assert entry['positive'] and entry['response'] == '50 03 00 32 01 F4'
    assert diagnostics.raw_request('xyz') != ''
    assert (await run(lambda: diagnostics.read_did('F190')))['description'] == 'VIN = WDB1234567890ABCD'
    assert (await run(lambda: diagnostics.read_did('F186')))['description'] == 'Active diagnostic session = extended'
    missing = await run(lambda: diagnostics.read_did('1234'))
    assert missing['error'] and 'requestOutOfRange' in missing['description']
    assert diagnostics.security_access(1) == 'Choose a key file defining compute_key(level, seed)'
    key_file = tmp_path / 'key.py'
    key_file.write_text('def compute_key(level, seed):\n    return bytes(b ^ 0xFF for b in seed)\n')
    diagnostics.set_key_file(QUrl.fromLocalFile(str(key_file)))
    assert (await run(lambda: diagnostics.security_access(1)))['description'] == 'Security level 1 unlocked'
    assert (await run(lambda: diagnostics.write_did('F198', '41 42')))['description'] == 'Repair shop code written'
    entry = await run(lambda: diagnostics.read_dtcs('FF'))
    assert entry['description'] == '2 DTCs with status mask 0xFF'
    assert diagnostics.dtcs[0] == {'code': 'P0123-45', 'status': '0x09', 'flags': 'testFailed, confirmedDTC'}
    await run(diagnostics.clear_dtcs)
    assert diagnostics.dtcs == []
    entry = await run(lambda: diagnostics.routine_control(1, '0203', '01'))
    assert entry['description'] == 'Routine 0x0203 start: 00'
    await run(lambda: diagnostics.ecu_reset(1))
    diagnostics.set_tester_present(True)
    assert diagnostics.tester_present
    diagnostics.set_tester_present(False)
    assert not diagnostics.tester_present
    assert diagnostics.set_addressing('7E1', '7E9', False, '') == ''
    assert 'out of range' in diagnostics.set_addressing('800', '7E9', False, '')
    diagnostics.clear_history()
    assert diagnostics.history == []
    simulator.stop()
    ecu_session.stop()


async def test_lin_connection(manager, channel, data_dir):
    connection = open_connection(manager, channel, bus='LIN', lin_baudrate='19200')
    assert connection.is_lin and connection.protocol == 'LIN'
    assert 'LIN @ 19200 baud' in connection.description
    slaves = BusSession(LiveSource(BusConfig('virtual', channel, bus_type='LIN')))
    slaves.start()
    simulator = LinSlaveSimulator(slaves, {0x10: bytes([40, 0x2C, 0x01])}, nad=0x01, server=UdsServer())
    assert connection.start_schedule('NormalTable') == 'Load an LDF file first'
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.ldf')))
    assert connection.schedule_tables == ['NormalTable', 'DiagnosticTable']
    assert [entry['signal'] for entry in connection.master_signals] == ['LightOn', 'SeatHeating']
    assert connection.set_master_signal('SeatHeating', 'Low') == ''
    assert 'Invalid value' in connection.set_master_signal('SeatHeating', 'Boiling')
    assert connection.start_schedule('NormalTable') == ''
    assert connection.schedule_running and connection.schedule_table == 'NormalTable'
    seen = lambda frame_id, rx: any(  # noqa: E731
        f.message.arbitration_id == frame_id and f.message.is_rx == rx for f in connection.session.trace
    )
    await wait_until(lambda: seen(0x10, True) and seen(0x11, False))
    connection.stop_schedule()
    assert not connection.schedule_running
    connection._refresh()
    infos = [connection.trace.display(row, 6) for row in range(connection.trace.rowCount())]
    assert any('SeatStatus: SeatPosition=20.000 mm' in info for info in infos)
    assert any(info.startswith('LIN header ID 0x10') for info in infos)
    assert any('SeatControl: SeatHeating=Low' in info for info in infos)
    # Header only and response from the transmit panel
    assert connection.send('20', '', False, False, 0, False, True) == ''
    assert 'identifiers are 00-3F' in connection.send('40', '01', False, False, 0, False, False)
    assert 'needs 1-8 data bytes' in connection.send('21', '', False, False, 0, False, False)
    # UDS over the LIN transport layer
    diagnostics = connection.diagnostics
    assert diagnostics.transport_name == 'LIN transport layer'
    assert diagnostics.set_addressing('', '', False, '01') == ''
    assert diagnostics.read_did('F190') == ''
    await wait_until(lambda: not diagnostics.busy)
    assert diagnostics.history[-1]['description'] == 'VIN = WDB1234567890ABCD'
    assert 'reserved' in diagnostics.set_addressing('', '', False, '00')
    connection.unload_database()
    assert connection.schedule_tables == [] and connection.master_signals == []
    simulator.close()
    slaves.stop()


async def test_channel_detection(manager, monkeypatch):
    monkeypatch.setattr(
        ConnectionManager,
        '_probe_interfaces',
        staticmethod(
            lambda: [
                {'interface': 'virtual', 'channel': 'a'},
                {'interface': 'virtual', 'channel': 'a'},
                {'interface': 'x'},
            ]
        ),
    )
    manager.detect_channels()
    assert manager.detecting
    await wait_until(lambda: not manager.detecting)
    assert manager.detected_channels == {'VirtualBus': ['a']}


async def test_fd_connection_and_recent_entries(manager, channel):
    connection = open_connection(manager, channel, fd=True, data_bitrate='2000000', protocol='ISO-TP')
    assert connection.fd and 'FD, data 2000000 bit/s' in connection.description
    assert connection.send('123', ' '.join(['AA'] * 64), False, True, 0, False, False) == ''
    recent = manager.app_state.recent_connections[0]
    assert recent['fd'] and recent['data_bitrate'] == 2000000 and recent['protocol'] == 'ISO-TP'
    manager.open_recent_connection(0)
    assert len(manager.connections) == 2 and manager.connections[1].fd


async def test_invalid_connection_options_are_reported(manager):
    errors = []
    manager.error_occurred.connect(errors.append)
    manager.add_connection({'name': 'x', 'interface': 'VirtualBus', 'channel': 'c', 'protocol': 'Nope'})
    manager.add_connection({'name': 'x'})
    assert len(errors) == 2 and manager.connections == []


async def test_dbf_database_and_conversion(manager, channel, data_dir, tmp_path):
    destination = tmp_path / 'example.dbf'
    summary = manager.convert_database(
        QUrl.fromLocalFile(str(data_dir / 'example.dbc')), QUrl.fromLocalFile(str(destination))
    )
    assert summary == 'Converted example.dbc to example.dbf: 2 messages, 4 signals'
    connection = open_connection(manager, channel)
    connection.load_database(QUrl.fromLocalFile(str(destination)))
    assert connection.database_file.endswith('example.dbf')
    peer = can.Bus(interface='virtual', channel=channel)
    peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([0, 0x10, 90, 2, 0, 0, 0, 0])))
    await wait_until(lambda: connection.session.trace.total == 1)
    peer.shutdown()
    connection._refresh()
    assert (
        connection.trace.display(0, 6) == 'EngineData: EngineSpeed=1024 rpm, CoolantTemp=50 degC, GearPosition=Second'
    )
    errors = []
    manager.error_occurred.connect(errors.append)
    assert (
        manager.convert_database(QUrl.fromLocalFile(str(destination)), QUrl.fromLocalFile(str(tmp_path / 'x.txt')))
        == ''
    )
    assert errors and 'Cannot write .txt' in errors[0]
