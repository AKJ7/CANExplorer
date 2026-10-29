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


async def test_promote_analysis_results_to_a_database(manager, data_dir, tmp_path):
    connection, _ = await replay(manager, data_dir, tmp_path)
    analysis = connection.reverse.analysis
    analysis.run(False)
    await finished(analysis)

    definitions = analysis.found_signals
    assert definitions  # Every found field except constant bits, as signal definitions
    # Promote in one step into a new database; overlapping interpretations are resolved, not rejected
    editor = manager.database_editor
    summary = editor.add_found_signals(definitions, '')
    assert summary.startswith(f'Promoted {len(definitions)} signal')
    assert len(editor.messages) == len({(d['frame_id'], d['extended']) for d in definitions})

    # The result is a valid database that saves and reloads as a DBC
    out = tmp_path / 'reversed.dbc'
    assert editor.save_as(QUrl.fromLocalFile(str(out))) == ''
    import cantools

    reloaded = cantools.database.load_file(str(out))
    assert sum(len(message.signals) for message in reloaded.messages) == len(definitions)

    assert manager.database_editor.add_found_signals([], '') == 'No signals to promote'


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


async def test_bits_are_cached_and_invalidated_on_selection(manager, channel, peer):
    """The QML grid reads `bits` once per byte row; it must be computed once per change (a naive recompute froze
    the GUI on wide CAN FD frames), and switching identifiers must invalidate it so the length is always right."""
    connection = open_connection(manager, channel)
    for i in range(20):
        send(peer, 0x300, [i, i, i, i, i, i, i, i])  # 8 bytes -> 64 bits
        send(peer, 0x400, [i, i])  # 2 bytes -> 16 bits
    await wait_until(lambda: connection.session.trace.total == 40)
    tools = connection.reverse
    tools.set_visible(True)
    tools._timer.stop()  # No periodic refresh interfering with the identity check

    tools.select('300')
    first = tools.bits
    assert len(first) == 64
    assert tools.bits is first  # Cached: not rebuilt on every access

    tools.select('400')  # Switching must invalidate the cache, not return the stale 64-bit list
    assert len(tools.bits) == 16
    tools.select('300')
    assert len(tools.bits) == 64


async def test_selecting_an_identifier_does_not_reset_the_id_list(manager, channel, peer):
    """Selecting must not re-notify the identifier list, or Qt would reset that list's model from inside its own
    delegate's onClicked (which called select), destroying the delegate mid-handler and crashing."""
    connection = open_connection(manager, channel)
    for i in range(10):
        send(peer, 0x100, [i])
        send(peer, 0x200, [i, i])
    await wait_until(lambda: connection.session.trace.total == 20)
    tools = connection.reverse
    tools.set_visible(True)
    tools._timer.stop()

    fired = {'identifiers': 0, 'changed': 0}
    tools.identifiers_changed.connect(lambda: fired.__setitem__('identifiers', fired['identifiers'] + 1))
    tools.changed.connect(lambda: fired.__setitem__('changed', fired['changed'] + 1))
    before = tools.identifiers

    tools.select('200')
    assert fired['identifiers'] == 0  # The id list is not reset by a selection
    assert fired['changed'] >= 1  # But the bit grid of the selected id does update
    assert tools.identifiers == before


async def test_id_list_updates_in_place_without_resetting(manager, channel, peer):
    """The id list must not be rebuilt on every refresh — that destroyed the delegates under the cursor, so a
    click landing during a refresh was lost and could crash. Stat updates change rows in place; new ids insert."""
    connection = open_connection(manager, channel)
    for i in range(10):
        send(peer, 0x100, [i])
    await wait_until(lambda: connection.session.trace.total == 10)
    tools = connection.reverse
    tools.set_visible(True)
    tools._timer.stop()
    model = tools.identifier_model
    resets, inserts, changes = [], [], []
    model.modelAboutToBeReset.connect(lambda: resets.append(1))
    model.rowsInserted.connect(lambda *a: inserts.append(1))
    model.dataChanged.connect(lambda *a: changes.append(1))
    assert model.rowCount() == 1

    for i in range(10, 20):  # More frames of the same id: rows update in place
        send(peer, 0x100, [i])
    await wait_until(lambda: connection.session.trace.total == 20)
    tools._refresh()
    assert changes and not resets and not inserts

    send(peer, 0x200, [1, 2])  # A new id appears: it is inserted, still no full reset
    await wait_until(lambda: connection.session.trace.total == 21)
    tools._refresh()
    assert inserts and not resets and model.rowCount() == 2


