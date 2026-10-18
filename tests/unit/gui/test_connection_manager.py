import asyncio
from pathlib import Path
import uuid

import can
import pytest
from PyQt6.QtCore import QUrl

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.session import SessionState
from can_explorer.gui.connection_manager import ConnectionManager


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


async def test_live_connection(manager, data_dir, tmp_path):
    channel = f'gui-{uuid.uuid4().hex}'
    errors = []
    manager.error_occurred.connect(errors.append)
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel, 'bitrate': '500000'})
    connection = manager.connections[0]
    connection.open()
    assert connection.connected and connection.can_transmit and not connection.is_replay

    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    connection.start_recording(QUrl.fromLocalFile(str(tmp_path / 'log.blf')))
    assert connection.recording and connection.database_file.endswith('example.dbc')

    peer = can.Bus(interface='virtual', channel=channel)
    peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([0, 0x10, 90, 1, 0, 0, 0, 0])))
    assert connection.send('7DF', '02 01 0D', False, False, 0, False, False) == ''
    assert 'Invalid hex' in connection.send('xyz', '', False, False, 0, False, False)
    assert 'At most 8' in connection.send('100', ' '.join(['00'] * 9), False, False, 0, False, False)
    await wait_until(lambda: connection.session.trace.total == 2)
    peer.shutdown()

    connection._refresh()
    assert connection.frame_count == 2
    assert connection.trace.rowCount() == 2 and connection.overview.rowCount() == 2
    infos = {connection.trace.display(row, 3): connection.trace.display(row, 6) for row in range(2)}
    assert infos['100'].startswith('EngineData: EngineSpeed=1024 rpm')
    assert infos['7DF'] == ''

    connection.set_filter('7DF')
    assert connection.trace.rowCount() == 1 and connection.filter_error == ''
    connection.set_filter('bogus?')
    assert connection.filter_error and connection.trace.rowCount() == 1

    connection.set_paused(True)
    connection.clear()
    assert connection.trace.rowCount() == 0 and connection.frame_count == 0

    connection.close()
    assert not connection.connected and not connection.recording
    assert errors == []


async def test_replay_connection(manager, tmp_path):
    path = tmp_path / 'replay.asc'
    with can.Logger(path) as logger:
        for i in range(5):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=float(i) / 100))
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'CANopen', 0.0)
    connection = manager.connections[0]
    assert connection.is_replay and not connection.can_transmit and connection.protocol == 'CANopen'
    await wait_until(lambda: connection.state == 'finished')
    connection._refresh()
    assert connection.frame_count == 5
    manager.remove_connection(0)
    assert manager.connections == []


async def test_errors_are_reported(manager):
    errors = []
    manager.error_occurred.connect(errors.append)
    manager.add_connection({'name': 'Broken', 'interface': 'NoSuchBus', 'channel': 'x'})
    manager.connections[0].open()
    assert errors and not manager.connections[0].connected


async def test_dashboard_figures(manager, data_dir):
    channel = f'gui-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel, 'bitrate': '500000'})
    connection = manager.connections[0]
    connection.open()
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    assert connection.rx_history == [] and connection.traffic_breakdown == [] and connection.top_identifiers == []

    peer = can.Bus(interface='virtual', channel=channel)
    for _ in range(3):
        peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes(8)))
    peer.send(can.Message(arbitration_id=0x18FEF100, is_extended_id=True, data=bytes(8)))
    await wait_until(lambda: connection.session.trace.total == 4)
    peer.shutdown()

    connection._sample_bus_statistics()
    connection._sample_bus_statistics()
    assert len(connection.rx_history) == len(connection.tx_history) == len(connection.load_history) == 2
    assert connection.identifier_count == 2
    assert connection.traffic_breakdown == [{'label': 'Standard', 'value': 3}, {'label': 'Extended', 'value': 1}]
    first, second = connection.top_identifiers
    assert first['id'] == '100' and first['name'] == 'EngineData' and first['direction'] == 'Rx'
    assert first['count'] == 3 and first['share'] == 1.0
    assert second['id'] == '18FEF100' and second['share'] == pytest.approx(1 / 3)

    connection.clear()
    assert connection.rx_history == [] and connection.top_identifiers == []


