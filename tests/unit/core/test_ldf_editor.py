import shutil

import ldfparser
import pytest

from can_explorer.core.database_editor import EditError
from can_explorer.core.decoding import open_signal_database
from can_explorer.core.ldf_editor import LdfEditor
from can_explorer.core.ldf_writer import ldf_text, parse_ldf_text


@pytest.mark.parametrize('name', ['example.ldf', 'full.ldf'])
def test_writer_round_trip(data_dir, tmp_path, name):
    original = ldfparser.parse_ldf_to_dict(str(data_dir / name))
    text = ldf_text(original)
    assert parse_ldf_text(text) == original
    (tmp_path / name).write_text(text)
    assert ldfparser.parse_ldf(str(tmp_path / name)).get_unconditional_frames()


@pytest.fixture
def editor(data_dir, tmp_path):
    shutil.copy(data_dir / 'full.ldf', tmp_path / 'full.ldf')
    return LdfEditor(tmp_path / 'full.ldf')


def test_frames_and_signals(editor, tmp_path):
    assert editor.warnings == ['Comments of the file are not kept when it is saved']
    frame = editor.add_frame('Door_Frm2')
    assert editor.frame(frame)['frame_id'] == 0x00  # The first free identifier
    editor.update_frame(frame, frame_id='0x21', length=4, publisher='DoorLeft')
    signal = editor.add_signal(frame, 'WindowPosition')
    editor.update_signal(
        frame, signal, start=0, length=12, initial='0x7FF', subscribers='CEM, LSM', encoding='TemperatureEncoding'
    )
    flag = editor.add_signal(frame)
    assert dict(editor.bit_owners(frame))[flag] == list(range(16, 24))  # First free byte
    assert editor.signal(signal)['publisher'] == 'DoorLeft' and editor.signal_encoding(signal) == 'TemperatureEncoding'
    editor.update_signal(frame, flag, start=8, length=8)
    assert editor.issues() == ['Door_Frm2: Signal1 overlaps WindowPosition']
    editor.update_signal(frame, flag, start=16, name='Lock')
    assert editor.issues() == []

    editor.save(tmp_path / 'edited.ldf')
    database = open_signal_database(tmp_path / 'edited.ldf')
    definition = database.frame('Door_Frm2')
    assert definition.frame_id == 0x21 and definition.signal_names == ['WindowPosition', 'Lock']
    reloaded = LdfEditor(tmp_path / 'edited.ldf')
    assert reloaded.signal('WindowPosition')['init_value'] == 0x7FF and reloaded.warnings == []


def test_renames_follow_references(editor):
    editor.update_frame('RSM_Frm1', name='RSM_Status')
    assert editor.data['event_triggered_frames'][0]['frames'] == ['RSM_Status', 'LSM_Frm1']
    assert 'RSM_Status' in editor.node_attributes('RSM')['configurable_frames']
    assert 'RSM_Status delay 15 ms;' in editor.schedule_text('Collision_resolver')
    editor.update_signal('RSM_Frm2', 'RSMerror', name='RightError')
    assert editor.node_attributes('RSM')['response_error'] == 'RightError'
    assert editor.encoding_signals('ErrorEncoding') == ['RightError', 'LSMerror']
    editor.rename_node('RSM', 'RightSeat')
    assert editor.frame('RSM_Status')['publisher'] == 'RightSeat'
    assert 'AssignFrameId { RightSeat, CEM_Frm1 }' in editor.schedule_text('Configuration_Schedule')
    editor.rename_schedule('Collision_resolver', 'Resolver')
    assert editor.data['event_triggered_frames'][0]['collision_resolving_schedule_table'] == 'Resolver'
    editor.save()


def test_removals(editor):
    with pytest.raises(EditError, match='publishes'):
        editor.remove_node('LSM')
    editor.remove_frame('LSM_Frm1')
    assert all('LSM_Frm1' not in entry['frames'] for entry in editor.data.get('sporadic_frames', []))
    assert 'LSM_Frm1' not in editor.schedule_text('Collision_resolver')
    assert 'LeftIntLightsSwitch' not in [signal['name'] for signal in editor.data['signals']]
    with pytest.raises(EditError, match='resolves collisions'):
        editor.remove_schedule('Collision_resolver')
    editor.remove_encoding('Dig2Bit')
    assert editor.signal_encoding('InternalLightsRequest') == ''
    editor.save()


