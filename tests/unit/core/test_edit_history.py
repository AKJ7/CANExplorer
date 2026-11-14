import shutil

import pytest
from cantools.database.can import Database, Message, Signal

from can_explorer.core.database_editor import DatabaseEditor, EditError, signal_bits
from can_explorer.core.edit_history import EditHistory
from can_explorer.core.ldf_editor import LdfEditor


@pytest.fixture
def editor(data_dir, tmp_path):
    path = tmp_path / 'complex.dbc'
    shutil.copy(data_dir / 'complex.dbc', path)
    return DatabaseEditor(path)


def step(editor, edit):
    with editor.edit_step():
        return edit()


# The history


def test_history_stacks():
    history = EditHistory(limit=3)
    assert not history.can_undo and not history.can_redo and history.undo() is None
    for number in range(5):
        history.push(number, number + 1)
    assert history.undo() == (4, 5) and history.undo() == (3, 4) and history.undo() == (2, 3)
    assert history.undo() is None  # Only the last 3 steps are kept
    assert history.redo() == (2, 3) and history.can_redo
    history.push('a', 'b')  # A new step drops the undone ones
    assert not history.can_redo
    history.clear()
    assert not history.can_undo


# CAN editor


def test_undo_and_redo_restore_each_step(editor):
    original = editor.message('EngineData').comment
    step(editor, lambda: editor.update_message('EngineData', comment='first'))
    step(editor, lambda: editor.update_signal('EngineData', 'Torque', unit='kNm'))
    step(editor, lambda: editor.remove_message('FloatData'))
    assert [m.name for m in editor.messages].count('FloatData') == 0
    assert editor.undo()
    assert 'FloatData' in [m.name for m in editor.messages]
    assert editor.undo()
    assert editor.signal('EngineData', 'Torque').unit == 'Nm'
    assert editor.message('EngineData').comment == 'first'
    assert editor.undo() and editor.message('EngineData').comment == original
    assert not editor.undo()
    assert editor.redo() and editor.redo()
    assert editor.message('EngineData').comment == 'first' and editor.signal('EngineData', 'Torque').unit == 'kNm'


def test_steps_on_different_messages_undo_independently(editor):
    """Each recorded state keeps its own message objects: a later step never changes what an earlier one restores."""
    step(editor, lambda: editor.update_message('EngineData', cycle_time=10))
    step(editor, lambda: editor.update_message('VehicleSpeed', cycle_time=20))
    step(editor, lambda: editor.update_message('EngineData', cycle_time=30))
    editor.undo()
    editor.undo()
    assert editor.message('EngineData').cycle_time == 10 and editor.message('VehicleSpeed').cycle_time is None
    editor.undo()
    assert editor.message('EngineData').cycle_time is None
    editor.redo()
    editor.redo()
    editor.redo()
    assert editor.message('EngineData').cycle_time == 30 and editor.message('VehicleSpeed').cycle_time == 20


def test_a_step_copies_only_the_messages_it_changes(editor):
    before = {message.name: message for message in editor.messages}
    step(editor, lambda: editor.update_signal('EngineData', 'Torque', unit='kNm'))
    after = {message.name: message for message in editor.messages}
    assert after['EngineData'] is not before['EngineData']  # Changed through a copy...
    assert before['EngineData'].signals[1].unit == 'Nm'  # ... the recorded original is untouched
    assert all(after[name] is before[name] for name in before if name != 'EngineData')


def test_several_changes_in_one_step_are_one_undo(editor):
    def edit():
        name = editor.add_message('NewMessage', 0x123)
        editor.add_signal(name, 'Value')
        editor.update_signal(name, 'Value', length=12)

    step(editor, edit)
    assert editor.signal('NewMessage', 'Value').length == 12
    editor.undo()
    assert 'NewMessage' not in [m.name for m in editor.messages]


