import asyncio
import uuid

import can
import pytest
from PyQt6.QtCore import QUrl

from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.markers import Marker
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


@pytest.fixture
def channel():
    return f'reverse-{uuid.uuid4().hex}'


@pytest.fixture
def peer(channel):
    bus = can.Bus(interface='virtual', channel=channel)
    yield bus
    bus.shutdown()


def open_connection(manager, channel):
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    return connection


def send(peer, can_id, data, extended=False):
    peer.send(can.Message(arbitration_id=can_id, is_extended_id=extended, data=bytes(data)))


async def test_statistics_start_with_the_page_and_follow_the_bus(manager, channel, peer):
    connection = open_connection(manager, channel)
    for i in range(10):
        send(peer, 0x100, [i, 0x55, 0x34, 0x12])
    send(peer, 0x18FEF100, [1, 2], extended=True)
    await wait_until(lambda: connection.session.trace.total == 11)
    tools = connection.reverse
    assert not tools.active and tools.identifiers == []

    tools.set_visible(True)  # The page is shown: the frames of the trace so far count
    assert tools.active and tools.selected == '100'
    assert [(item['id'], item['count'], item['length']) for item in tools.identifiers] == [
        ('100', 10, 4),
        ('18FEF100', 1, 2),
    ]
    bits = tools.bits
    assert len(bits) == 32 and [bit['bit'] for bit in bits[:8]] == [7, 6, 5, 4, 3, 2, 1, 0]
    assert bits[7]['flips'] > 0 and bits[8]['flips'] == 0 and bits[8]['value'] == 0  # 0x55: bit 15 is 0
    assert [byte['value'] for byte in tools.bytes] == ['09', '55', '34', '12'] and tools.bytes[0]['distinct'] == 10
    assert tools.last_data == '09 55 34 12'

    send(peer, 0x100, [10, 0x55, 0x34, 0x12])  # Later frames are counted live
    await wait_until(lambda: tools.identifiers[0]['count'] == 11)
    tools.notch()
    send(peer, 0x100, [10, 0x54, 0x34, 0x12])
    send(peer, 0x123, [0])
    await wait_until(lambda: len(tools.identifiers) == 3)
    by_id = {item['id']: item for item in tools.identifiers}
    assert by_id['100']['hits'] == 1 and by_id['123']['new'] and not by_id['100']['new']
    assert [bit['bit'] for bit in tools.bits if bit['hit']] == [8]

    connection.clear()
    assert tools.identifiers == [] and not tools.notched
    tools.set_visible(False)


async def test_bit_field_plot_and_database(manager, channel, peer, data_dir):
    connection = open_connection(manager, channel)
    for raw in (1000, 2000, 3000):
        send(peer, 0x100, [0, 0, raw >> 8, raw & 0xFF])
    await wait_until(lambda: connection.session.trace.total == 3)
    tools = connection.reverse
    tools.set_visible(True)
    tools.set_field({'byte_order': 'big_endian'})
    tools.select_bits(16, 31)  # Byte 2 bit 0 and byte 3 bit 7 are neighbours in Motorola order
    assert (tools.field['start'], tools.field['length']) == (16, 2)
    tools.select_bits(24, 23)  # Byte 3 bit 0 to byte 2 bit 7, clicked in the grid
    assert (tools.field['start'], tools.field['length']) == (23, 16) and tools.field_key == '100[23|16@0+]'
    tools.set_field({'scale': '0.25', 'unit': 'rpm', 'name': 'Speed'})
    assert tools.field_value == '750 rpm (raw 3000, 0xBB8)'
    assert {bit['bit'] for bit in tools.bits if bit['field']} == set(range(16, 32))
    tools.set_field({'length': 'many'})
    assert tools.field_error and tools.field['length'] == 16
    tools.set_field({'length': 16})
    assert tools.field_error == ''

    # Plotting includes the frames received before
    assert tools.plot_field() == ''
    key = '100[23|16@0+]'
    assert connection.tracked_signals == [key]
    assert connection.session.signals.series[key].values == [250.0, 500.0, 750.0]
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    connection.unload_database()  # Fields stay plotted without a database
    assert connection.tracked_signals == [key]
    connection.untrack_key(key)
    assert connection.tracked_signals == []

    # A new database with the message and the signal
    editor = manager.database_editor
    assert editor.add_found_signal(tools.signal_definition, '') == ''
    assert (editor.current_message, editor.current_signal) == ('Message_100', 'Speed')
    signal = editor._editor.signal('Message_100', 'Speed')
    assert (signal.start, signal.length, signal.byte_order, signal.scale, signal.unit) == (
        23,
        16,
        'big_endian',
        0.25,
        'rpm',
    )
    assert editor._editor.message('Message_100').length == 4 and editor.dirty
    # The same name again is refused, without leaving a signal behind
    assert 'has a signal named Speed' in editor.add_found_signal(tools.signal_definition, '')
    assert [signal.name for signal in editor._editor.message('Message_100').signals] == ['Speed']