def test_text_editing_of_encodings_and_schedules(editor):
    editor.set_encoding_text('ErrorEncoding', 'logical_value, 0, "fine";\nlogical_value, 1, "broken";')
    assert editor.encoding('ErrorEncoding')['values'][1] == {'type': 'logical', 'value': 1, 'text': 'broken'}
    with pytest.raises(EditError, match='Invalid encoding'):
        editor.set_encoding_text('ErrorEncoding', 'logical_value 0 "x"')
    with pytest.raises(EditError, match='minimum is above'):
        editor.set_encoding_text('ErrorEncoding', 'physical_value, 10, 1, 1, 0;')
    editor.set_schedule_text(
        'Normal_Schedule', 'CEM_Frm1 delay 20 ms; MasterReq delay 10 ms; AssignNAD { LSM } delay 5 ms;'
    )
    assert len(editor.schedule('Normal_Schedule')['schedule']) == 3
    with pytest.raises(EditError, match="Unknown frame 'Nope'"):
        editor.set_schedule_text('Normal_Schedule', 'Nope delay 10 ms;')
    with pytest.raises(EditError, match="Unknown node 'Nobody'"):
        editor.set_schedule_text('Normal_Schedule', 'SaveConfiguration { Nobody } delay 10 ms;')
    editor.add_schedule('Diagnostics')
    assert 'Door_Frm1 delay 10 ms;' in editor.schedule_text('Diagnostics')
    editor.save()


def test_nodes_header_and_validation(editor, tmp_path):
    editor.add_node('DoorRight')
    editor.update_node_attributes(
        'DoorRight', configured_nad='0x23', initial_nad=0x23, supplier_id='0x1234', function_id=2, P2_min_ms=50
    )
    editor.set_master(timebase_ms=10, jitter_ms=0.5)
    editor.set_header(speed=10417, channel_name='Seats')
    for edit, message in (
        (lambda: editor.update_frame('CEM_Frm1', frame_id=0x3C), 'identifiers are 0 to 0x3B'),
        (lambda: editor.update_frame('CEM_Frm1', frame_id=0x02), 'used by LSM_Frm1'),
        (lambda: editor.update_frame('CEM_Frm1', length=9), '1 to 8 bytes'),
        (lambda: editor.update_signal('Door_Frm1', 'Temperature', initial=5000), 'does not fit into 12 bits'),
        (lambda: editor.update_signal('Door_Frm1', 'Serial', initial='1, 2'), 'needs 4 bytes'),
        (lambda: editor.update_signal('Door_Frm1', 'Temperature', subscribers='Ghost'), "No node 'Ghost'"),
        (lambda: editor.update_node_attributes('DoorRight', configured_nad=0x80), 'NADs are 1 to 0x7F'),
        (lambda: editor.set_header(speed=50000), '1 to 20 kbit/s'),
        (lambda: editor.add_node('2nd'), 'letters, digits'),
    ):
        with pytest.raises(EditError, match=message):
            edit()
    editor.save(tmp_path / 'nodes.ldf')
    ldf = ldfparser.parse_ldf(str(tmp_path / 'nodes.ldf'))
    assert ldf.get_baudrate() == 10417 and ldf.get_master().timebase == pytest.approx(0.01)
    assert ldf.get_slave('DoorRight').configured_nad == 0x23


def test_new_database(tmp_path):
    editor = LdfEditor()
    editor.add_node('Slave1')
    frame = editor.add_frame()
    editor.add_signal(frame)
    editor.add_schedule('Main')
    with pytest.raises(EditError, match='.ldf'):
        editor.save(tmp_path / 'new.dbc')
    editor.save(tmp_path / 'new.ldf')
    assert open_signal_database(tmp_path / 'new.ldf').frame('Frame1').signal_names == ['Signal1']