def test_a_failing_step_is_rolled_back_and_not_recorded(editor):
    original = editor.message('EngineData').comment

    def edit():
        editor.update_message('EngineData', comment='partial')
        editor.update_signal('EngineData', 'Torque', length=99)  # Invalid

    with pytest.raises(EditError):
        step(editor, edit)
    assert editor.message('EngineData').comment == original
    assert not editor.history.can_undo and not editor.dirty


def test_a_step_that_changes_nothing_is_not_recorded(editor):
    step(editor, lambda: editor.update_signal('EngineData', 'Torque', unit='Nm', scale='0.5'))
    assert not editor.history.can_undo and not editor.dirty


def test_unsaved_changes_follow_undo(editor, tmp_path):
    step(editor, lambda: editor.update_message('EngineData', comment='saved'))
    assert editor.dirty
    editor.save()
    assert not editor.dirty and editor.history.can_undo  # Saving keeps the history
    editor.undo()
    assert editor.dirty  # Different from the file again
    editor.redo()
    assert not editor.dirty
    step(editor, lambda: editor.update_message('EngineData', comment='other'))
    step(editor, lambda: editor.update_message('EngineData', comment='saved'))  # Edited back to the saved content
    assert not editor.dirty


def test_removing_a_node_is_undone_in_every_message(editor):
    step(editor, lambda: editor.remove_node('Dashboard'))
    assert 'Dashboard' not in editor.signal('VehicleSpeed', 'WheelSpeed').receivers
    editor.undo()
    assert 'Dashboard' in editor.node_names
    assert editor.signal('VehicleSpeed', 'WheelSpeed').receivers == ['Dashboard']
    assert editor.signal('EngineData', 'EngineSpeed').receivers == ['Gateway', 'Dashboard']


def test_changed_message_names_what_a_step_changed(editor):
    first = editor.state()
    step(editor, lambda: editor.update_message('VehicleSpeed', cycle_time=50))
    assert editor.changed_message(first, editor.state()) == 'VehicleSpeed'
    second = editor.state()
    step(editor, lambda: editor.add_message('Added', 0x321))
    assert editor.changed_message(second, editor.state()) == 'Added'
    assert editor.changed_message(editor.state(), editor.state()) is None


def test_new_database_undo(tmp_path):
    editor = DatabaseEditor()
    assert not editor.dirty
    step(editor, lambda: editor.add_message())
    assert editor.dirty
    editor.undo()
    assert not editor.dirty and editor.messages == []


# Duplicating and copying


def test_duplicate_message(editor):
    name = step(editor, lambda: editor.duplicate_message('EngineData'))
    assert name == 'EngineData_copy'
    names = [message.name for message in editor.messages]
    assert names.index(name) == names.index('EngineData') + 1  # Right after the original
    copy, original = editor.message(name), editor.message('EngineData')
    assert copy.frame_id == 0x101  # The next free identifier after 0x100
    assert [s.name for s in copy.signals] == [s.name for s in original.signals] and copy.senders == ['Engine']
    step(editor, lambda: editor.update_signal(name, 'Torque', unit='kNm'))
    assert editor.signal('EngineData', 'Torque').unit == 'Nm'  # Independent of the original
    assert step(editor, lambda: editor.duplicate_message('EngineData')) == 'EngineData_copy2'
    assert editor.message('EngineData_copy2').frame_id == 0x102
    editor.undo()
    editor.undo()
    editor.undo()
    assert 'EngineData_copy' not in [m.name for m in editor.messages]


def test_duplicate_an_extended_message_keeps_its_format(editor):
    name = step(editor, lambda: editor.duplicate_message('VehicleSpeed'))
    copy = editor.message(name)
    assert copy.is_extended_frame and copy.frame_id == 0x18FEF1FF


def test_copy_signal_into_the_same_message_goes_to_free_bits(editor):
    name = step(editor, lambda: editor.copy_signal('FloatData', 'Ratio'))
    assert name == 'Ratio_copy'
    copy = editor.signal('FloatData', name)
    assert copy.start == 48 and copy.length == 16  # Bits 48-63 are the free ones
    assert editor.message_issues(editor.message('FloatData')) == []