async def test_replay_playback_controls(manager, tmp_path):
    path = tmp_path / 'replay.asc'
    with can.Logger(path) as logger:
        for i in range(30):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=10.0 + i * 0.02))
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 1.0)
    connection = manager.connections[0]
    replay = connection.replay
    assert connection.bus_replay.active is False
    await wait_until(lambda: connection.session.trace.total > 0)
    assert replay.state == 'playing' and replay.file_name == 'replay.asc'
    assert replay.speeds[replay.speed_labels.index('1×')] == replay.speed == 1.0
    replay.set_paused(True)
    replay.refresh()
    assert replay.state == 'paused' and replay.paused
    replay.set_paused(False)
    replay.set_speed(0.0)
    await wait_until(lambda: connection.session.state is SessionState.FINISHED)
    await wait_until(lambda: connection.session.source.duration is not None)
    replay.refresh()
    assert replay.state == 'finished' and replay.frames == 30
    assert replay.duration == pytest.approx(0.58) and replay.position == pytest.approx(0.58)


async def test_bus_replay(manager, tmp_path):
    channel = f'gui-{uuid.uuid4().hex}'
    path = tmp_path / 'log.asc'
    with can.Logger(path) as logger:
        for i in range(4):
            logger.on_message_received(can.Message(arbitration_id=0x200 + i, timestamp=1.0 + i * 0.01))
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[0]
    bus_replay = connection.bus_replay
    url = QUrl.fromLocalFile(str(path))
    assert 'Connect' in bus_replay.start(url, 1.0, False, '')
    connection.open()
    assert 'Unsupported' in bus_replay.start(QUrl.fromLocalFile(str(tmp_path / 'log.txt')), 1.0, False, '')
    assert 'Invalid filter' in bus_replay.start(url, 1.0, False, 'bogus?')
    peer = can.Bus(interface='virtual', channel=channel)
    assert bus_replay.start(url, 0.0, False, '!203') == ''
    assert bus_replay.active and bus_replay.file_name == 'log.asc'
    await wait_until(lambda: bus_replay.state == 'finished')
    received = [peer.recv(timeout=1).arbitration_id for _ in range(3)]
    peer.shutdown()
    assert received == [0x200, 0x201, 0x202] and bus_replay.frames == 3
    await wait_until(lambda: connection.session.trace.total == 3)  # Shown as transmitted frames

    assert bus_replay.start(url, 1.0, True, '') == ''
    await wait_until(lambda: bus_replay.iteration >= 1)
    assert bus_replay.loop
    connection.close()  # Stops the replay
    await wait_until(lambda: bus_replay.state == 'stopped')
    bus_replay.dismiss()
    assert not bus_replay.active


async def test_database_editor_saves_and_reloads_connections(manager, data_dir, tmp_path):
    import shutil

    path = tmp_path / 'vehicle.dbc'
    shutil.copy(data_dir / 'example.dbc', path)
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'gui-{uuid.uuid4().hex}'})
    connection = manager.connections[0]
    connection.load_database(QUrl.fromLocalFile(str(path)))

    editor = manager.database_editor
    assert editor.open_path(str(path)) == '' and editor.file_name == 'vehicle.dbc' and editor.writable
    assert editor.current_message == 'EngineData' and editor.current_signal == 'EngineSpeed'
    assert editor.message['frame_id'] == '100' and editor.message['lengths'][-1] == '8'
    assert editor.layout[:16] == [0] * 16 and editor.signals[0]['byte_order'] == 'little_endian'
    assert editor.issues == []
    assert editor.update_message({'length': 3}) == ''  # Allowed, but the signals behind byte 3 are reported
    assert editor.issues == ['EngineData.GearPosition does not fit into 3 bytes'] and editor.messages[0]['issue']
    assert 'not possible' in editor.update_message({'length': 9})
    assert editor.update_message({'length': 8}) == '' and editor.issues == []
    assert editor.update_message({'name': 'Engine', 'cycle_time': '10'}) == ''
    assert editor.current_message == 'Engine' and editor.dirty
    assert editor.update_signal({'unit': 'U/min', 'choices': '0=Stopped'}) == ''
    assert editor.signals[0]['unit'] == 'U/min' and editor.signals[0]['choices'] == '0=Stopped'
    assert editor.add_signal() == '' and editor.current_signal == 'Signal1'
    assert editor.remove_signal() == ''
    assert 'name' in editor.update_signal({'name': '9lives'}).lower()

    assert editor.save() == '' and not editor.dirty
    decoder = connection.session.decoder.database
    assert [message.name for message in decoder.messages()][0] == 'Engine'  # The connection reloaded the file

    assert editor.add_message() == '' and editor.current_message == 'Message1'
    assert editor.remove_message() == '' and editor.current_message == 'VehicleSpeed'
    assert editor.revert() == '' and not editor.dirty
    assert 'Could not open' in editor.open_path(str(tmp_path / 'missing.dbc'))
    assert editor.open_path(str(data_dir / 'example.arxml')) == '' and not editor.writable
    assert 'Save as' in editor.save() and any('cannot be written' in note for note in editor.notes)
    editor.new_database()
    assert editor.file_name == 'Untitled' and editor.messages == []