def test_add_found_signal_to_existing_messages(manager, data_dir):
    editor = manager.database_editor
    definition = {
        'frame_id': 0x100,
        'extended': False,
        'frame_length': 8,
        'name': 'Found',
        'start': 40,
        'length': 8,
        'byte_order': 'little_endian',
        'is_signed': False,
        'scale': 1.0,
        'offset': 0.0,
        'unit': '',
    }
    # Without an open database, the connection's database is opened
    assert editor.add_found_signal(definition, str(data_dir / 'example.dbc')) == ''
    assert editor.file_name == 'example.dbc' and editor.current_message == 'EngineData'
    assert 'Found' in [signal.name for signal in editor._editor.message('EngineData').signals]
    # A longer frame makes the message CAN FD with the next possible length
    assert editor.add_found_signal({**definition, 'name': 'Far', 'frame_length': 10, 'start': 72}, '') == ''
    assert editor._editor.message('EngineData').length == 12 and editor._editor.message('EngineData').is_fd
    # Extended identifiers get messages of their own
    assert editor.add_found_signal({**definition, 'frame_id': 0x100, 'extended': True}, '') == ''
    assert editor.current_message == 'Message_00000100'
    assert editor.add_found_signal({'frame_id': 'x'}, '').startswith('Invalid signal')

    editor.new_lin_database()
    assert 'LIN database' in editor.add_found_signal(definition, '')


def test_plotted_fields_are_saved_in_workspaces(manager, tmp_path):
    channel = f'reverse-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    field = BitField(0x1A0, False, 7, 12, 'big_endian', signed=True, scale=0.1, offset=-5)
    connection.track_field(field, 'bar')
    workspace = tmp_path / 'bench.cxw'
    assert manager.save_workspace(QUrl.fromLocalFile(str(workspace)), 0) == ''
    assert manager.open_workspace(QUrl.fromLocalFile(str(workspace))) == ''
    restored = manager.connections[-1].session.signals
    assert restored.fields == {field.key: field} and restored.series[field.key].unit == 'bar'


# The analysis tools


async def finished(tool):
    await wait_until(lambda: not tool.running, timeout=20)


async def replay(manager, data_dir, tmp_path, seed=0):
    """A replay connection with synthetic traffic of reverse.dbc (checksum, counter, multiplexed cells, …)."""
    from can_explorer.core.decoding import open_signal_database
    from can_explorer.core.reverse.synthetic import Behaviour, generate

    def full(period):  # Different periods, so the signals do not correlate
        return Behaviour('sine', period=period, amplitude=0.95)

    behaviours = {
        'Powertrain.Checksum': Behaviour('checksum'),
        'Powertrain.Counter': Behaviour('counter'),
        'Powertrain.EngineSpeed': full(20),
        'Body.VehicleSpeed': full(27),
        'Body.DoorOpen': Behaviour('step', at=10),
    }
    for group in range(3):
        behaviours[f'Battery.CellA{group}'] = full(7 + 2 * group)
    traffic = generate(open_signal_database(data_dir / 'reverse.dbc'), 30, behaviours, seed=seed)
    path = tmp_path / 'traffic.asc'
    traffic.write(path)
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 0.0)
    connection = manager.connections[-1]
    await wait_until(lambda: connection.state == 'finished', timeout=10)
    return connection, traffic