def test_copy_a_motorola_signal_keeps_it_inside_the_message(editor):
    name = step(editor, lambda: editor.copy_signal('EngineData', 'Temperature', 'FloatData'))
    copy = editor.signal('FloatData', name)
    assert name == 'Temperature' and copy.byte_order == 'big_endian'
    used = {bit for other in editor.message('FloatData').signals if other is not copy for bit in signal_bits(other)}
    assert not used & set(signal_bits(copy)) and all(0 <= bit < 64 for bit in signal_bits(copy))


def test_copy_signal_without_room_overlaps_and_reports_it(editor):
    name = step(editor, lambda: editor.copy_signal('FloatData', 'Pressure'))  # 32 bits, 16 free
    assert editor.signal('FloatData', name).start == 0
    assert any('overlaps' in issue for issue in editor.message_issues(editor.message('FloatData')))


def test_copy_signal_multiplexing(editor):
    # A multiplexed signal into a message without its switch becomes a plain signal
    name = step(editor, lambda: editor.copy_signal('Multiplexed', 'ValueA', 'FloatData'))
    copy = editor.signal('FloatData', name)
    assert copy.multiplexer_ids is None and copy.multiplexer_signal is None
    # Within its message it keeps following the switch; a second switch becomes a plain signal
    name = step(editor, lambda: editor.copy_signal('Multiplexed', 'ValueA'))
    assert editor.signal('Multiplexed', name).multiplexer_signal == 'Mux'
    name = step(editor, lambda: editor.copy_signal('Multiplexed', 'Mux'))
    assert not editor.signal('Multiplexed', name).is_multiplexer


def test_paste_a_signal_copied_earlier(editor):
    copied = editor.signal('EngineData', 'Torque')
    step(editor, lambda: editor.remove_message('EngineData'))  # The source is gone; the copy still pastes
    name = step(editor, lambda: editor.add_signal_copy('FloatData', copied))
    assert name == 'Torque' and editor.signal('FloatData', 'Torque').unit == 'Nm'


def test_a_step_on_a_large_database_copies_one_message():
    """A full copy of a 1500 message database takes about a second; an edit step must not make one."""
    editor = DatabaseEditor()
    database: Database = editor.database
    for index in range(1500):
        signals = [Signal(f'S{bit}', bit * 4, 4) for bit in range(16)]
        database.messages.append(Message(0x100 + index, f'M{index}', 8, signals, strict=False))
    editor._init_history()
    before = list(database.messages)
    step(editor, lambda: editor.update_signal('M700', 'S3', unit='V'))
    changed = [index for index, message in enumerate(database.messages) if message is not before[index]]
    assert changed == [700]


# LIN editor


def test_lin_undo_redo_and_unsaved_changes(data_dir, tmp_path):
    path = tmp_path / 'example.ldf'
    shutil.copy(data_dir / 'example.ldf', path)
    editor = LdfEditor(path)
    step(editor, lambda: editor.update_frame('SeatStatus', length=editor.frame('SeatStatus').get('length') or 8))
    assert not editor.dirty and not editor.history.can_undo  # Unchanged: no step
    frames = len(editor.frames)
    name = step(editor, lambda: editor.add_frame())
    assert len(editor.frames) == frames + 1 and editor.dirty
    assert editor.changed_message({'frames': []}, editor.state()) == editor.frames[0]['name']
    editor.undo()
    assert len(editor.frames) == frames and not editor.dirty
    editor.redo()
    assert name in [frame['name'] for frame in editor.frames] and editor.dirty
    editor.undo()
    step(editor, lambda: editor.update_frame('SeatStatus', name='SeatState'))
    assert not editor.history.can_redo  # The new step dropped the undone frame
    editor.save()
    assert not editor.dirty
    editor.undo()
    assert editor.dirty and editor.frame('SeatStatus')