async def test_tests_view_controller(manager, tmp_path):
    from pathlib import Path

    examples = Path(__file__).resolve().parents[3] / 'examples'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'gui-{uuid.uuid4().hex}'})
    connection = manager.connections[0]
    tests = connection.tests
    script = QUrl.fromLocalFile(str(examples / 'testing' / 'vehicle_tests.py'))
    assert tests.load_script(script) == ''
    assert [item['key'] for item in tests.items][:2] == ['EngineTests', 'EngineTests.engine_data_is_sent_every_100_ms']
    assert tests.current == 'EngineTests.engine_data_is_sent_every_100_ms' and tests.details['verdict'] == ''
    assert 'Connect' in tests.run_selected()

    connection.open()
    connection.load_database(QUrl.fromLocalFile(str(examples / 'simulation' / 'vehicle.dbc')))
    simulation = connection.simulation
    simulation.load_script(QUrl.fromLocalFile(str(examples / 'simulation' / 'engine_simulation.py')))
    assert simulation.start() == ''

    tests.select_all(False)
    assert tests.selected_count == 0 and 'Select' in tests.run_selected()
    tests.set_selected('GearboxTests', True)
    assert tests.selected_count == 2
    assert tests.run_selected() == '' and tests.running
    assert 'running already' in tests.run_case('EngineTests')
    await wait_until(lambda: not tests.running, timeout=10)
    summary = tests.summary
    assert (summary['total'], summary['passed'], summary['verdict']) == (2, 2, 'passed')
    tests.show_case('GearboxTests.obd_vehicle_speed_request')
    details = tests.details
    assert details['verdict'] == 'passed' and details['steps'][0]['kind'] == 'send'
    assert any(step['passed'] for step in details['steps'])

    html, xml = tmp_path / 'report.html', tmp_path / 'report.xml'
    assert 'written' in tests.export_report(QUrl.fromLocalFile(str(html))) and html.exists()
    assert 'written' in tests.export_report(QUrl.fromLocalFile(str(xml))) and xml.exists()
    assert 'Could not' in tests.export_report(QUrl.fromLocalFile(str(tmp_path / 'report.pdf')))

    assert tests.run_case('DiagnosticsTests') == ''
    connection.close()  # Stops the tests
    await wait_until(lambda: not tests.running)
    assert tests.summary['stopped'] and tests.summary['verdict'] == 'pending'
    assert tests.reload_scripts() == ''
    tests.remove_script(0)
    assert tests.items == []