async def test_automatic_analysis(manager, data_dir, tmp_path):
    connection, _ = await replay(manager, data_dir, tmp_path)
    analysis = connection.reverse.analysis
    analysis.run(False)
    assert analysis.running
    await finished(analysis)
    rows = analysis.results
    assert 'fields in 3 identifiers' in analysis.status
    by_key = {(row['key'], row['condition']): row for row in rows}
    assert by_key[('123[0|8@1+]', '')]['kind_name'] == 'Checksum'
    counter = by_key[('123[8|4@1+]', '')]
    assert (counter['kind'], counter['confidence'], counter['id']) == ('counter', 100, '123')
    cell = by_key[('600[8|16@1+]', 'if 600[0|2@1+] = 1')]
    assert cell['kind'] == 'physical'

    # Use: the field in the form, with its multiplexer
    reverse = connection.reverse
    analysis.use(cell['index'])
    assert reverse.selected == '600' and reverse.field_key == '600[8|16@1+]'
    assert reverse.field_multiplexer == '600[0|2@1+] = 1' and reverse.field['name'] == 'Value_600_8'
    assert 'Multiplexed' in reverse.signal_definition['comment']
    assert reverse.plot_field() == ''
    key = '600[8|16@1+] if 600[0|2@1+]=1'
    assert key in connection.tracked_signals
    values = connection.session.signals.series[key].values
    assert values and max(abs(b - a) for a, b in zip(values, values[1:], strict=False)) < 5000  # One cell only
    reverse.clear_multiplexer()
    assert reverse.field_multiplexer == '' and reverse.signal_definition['comment'] == ''
    assert analysis.plot(counter['index']) == '' and '123[8|4@1+]' in connection.tracked_signals
    assert analysis.plot(10_000) == 'Nothing to plot'

    # Only the selected identifier: the others' results stay
    reverse.select('500')
    analysis.run(True)
    await finished(analysis)
    assert {row['id'] for row in analysis.results} == {'123', '500', '600'}
    assert 'in 1 identifiers' in analysis.status

    connection.clear()  # The results refer to the cleared frames
    assert analysis.results == [] and reverse.analyses == []


