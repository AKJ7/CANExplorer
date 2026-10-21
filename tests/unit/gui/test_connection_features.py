import asyncio
import time
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
    plot.view = connection.plot_view
    plot.keys = ['EngineData.EngineSpeed']
    plot.palette = [QColor('red')]
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


async def test_plot_view(manager, channel, data_dir, tmp_path):
    import csv
    import math

    connection = open_connection(manager, channel)
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    connection.track_signal('EngineData', 'EngineSpeed', 'rpm')
    connection.track_signal('EngineData', 'CoolantTemp', 'degC')
    view = connection.plot_view
    assert view.charts == [['EngineData.EngineSpeed'], ['EngineData.CoolantTemp']]
    view.move_to_chart('EngineData.CoolantTemp', 0)
    assert view.charts == [['EngineData.EngineSpeed', 'EngineData.CoolantTemp']]
    assert view.separate_scales(0)  # Different units
    view.move_to_chart('EngineData.CoolantTemp', -1)
    assert len(view.charts) == 2

    # Values at known times: speed i * 64 rpm (raw i * 64, factor 0.25) at 10 + i * 0.1 s
    series = connection.session.signals
    for i in range(100):
        message = can.Message(
            arbitration_id=0x100,
            is_extended_id=False,
            timestamp=10 + i * 0.1,
            data=bytes([0, i, 40 + i % 5, 0, 0, 0, 0, 0]),
        )
        series.update(message, connection.session.decoder.database)
    connection.session.start_time = 10.0
    assert view.follow and view.time_range() == (pytest.approx(9.9), pytest.approx(19.9))
    view.set_window(5)
    assert view.time_range()[0] == pytest.approx(14.9)

    view.fit_all()
    start, end = view.time_range()
    assert not view.follow and start < 10 and end > 19.9
    view.zoom(0.5, 15.0)
    assert view.time_range()[1] - view.time_range()[0] == pytest.approx((end - start) / 2)
    view.pan(1.0)
    assert view.time_range()[0] == pytest.approx(15.0 - (15.0 - start) / 2 + 1.0)

    view.set_cursor(0, 12.05)  # Holds the value of 12.0 s
    view.set_cursor(1, 14.0)
    rows = {row['key']: row for row in view.readout}
    speed = rows['EngineData.EngineSpeed']
    assert speed['c1'] == 20 * 64 and speed['c2'] == 40 * 64 and speed['delta'] == 20 * 64
    assert speed['minimum'] == 21 * 64 and speed['maximum'] == 40 * 64 and speed['count'] == 20
    assert view.cursor_times['delta'] == '1950.000 ms'

    message = view.export_csv(QUrl.fromLocalFile(str(tmp_path / 'signals.csv')), False)
    assert message == 'Exported 100 rows of 2 signals to signals.csv'
    with open(tmp_path / 'signals.csv') as file:
        rows = list(csv.reader(file))
    assert rows[0] == ['time_s', 'EngineData.EngineSpeed [rpm]', 'EngineData.CoolantTemp [degC]']
    assert rows[1][0] == '0.000000' and float(rows[-1][1]) == 99 * 64
    visible = view.export_csv(QUrl.fromLocalFile(str(tmp_path / 'visible.csv')), True)
    assert 'Exported' in visible and int(visible.split()[1].replace(',', '')) < 100

    # Drawing several signals with separate scales and cursors
    plot = SignalPlot()
    plot.view = view
    plot.keys = ['EngineData.EngineSpeed', 'EngineData.CoolantTemp']
    plot.separate_scales = True
    plot.palette = [QColor('red'), QColor('lime')]
    plot.setWidth(500)
    plot.setHeight(200)
    assert plot.time_at(70) == pytest.approx(view.time_range()[0])
    assert plot.seconds_per_pixel() > 0
    image = QImage(500, 200, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    plot.paint(painter)
    painter.end()
    colours = [QColor(image.pixel(x, y)) for x in range(500) for y in range(200)]
    assert sum(1 for colour in colours if colour.red() > 200 and colour.green() < 100) > 30
    assert sum(1 for colour in colours if colour.green() > 200 and colour.red() < 100) > 30

    view.clear_cursors()
    view.follow_live()
    assert not view.has_cursors and view.follow and not math.isnan(view.time_range()[1])
    connection.untrack_signal('EngineData', 'CoolantTemp')
    assert view.charts == [['EngineData.EngineSpeed']]


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


def test_channel_probe_survives_failing_backend(monkeypatch):
    etas = [{'interface': 'etas', 'channel': 'ETAS://USB/SuperCAN:3c04469d/CAN:0'}]

    def detect(interface, timeout):
        if interface == 'etas':
            return etas
        if interface == 'vector':
            raise OSError('driver error')
        return []

    monkeypatch.setattr(can, 'detect_available_configs', detect)
    assert ConnectionManager._probe_interfaces() == etas


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


async def test_diagnostic_description_and_database_buses(manager, channel, data_dir):
    # 500 kbit/s matches the Powertrain cluster of example.arxml, which is selected automatically
    connection = open_connection(manager, channel, bitrate='500000', protocol='UDS')
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.arxml')))
    assert connection.database_buses == ['Powertrain', 'Chassis']
    assert connection.database_bus == 'Powertrain'
    keys = [info['key'] for info in connection.database_signals]
    assert 'EngineData.OilPressure' in keys and 'WheelSpeeds.WheelSpeedFrontLeft' not in keys
    connection.set_database_bus('Chassis')
    assert connection.database_bus == 'Chassis'
    assert [info['key'] for info in connection.database_signals] == ['WheelSpeeds.WheelSpeedFrontLeft']
    connection.set_database_bus('')
    assert connection.database_bus == ''

    # A CDD loaded as database goes to the diagnostics
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    server = UdsServer(dids={0xF100: bytes([0x0F, 0xA0, 90, 2]), 0xF190: b'WDB1234567890ABCD'})
    simulator = UdsEcuSimulator(ecu_session, server=server)
    simulator.start()
    diagnostics = connection.diagnostics
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.cdd')))
    assert diagnostics.description_file.endswith('example.cdd')
    assert connection.database_file.endswith('example.arxml')  # The signal database is unchanged
    assert diagnostics.known_dids[:3] == [
        {'did': '0123', 'name': 'OperatingHours'},
        {'did': 'F100', 'name': 'EngineStatus'},
        {'did': 'F190', 'name': 'VIN'},
    ]
    assert diagnostics.read_did('F100') == ''
    await wait_until(lambda: not diagnostics.busy)
    entry = diagnostics.history[-1]
    assert entry['description'] == 'EngineStatus: EngineSpeed=1000 rpm, CoolantTemperature=50 degC, DrivingMode=Sport'
    assert entry['request_description'] == 'ReadDataByIdentifier F1 00 (EngineStatus)'
    # The trace decoder uses the description as well
    connection._refresh()
    infos = [connection.trace.display(row, 6) for row in range(connection.trace.rowCount())]
    assert any('UDS Positive response to ReadDataByIdentifier: EngineStatus: EngineSpeed=1000 rpm' in i for i in infos)
    diagnostics.unload_description()
    assert diagnostics.description_file == ''
    assert {'did': 'F100', 'name': 'EngineStatus'} not in diagnostics.known_dids
    assert 'Could not load' in diagnostics.load_description(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    simulator.stop()
    ecu_session.stop()


async def test_odx_description(manager, channel, data_dir):
    """An ODX description loaded as a database goes to the diagnostics, like a CDD."""
    connection = open_connection(manager, channel, protocol='UDS')
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    server = UdsServer(dids={0x1001: bytes([0x0F, 0xA0]), 0xF190: b'WDB1234567890ABCD'})
    simulator = UdsEcuSimulator(ecu_session, server=server)
    simulator.start()
    diagnostics = connection.diagnostics
    assert diagnostics.load_description(QUrl.fromLocalFile(str(data_dir / 'example.odx'))) == ''
    assert diagnostics.description_file.endswith('example.odx')
    assert {'did': '1001', 'name': 'EngineSpeed'} in diagnostics.known_dids
    assert diagnostics.read_did('1001') == ''
    await wait_until(lambda: not diagnostics.busy)
    assert diagnostics.history[-1]['description'] == 'EngineSpeed: EngineSpeed=1000 rpm'
    assert diagnostics.history[-1]['request_description'] == 'ReadDataByIdentifier 10 01 (EngineSpeed)'
    simulator.stop()
    ecu_session.stop()


def test_database_file_filters(manager):
    first = manager.database_file_filters[0]
    for suffix in ('*.dbc', '*.dbf', '*.arxml', '*.kcd', '*.sym', '*.ldf', '*.cdd', '*.odx', '*.odx-d', '*.pdx'):
        assert suffix in first
    assert 'AUTOSAR ARXML (*.arxml)' in manager.database_file_filters
    assert 'CANdelaStudio CDD (*.cdd)' in manager.database_file_filters
    assert 'Packaged ODX (*.pdx)' in manager.database_file_filters


async def test_j1939_page(manager, channel, data_dir):
    from can_explorer.core.j1939_tools import PGN_ADDRESS_CLAIMED, PGN_DM1, Dtc, J1939Name, build_can_id, encode_dm

    connection = open_connection(manager, channel, protocol='J1939')
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    j1939 = connection.j1939
    assert j1939.active
    assert '0x0FEF1 VehicleSpeed' in j1939.pgn_names and '0x0FECA DM1' in j1939.pgn_names
    peer = can.Bus(interface='virtual', channel=channel)

    def send(can_id, data):
        peer.send(can.Message(arbitration_id=can_id, is_extended_id=True, data=bytes(data)))

    name = J1939Name((1 << 63) | (123 << 21) | 4567)
    send(build_can_id(PGN_ADDRESS_CLAIMED, 0x00), name.to_bytes())
    send(build_can_id(PGN_DM1, 0x00), encode_dm([Dtc(110, 0, 3)], {'MIL': 'on'}))
    send(build_can_id(0xF004, 0x03, priority=3), bytes(8))
    await wait_until(lambda: len(j1939.nodes) == 2, timeout=5)
    engine, other = j1939.nodes
    assert (engine['address_text'], engine['function'], engine['manufacturer']) == ('0x00', 'Engine', 123)
    assert engine['lamps'] == ['MIL'] and engine['active_dtcs'] == 1 and engine['previous_dtcs'] == -1
    assert not other['claimed'] and other['frames'] == 1
    details = j1939.node_details(0)
    assert details['dm1']['dtcs'][0]['name'] == 'Engine coolant temperature'
    assert {pgn['label'] for pgn in details['pgns']} == {'0x0EE00 Address Claimed', '0x0FECA DM1'}

    def received():
        frames = []
        while (frame := peer.recv(timeout=0.2)) is not None:
            frames.append(frame)
        return frames

    assert j1939.request_dm2(0) == '' and j1939.clear_dtcs(0, False) == ''
    assert j1939.send_pgn('VehicleSpeed', 6, 'FF', 'WheelSpeed=100') == ''
    await wait_until(lambda: not j1939.busy)
    frames = received()
    assert [(frame.arbitration_id, bytes(frame.data)) for frame in frames] == [
        (0x18EA00F9, b'\xcb\xfe\x00'),  # Request DM2 from 0x00
        (0x18EA00F9, b'\xd3\xfe\x00'),  # Request DM11: clear active codes
        (0x18FEF1F9, b'\xff\x00\x64\xff\xff\xff\xff\xff'),  # 100 km/h, other bytes "not available"
    ]
    assert j1939.send_pgn('VehicleSpeed', 6, 'FF', 'Nope=1') == "VehicleSpeed has no signal 'Nope'"
    assert j1939.send_pgn('Unknown', 6, 'FF', '00') == "Unknown PGN 'Unknown'"
    assert j1939.set_tool_address('1G').startswith('Invalid hex')
    assert j1939.send_pgn('0xEF00', 6, '42', '00 ' * 20) == ''  # RTS/CTS to a node that does not answer
    await wait_until(lambda: not j1939.busy, timeout=5)
    assert j1939.log[-1]['error'] and 'did not answer' in j1939.log[-1]['text']
    j1939.clear()
    assert j1939.nodes == [] or all(node['address'] == 0xF9 for node in j1939.nodes)
    peer.shutdown()


async def test_canopen_page(manager, channel):
    from pathlib import Path

    from can_explorer.core.canopen_tools import CanOpenNodeSimulator, ObjectDictionary

    eds = Path(__file__).parents[3] / 'examples' / 'canopen' / 'io_module.eds'
    connection = open_connection(manager, channel, protocol='CANopen')
    canopen = connection.canopen
    assert canopen.active and canopen.objects == [] and canopen.pdos == []
    slave = BusSession(LiveSource(BusConfig('virtual', channel)))
    slave.start()
    node = CanOpenNodeSimulator(slave, 5, ObjectDictionary.from_file(eds), heartbeat_ms=50)
    node.start()
    try:
        await wait_until(lambda: [entry['node_id'] for entry in canopen.nodes] == [5])
        canopen.set_node(5)

        # Without a dictionary: hex values and PDO configuration read from the node
        assert canopen.sdo_read('1018', '01') == ''
        await wait_until(lambda: not canopen.busy)
        assert canopen.log[-1]['text'] == 'Node 5 0x1018:01 Identity object sub 1 = 55 0A 00 00'
        assert canopen.read_pdo_configuration() == ''
        await wait_until(lambda: not canopen.busy)
        assert canopen.log[-1]['text'] == 'Read the configuration of 3 PDOs from node 5'
        assert [(pdo['name'], pdo['cob_id']) for pdo in canopen.pdos] == [
            ('RPDO1', '205'),
            ('TPDO1', '185'),
            ('TPDO2', '285'),
        ]

        assert canopen.load_dictionary(QUrl.fromLocalFile(str(eds))) == ''
        assert canopen.device_name == 'CANExplorer · IO module 8DI 8DO 2AI'
        assert {'index': '2100', 'name': 'Module temperature', 'type': 'REAL32'}.items() <= next(
            o for o in canopen.objects if o['index'] == '2100'
        ).items()
        assert canopen.sdo_read('1008', '0') == ''
        await wait_until(lambda: not canopen.busy)
        assert canopen.log[-1]['text'] == 'Node 5 0x1008:00 Manufacturer device name = "IO module 8DI 8DO 2AI"'
        assert canopen.sdo_write('1017', '0', '200') == ''
        await wait_until(lambda: not canopen.busy)
        assert node.values[(0x1017, 0)] == (200).to_bytes(2, 'little')
        assert canopen.sdo_write('1017', '0', 'fast') == 'UNSIGNED16 needs a number, got \'fast\''
        assert canopen.sdo_write('1000', '0', '1') == ''
        await wait_until(lambda: not canopen.busy)
        assert canopen.log[-1] == {'text': 'SDO abort 0x06010002: Attempt to write a read only object', 'error': True}

        assert canopen.nmt(0x01, False) == ''
        await wait_until(lambda: canopen.nodes[0]['state'] == 'Operational', timeout=5)
        await wait_until(lambda: canopen.pdos[1]['received'], timeout=5)
        values = [variable['value'] for variable in canopen.pdos[1]['variables']]
        assert values == ['5', '1200', '-350']
        assert 'Analogue input 1=1200' in connection.session.decoder.protocol.describe(
            can.Message(arbitration_id=0x185, is_extended_id=False, data=[5, 0xB0, 4, 0xA2, 0xFE])
        )
        node.emergency(0x4210, 0x09)
        await wait_until(lambda: canopen.emergencies != [])
        assert canopen.emergencies[0]['text'] == 'Device temperature' and canopen.nodes[0]['emcy_count'] == 1
        assert canopen.nmt(0x33, True).startswith('Unknown NMT command')
        assert canopen.send_sync() == ''
    finally:
        node.stop()
        slave.stop()


async def test_flashing(manager, channel, tmp_path, monkeypatch):
    from pathlib import Path

    from can_explorer.core.flashing import load_image

    connection = open_connection(manager, channel)
    flashing = connection.flashing
    firmware = Path(__file__).parents[3] / 'examples' / 'flashing' / 'firmware.hex'
    key_file = Path(__file__).parents[3] / 'examples' / 'flashing' / 'simulator_key.py'
    assert flashing.start({}) == 'Open a firmware file first'
    assert flashing.load_file(QUrl.fromLocalFile(str(tmp_path / 'missing.hex')), '0').startswith('Could not read')
    (tmp_path / 'broken.hex').write_text(':0100000000FE\n')
    assert 'checksum error' in flashing.load_file(QUrl.fromLocalFile(str(tmp_path / 'broken.hex')), '0')
    assert flashing.load_file(QUrl.fromLocalFile(str(firmware)), '0') == ''
    assert flashing.image_text.startswith('21504 bytes in 2 segments: 0x08004000–0x08008FFF, 0x08040000–0x080403FF')
    assert not flashing.binary
    (tmp_path / 'app.bin').write_bytes(bytes(100))
    assert flashing.load_file(QUrl.fromLocalFile(str(tmp_path / 'app.bin')), '8000') == '' and flashing.binary
    assert flashing.image_text.startswith('100 bytes in 1 segment: 0x00008000–0x00008063')
    assert flashing.load_file(QUrl.fromLocalFile(str(firmware)), '') == ''

    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    ecu = UdsEcuSimulator(ecu_session, server=UdsServer(download_requires_security=True))
    ecu.start()
    try:
        assert (
            flashing.start({'security_level': 1}) == 'Choose the key file in the Diagnostics view for security access'
        )
        assert flashing.start({'security_level': 2}) == 'Security access levels for requesting a seed are odd'
        assert flashing.start({'erase_routine': 'XYZ'}) == "Invalid hex erase routine: 'XYZ'"
        assert flashing.start({}) == ''  # Without security access: denied
        await wait_until(lambda: not flashing.running, timeout=10)
        assert flashing.state == 'failed' and 'ecurity' in flashing.error
        assert [step['status'] for step in flashing.steps][:3] == ['done', 'done', 'failed']

        connection.diagnostics.set_key_file(QUrl.fromLocalFile(str(key_file)))
        options = {'security_level': 1, 'check': True, 'check_routine': 'FF01', 'max_block': 0x202}
        assert flashing.start(options) == ''
        assert flashing.start(options) == 'Flashing is in progress'
        await wait_until(lambda: not flashing.running, timeout=20)
        assert flashing.state == 'done', flashing.error
        image = load_image(firmware)
        assert ecu.server.memory == {segment.address: segment.data for segment in image.segments}
        assert flashing.progress == 1.0 and flashing.progress_text.startswith('21.0 KiB of 21.0 KiB')
        assert all(step['status'] == 'done' for step in flashing.steps)
        assert flashing.steps[3]['detail'] == '20480 / 20480 bytes, blocks of 512'

        # Cancelled during the download: from the progress report itself, as polling for the progress could miss
        # the download on a fast machine (it finishes between two polls)
        report = flashing._on_job_change

        def cancel_at_ten_percent(job):
            report(job)
            if job.progress > 0.1:
                flashing.cancel()

        monkeypatch.setattr(flashing, '_on_job_change', cancel_at_ten_percent)
        assert flashing.start(options) == ''
        await wait_until(lambda: not flashing.running, timeout=10)
        assert flashing.state == 'cancelled' and 'skipped' in [step['status'] for step in flashing.steps]
    finally:
        ecu.stop()
        ecu_session.stop()


async def test_compare_recordings(manager, data_dir, tmp_path):
    compare = manager.compare

    def write(name, speeds, start):
        with can.Logger(str(tmp_path / name)) as logger:
            for i, speed in enumerate(speeds):
                raw = int(speed * 4)
                logger.on_message_received(
                    can.Message(
                        timestamp=start + i * 0.1,
                        arbitration_id=0x100,
                        is_extended_id=False,
                        data=[raw & 0xFF, raw >> 8, 90, 3, 0, 0, 0, 0],
                    )
                )
                if name.startswith('b') and i % 5 == 0:
                    logger.on_message_received(
                        can.Message(
                            timestamp=start + i * 0.1 + 0.01,
                            arbitration_id=0x7E8,
                            is_extended_id=False,
                            data=[3, 0x7F, 0x22, 0x31],
                        )
                    )

    write('a.asc', [800 + 10 * i for i in range(30)], 10.0)
    write('b.blf', [800 + 10 * i if i < 20 else 900 for i in range(30)], 50.0)
    assert compare.open_file('a', QUrl.fromLocalFile(str(tmp_path / 'notes.txt'))).startswith('Unsupported log format')
    assert compare.open_file('a', QUrl.fromLocalFile(str(tmp_path / 'a.asc'))) == ''
    assert compare.open_file('b', QUrl.fromLocalFile(str(tmp_path / 'b.blf'))) == ''
    await wait_until(lambda: not compare.busy and compare.has_result)
    assert compare.recording_a['text'] == '30 frames · 1 identifiers · 2.9 s'
    assert compare.counts == {'only_a': 0, 'only_b': 1, 'changed': 1, 'same': 0}
    engine = compare.identifiers[0]
    assert (engine['id'], engine['status'], engine['differing'], engine['paired']) == ('100', 'changed', 10, 30)
    assert engine['first_time'] == '2.000' and engine['cycle_a'] == '100.0'
    assert compare.identifiers[1]['status'] == 'only_b' and compare.identifiers[1]['cycle_b'] == '500.0'

    assert compare.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc'))) == ''
    await wait_until(lambda: not compare.busy and compare.signal_differences)
    assert compare.identifiers[0]['name'] == 'EngineData'
    speed = compare.signal_differences[0]
    assert speed['key'] == 'EngineData.EngineSpeed' and speed['range_b'] == '800 … 990'
    assert (speed['differing'], speed['max_difference'], speed['first']) == (10, '190', '2.000 s: 1000 → 900')

    assert compare.set_alignment('id', 'XYZ') == "Invalid hex identifier: 'XYZ'"
    assert compare.set_alignment('id', '7E8') == ''
    await wait_until(lambda: not compare.busy and compare.align_error)
    assert compare.align_error == '7E8 is not in recording A; aligned by the start instead'
    assert compare.set_alignment('sequence', '') == ''
    await wait_until(lambda: not compare.busy and not compare.align_error)

    message = compare.export_csv(QUrl.fromLocalFile(str(tmp_path / 'diff.csv')))
    assert message == 'Saved diff.csv' and (tmp_path / 'diff.csv').read_text().startswith('id;status;')
    compare.swap()
    await wait_until(lambda: not compare.busy and compare.counts.get('only_a') == 1)
    assert compare.recording_a['name'] == 'b.blf'


async def test_multi_bus_trace(manager):
    channels = [f'multi-{uuid.uuid4().hex}' for _ in range(2)]
    manager.add_connection({'name': 'Powertrain', 'interface': 'VirtualBus', 'channel': channels[0]})
    manager.add_connection({'name': 'Body', 'interface': 'VirtualBus', 'channel': channels[1]})
    for connection in manager.connections:
        connection.open()
    # Explicit timestamps: the virtual bus would otherwise stamp frames with time.time(), which only ticks every
    # 15.6 ms on Windows (before Python 3.13), so frames of the two buses could share a timestamp
    peers = [can.Bus(interface='virtual', channel=channel, preserve_timestamps=True) for channel in channels]
    multi = manager.multi_bus
    trace = multi.trace
    base = time.time()

    def message(can_id, data, offset):
        return can.Message(arbitration_id=can_id, is_extended_id=False, data=data, timestamp=base + offset)

    try:
        peers[0].send(message(0x100, [1], 0.0))  # Before selecting
        await wait_until(lambda: manager.connections[0].session.trace.total == 1)
        assert [entry['name'] for entry in multi.connections] == ['Powertrain', 'Body']
        multi.set_selected('Powertrain', True)
        multi.set_selected('Body', True)
        assert multi.selected == ['Powertrain', 'Body'] and trace.rowCount() == 1
        for i in range(3):
            peers[1].send(message(0x200 + i, [i], 0.1 + i * 0.2))
            peers[0].send(message(0x100, [i], 0.2 + i * 0.2))
        # Powertrain: the frame before selecting and three more; Body: three
        await wait_until(lambda: [c.session.trace.total for c in manager.connections] == [4, 3])
        multi.poll()
        rows = [(trace.display(row, 1), trace.display(row, 4)) for row in range(trace.rowCount())]
        assert rows == [('Powertrain', '100')] + [
            pair for i in range(3) for pair in (('Body', f'{0x200 + i:03X}'), ('Powertrain', '100'))
        ]
        times = [float(trace.display(row, 0)) for row in range(trace.rowCount())]
        assert times == sorted(times) and trace.COLUMNS[1] == 'Bus' and multi.frame_count == 7

        multi.set_filter('200-2FF')
        assert trace.rowCount() == 3 and multi.filter_error == ''
        multi.set_filter('zz')
        assert multi.filter_error and trace.rowCount() == 3
        multi.set_filter('')
        multi.set_paused(True)
        peers[0].send(message(0x101, [9], 1.0))
        await wait_until(lambda: manager.connections[0].session.trace.total == 5)
        multi.poll()
        assert trace.rowCount() == 7
        multi.set_paused(False)
        assert trace.rowCount() == 8
        multi.clear()
        assert trace.rowCount() == 0

        manager.remove_connection(1)
        assert multi.selected == ['Powertrain']
    finally:
        for peer in peers:
            peer.shutdown()


async def test_bus_health(manager, channel):
    from can_explorer.core.bushealth import ERR_BUSOFF, ERR_CNT, ERR_CRTL

    connection = open_connection(manager, channel)
    health = connection.bus_health
    peer = can.Bus(interface='virtual', channel=channel)
    try:
        assert health.state == '' and not health.has_counters and health.events == []
        peer.send(can.Message(arbitration_id=0x100, data=[1]))
        await wait_until(lambda: connection.session.trace.total == 1)
        connection._sample_bus_statistics()
        assert health.state == 'active'
        peer.send(
            can.Message(arbitration_id=ERR_CRTL | ERR_CNT, is_error_frame=True, data=[0, 0x20, 0, 0, 0, 0, 140, 3])
        )
        peer.send(can.Message(arbitration_id=ERR_BUSOFF, is_error_frame=True, data=bytes(8)))
        await wait_until(lambda: connection.session.trace.total == 3)
        connection._sample_bus_statistics()
        assert health.state == 'bus-off' and health.error_frames == 2 and health.counters_text == 'TEC 140 · REC 3'
        assert (health.bus_off_count, health.passive_count) == (1, 1)
        assert [event['text'] for event in health.events] == ['bus off', 'TX error passive (TEC 140, REC 3)']
        assert health.errors == [0.0, 2.0] and health.tec == [140.0] and health.peak_errors == 2
        assert [segment['state'] for segment in health.timeline] == ['active', 'bus-off']
        connection.trace.refresh()
        assert connection.trace.display(2, 6) == 'bus off'
        health.set_window(3600)
        assert (health.window_text, health.bucket, health.points) == ('1 h', 30, 120)
        assert health.errors == [2.0]  # Both seconds in one bucket, its peak
        health.clear()
        assert health.error_frames == 0 and health.state == ''
    finally:
        peer.shutdown()


async def test_remote_connection(manager, channel):
    from can_server.core.server import BusSpec, ServerThread

    thread = ServerThread([BusSpec('vehicle', 'virtual', channel)])
    port = thread.start()
    peer = can.Bus(interface='virtual', channel=channel)
    try:
        assert 'RemoteBus' in manager.interfaces
        manager.add_connection({'name': 'Car', 'interface': 'RemoteBus', 'channel': f'127.0.0.1:{port}/vehicle'})
        connection = manager.connections[-1]
        connection.open()
        await wait_until(lambda: connection.connected)
        peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=[1]))
        await wait_until(lambda: connection.session.trace.total == 1)
        assert connection.send('200', '01 02', False, False, 0, False, False) == ''
        msg = await asyncio.to_thread(peer.recv, 3)
        assert msg.arbitration_id == 0x200
        connection.close()
    finally:
        peer.shutdown()
        thread.stop()