async def test_trace_search_and_bookmarks(manager, tmp_path):
    from PyQt6.QtCore import QItemSelectionModel

    examples = Path(__file__).resolve().parents[3] / 'examples'
    path = tmp_path / 'trace.asc'
    database = open_signal_database(examples / 'simulation' / 'vehicle.dbc')
    engine = database.messages()[0]
    with can.Logger(path) as logger:
        for i in range(50):
            data = engine.encode({'EngineSpeed': i * 100, 'CoolantTemp': 20, 'GearPosition': 'First'})
            logger.on_message_received(
                can.Message(arbitration_id=0x100, is_extended_id=False, data=data, timestamp=i * 0.01)
            )
            logger.on_message_received(
                can.Message(
                    arbitration_id=0x7E8,
                    is_extended_id=False,
                    data=bytes([3, 0x41, 0x0D, i]),
                    timestamp=i * 0.01 + 0.001,
                )
            )
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 0.0)
    connection = manager.connections[0]
    connection.load_database(QUrl.fromLocalFile(str(examples / 'simulation' / 'vehicle.dbc')))
    await wait_until(lambda: connection.session.trace.total == 100)
    connection._refresh()
    tools = connection.trace_tools
    results = []
    tools.search_finished.connect(lambda row, message: results.append((row, message)))

    async def find(query, row=-1, forward=True):
        results.clear()
        tools.find(query, row, forward)
        await wait_until(lambda: results)
        return results[0]

    row, message = await find('EngineData.EngineSpeed >= 2500')
    assert row == 50 and message == 'Row 51 of 100'  # Frame 25 of EngineData, rows alternate
    assert (await find('7E8 data=03 41 0D 2A'))[0] == 2 * 42 + 1
    assert (await find('EngineSpeed=3000'))[0] == 60  # Text of the info column
    assert (await find('"0D"'))[0] == 1  # Quoted: text, not the identifier 0x0D
    row, message = await find('7E8', row=1, forward=False)
    assert row == 99 and 'wrapped' in message
    assert (await find('nothing like this'))[0] == -1 and tools.status == 'No match'

    selection = QItemSelectionModel(connection.trace)
    selection.select(
        connection.trace.index(4, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    )
    assert tools.toggle_bookmarks(selection) == 1 and tools.bookmark_count == 1
    tools.set_note(4, 'look here')
    bookmark = tools.bookmarks[0]
    assert (bookmark['row'], bookmark['note'], bookmark['id']) == (4, 'look here', '100')
    connection.set_filter('7E8')
    assert tools.row_of(4) == -1 and tools.row_of(5) == 2  # Filtered out; rows of the filtered view
    connection.set_filter('')
    tools.remove(4)
    assert tools.bookmark_count == 0


async def test_record_options_and_replay_from_row(manager, tmp_path):
    channel = f'gui-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[0]
    connection.open()
    events = []
    connection.recording_event.connect(events.append)
    target = QUrl.fromLocalFile(str(tmp_path / 'event.asc'))
    assert 'Invalid trigger' in connection.record(target, {'trigger': True, 'start': 'Engine.Speed >'})
    assert connection.record(target, {'trigger': True, 'start': '123', 'pre': 1, 'post': 0}) == ''
    assert connection.recording_triggered and connection.recording_status == 'armed: 123'
    peer = can.Bus(interface='virtual', channel=channel)
    for can_id in (0x100, 0x123, 0x101):
        peer.send(can.Message(arbitration_id=can_id, is_extended_id=False))
        await asyncio.sleep(0.02)
    peer.shutdown()
    await wait_until(lambda: connection.session.recorder.state == 'done')
    assert any('Trigger' in event for event in events) and 'recorded event.asc' in connection.recording_status
    connection.stop_recording()

    split = QUrl.fromLocalFile(str(tmp_path / 'split.log'))
    assert connection.record(split, {'split_mb': 0.001, 'split_minutes': 0}) == ''
    assert connection.recording_status == 'recording to split_001.log'
    connection.stop_recording()

    # Replaying from a row of the trace of a replay connection
    with can.Logger(tmp_path / 'replay.asc') as logger:
        for i in range(30):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=i * 0.1))
    manager.open_log_file(QUrl.fromLocalFile(str(tmp_path / 'replay.asc')), 'Raw', 0.0)
    replay = manager.connections[1]
    await wait_until(lambda: replay.session.state is SessionState.FINISHED)
    replay._refresh()
    replay.replay_from_row(20)  # The replay finished: it starts again, delivering frames 0..20 at once
    await wait_until(lambda: replay.session.trace.total >= 21)
    assert replay.session.state in (SessionState.OPEN, SessionState.FINISHED)
    await wait_until(lambda: replay.session.state is SessionState.FINISHED)
    assert replay.session.trace.total == 30
    assert [frame.message.arbitration_id for frame in replay.session.trace] == list(range(30))