async def test_reference_signals(manager, data_dir, tmp_path):
    connection, traffic = await replay(manager, data_dir, tmp_path)
    reference = connection.reverse.reference
    start = traffic.messages[0].timestamp
    csv = tmp_path / 'gps.csv'
    rows = [f'{time - start:.3f},{raw * 0.01:.2f}' for time, raw in traffic.raw['Body.VehicleSpeed'][::25]]
    csv.write_text('time,speed [km/h]\n' + '\n'.join(rows))
    assert reference.open_csv(QUrl.fromLocalFile(str(csv))) == ''
    assert reference.sources[0] == {'key': 'csv:0', 'name': 'speed (gps.csv)', 'unit': 'km/h', 'group': 'CSV'}
    reference.find('csv:0', 0.5)
    await finished(reference)
    best = reference.matches[0]
    assert (best['key'], best['scale'], best['lag']) == ('500[7|16@0+]', '0.01', '0 ms')
    assert best['correlation'] > 0.99
    reference.use(0)
    reverse = connection.reverse
    assert (reverse.field['scale'], reverse.field['unit'], reverse.field['name']) == (0.01, 'km/h', 'Speed')
    assert reference.plot(0) == '' and '500[7|16@0+]' in connection.tracked_signals

    # A database signal: nothing else carries the engine speed
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'reverse.dbc')))
    assert 'signal:Powertrain.EngineSpeed' in [source['key'] for source in reference.sources]
    reference.find('signal:Powertrain.EngineSpeed', 0.2)
    await finished(reference)
    assert 'No field follows Powertrain.EngineSpeed closely' in reference.status
    reference.find('csv:7', 0.2)
    await finished(reference)
    assert reference.status == 'Choose a reference with values'
    assert 'no numeric columns' in reference.open_csv(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    # A replay cannot poll OBD-II
    assert reference.start_obd([0x0D], 50, False) == 'Connect to a bus that can transmit first'


async def test_obd_polling(manager, channel, peer):
    connection = open_connection(manager, channel)
    reference = connection.reverse.reference
    assert reference.start_obd([], 50, False) == 'Choose at least one value'
    assert reference.start_obd([0x0D], 20, False) == ''
    assert reference.obd_running

    # An ECU answering the vehicle speed
    from can_explorer.core.reverse.obd import response

    speed = 0
    for _ in range(300):
        msg = peer.recv(0)
        while msg is not None:
            if msg.arbitration_id == 0x7DF and msg.data[2] == 0x0D:
                speed += 1
                peer.send(response(0x0D, bytes([speed])))
            msg = peer.recv(0)
        await asyncio.sleep(0.005)
        if reference.obd_answers(0x0D) >= 5:
            break
    assert reference.obd_answers(0x0D) >= 5
    assert 'answers' in reference.obd_status
    assert {'key': 'obd:13', 'name': 'OBD Vehicle speed', 'unit': 'km/h', 'group': 'OBD-II'} in reference.sources
    reference.stop_obd()
    assert not reference.obd_running
    connection.close()


async def test_markers(manager, data_dir, tmp_path):
    connection, traffic = await replay(manager, data_dir, tmp_path)
    markers = connection.reverse.markers
    # The door opened at 10 s; marked at the time of a frame just after it
    door_time = next(msg.timestamp for msg in traffic.messages if msg.timestamp >= traffic.messages[0].timestamp + 10)
    markers._markers.append(Marker(door_time + 0.2, 'door'))
    assert markers.add('end') == ''  # At the last frame
    assert [marker['label'] for marker in markers.markers] == ['door', 'end'] and markers.labels == ['door', 'end']
    markers.find('door', 0.5, 1.0)
    await finished(markers)
    best = markers.results[0]
    assert (best['key'], best['hits'], best['transitions']) == ('500[32|1@1+]', '1/1', '0 → 1')
    markers.use(0)
    assert connection.reverse.field_key == '500[32|1@1+]' and connection.reverse.field['name'] == 'Event_500_32'
    assert markers.plot(0) == ''
    markers.remove(1)
    assert len(markers.markers) == 1
    markers.find('nothing', 0.5, 1.0)
    await finished(markers)
    assert markers.status == 'Set markers first'
    markers.clear()
    assert markers.markers == [] and markers.results == []


async def test_markers_need_frames(manager, channel):
    connection = open_connection(manager, channel)
    assert connection.reverse.markers.add('x') == 'No frames yet'


def test_multiplexed_fields_are_saved_in_workspaces(manager, tmp_path):
    channel = f'reverse-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    field, multiplexer = BitField(0x600, False, 8, 16), BitField(0x600, False, 0, 2)
    key = connection.track_field(field, 'V', multiplexer, 2)
    workspace = tmp_path / 'bench.cxw'
    assert manager.save_workspace(QUrl.fromLocalFile(str(workspace)), 0) == ''
    assert manager.open_workspace(QUrl.fromLocalFile(str(workspace))) == ''
    restored = manager.connections[-1].session.signals
    assert restored.multiplexers == {key: (multiplexer, 2)} and restored.fields[key] == field