async def test_id_list_filtering(manager, channel, peer):
    connection = open_connection(manager, channel)
    for i in range(5):
        send(peer, 0x100, [i])
        send(peer, 0x201, [i])
        send(peer, 0x202, [i])
    await wait_until(lambda: connection.session.trace.total == 15)
    tools = connection.reverse
    tools.set_visible(True)
    tools._timer.stop()
    model = tools.identifier_model
    assert model.rowCount() == 3
    tools.set_id_search('20')
    assert model.rowCount() == 2  # 201 and 202
    tools.set_id_search('')
    assert model.rowCount() == 3


async def test_timeline_replay_scrub_and_playback(manager, channel, peer):
    """Replay the selected identifier's frames: the bit grid and bytes follow the scrub position, and playback
    advances it."""
    connection = open_connection(manager, channel)
    for i in range(6):
        send(peer, 0x100, [i, 0x80 if i >= 3 else 0x00])  # byte 0 counts up; bit 15 flips on at frame 3
    await wait_until(lambda: connection.session.trace.total == 6)
    tools = connection.reverse
    tools.set_visible(True)
    tools.select('100')
    timeline = tools.timeline
    assert not timeline.active

    tools.start_replay()
    await wait_until(lambda: timeline.active)
    assert timeline.frames == 6

    # Scrub to the first frame: byte 0 == 0, bit 15 (byte 1 bit 7) is 0
    timeline.set_position(0.0)
    assert timeline.frame == 1 and tools.last_data == '00 00'
    bits = {b['bit']: b['value'] for b in tools.bits}
    assert bits[15] == 0 and tools.bytes[0]['value'] == '00'

    # Scrub to the end: byte 0 == 5, bit 15 is now 1
    timeline.set_position(1.0)
    assert timeline.frame == 6 and tools.last_data == '05 80'
    bits = {b['bit']: b['value'] for b in tools.bits}
    assert bits[15] == 1 and tools.bytes[0]['value'] == '05'

    # Stepping back one frame
    timeline.step(-1)
    assert timeline.frame == 5 and tools.last_data == '04 80'

    # Playback advances the position and pauses at the end. The QTimer that drives it fires on the Qt event loop
    # (qasync in the app); under the asyncio test loop it is not pumped, so the tick is invoked directly.
    timeline.set_position(0.0)
    timeline.set_speed(10.0)
    timeline.play()
    assert timeline.playing and timeline.frame == 1
    await asyncio.sleep(0.05)
    timeline._tick()
    assert not timeline.playing and timeline.frame == 6  # Reached the end

    # Leaving replay returns the grid to the live latest frame
    tools.stop_replay()
    assert not timeline.active and tools.last_data == '05 80'
    # Selecting another identifier drops the timeline
    tools.start_replay()
    await wait_until(lambda: timeline.active)
    send(peer, 0x200, [1, 2, 3])
    await wait_until(lambda: any(item['id'] == '200' for item in tools.identifiers))
    tools.select('200')
    assert not timeline.active
    tools.set_visible(False)


def test_timeline_scrubs_by_index_when_all_timestamps_equal(qapp):
    """A capture whose frames share a timestamp (a coarse clock, or a tight loop) has zero duration; scrubbing
    must still map the slider to the first/last frame instead of always landing on the last."""
    from types import SimpleNamespace

    from can_explorer.core.reverse.timeline import IdTimeline
    from can_explorer.gui.reverse_timeline import ReverseTimeline

    reverse = SimpleNamespace(session=SimpleNamespace(start_time=None))
    timeline = ReverseTimeline(reverse)
    timeline.enable(IdTimeline([5.0] * 4, [bytes([i]) for i in range(4)]))
    assert timeline.active and timeline.frames == 4 and timeline.timeline.duration == 0.0

    timeline.set_position(0.0)
    assert timeline.frame == 1 and timeline.payload() == b'\x00'
    timeline.set_position(1.0)
    assert timeline.frame == 4 and timeline.payload() == b'\x03'
    timeline.step(-1)
    assert timeline.frame == 3 and timeline.payload() == b'\x02'

    # Playback over a zero-duration capture jumps to the end and pauses
    timeline.set_position(0.0)
    timeline.play()
    timeline._tick()
    assert not timeline.playing and timeline.frame == 4