async def test_workspace_round_trip(manager, tmp_path):
    import json
    import shutil

    examples = Path(__file__).resolve().parents[3] / 'examples'
    project = tmp_path / 'project'
    project.mkdir()
    for name in ('vehicle.dbc', 'engine_simulation.py', 'engine_panel.qml'):
        shutil.copy(examples / 'simulation' / name, project / name)
    shutil.copy(examples / 'testing' / 'vehicle_tests.py', project / 'vehicle_tests.py')
    with can.Logger(project / 'drive.asc') as logger:
        logger.on_message_received(can.Message(arbitration_id=0x100, timestamp=1.0))

    channel = f'workspace-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Vehicle', 'interface': 'VirtualBus', 'channel': channel, 'bitrate': 250000})
    vehicle = manager.connections[0]
    vehicle.open()
    vehicle.load_database(QUrl.fromLocalFile(str(project / 'vehicle.dbc')))
    vehicle.set_filter('100-1FF')
    vehicle.track_signal('EngineData', 'EngineSpeed', 'rpm')
    vehicle.simulation.load_script(QUrl.fromLocalFile(str(project / 'engine_simulation.py')))
    vehicle.simulation.add_panel(QUrl.fromLocalFile(str(project / 'engine_panel.qml')))
    assert vehicle.simulation.start() == ''
    vehicle.tests.load_script(QUrl.fromLocalFile(str(project / 'vehicle_tests.py')))
    manager.open_log_file(QUrl.fromLocalFile(str(project / 'drive.asc')), 'J1939', 2.0)

    workspace = project / 'bench.cxw'
    assert manager.save_workspace(QUrl.fromLocalFile(str(workspace)), 1) == ''
    data = json.loads(workspace.read_text())
    first = data['connections'][0]
    assert first['database'] == 'vehicle.dbc' and first['simulation']['scripts'] == ['engine_simulation.py']
    assert first['settings']['bitrate'] == 250000 and first['connected'] and first['simulation']['running']
    assert data['connections'][1]['path'] == 'drive.asc' and data['current'] == 1

    # The project moves elsewhere: relative paths still work
    moved = shutil.move(str(project), str(tmp_path / 'moved'))
    manager.close_all()
    problems = manager.open_workspace(QUrl.fromLocalFile(str(Path(moved) / 'bench.cxw')))
    assert problems == ''
    assert manager.workspace_name == 'bench' and manager.workspace_current() == 1
    vehicle, replay = manager.connections
    assert vehicle.name == 'Vehicle' and vehicle.connected and vehicle.filter_text == '100-1FF'
    assert Path(vehicle.database_file) == (Path(moved) / 'vehicle.dbc').resolve()
    assert vehicle.tracked_signals == ['EngineData.EngineSpeed']
    assert vehicle.simulation.running and [panel['name'] for panel in vehicle.simulation.panels] == ['engine_panel']
    assert [script['name'] for script in vehicle.tests.scripts] == ['vehicle_tests.py']
    assert replay.is_replay and replay.protocol == 'J1939' and replay.replay.speed == 2.0
    assert manager.app_state.recent_workspaces[0]['name'] == 'bench'

    # Missing files are reported; the rest opens
    (Path(moved) / 'engine_panel.qml').unlink()
    problems = manager.open_workspace(QUrl.fromLocalFile(str(Path(moved) / 'bench.cxw')))
    assert 'panel' in problems and 'not found' in problems and len(manager.connections) == 2
    assert 'not a CANExplorer workspace' in manager.open_workspace(QUrl.fromLocalFile(str(Path(moved) / 'vehicle.dbc')))
    manager.close_all()
