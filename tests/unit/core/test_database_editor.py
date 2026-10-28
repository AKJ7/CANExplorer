import shutil

import pytest

from can_explorer.core.database_editor import (
    DatabaseEditor,
    EditError,
    format_choices,
    parse_choices,
    signal_bits,
)
from can_explorer.core.database_io import DatabaseFormatError


@pytest.fixture
def editor(data_dir, tmp_path):
    path = tmp_path / 'example.dbc'
    shutil.copy(data_dir / 'example.dbc', path)
    return DatabaseEditor(path)


def test_choices_text():
    assert parse_choices('0=Off; 1 = On;0x0F=Error') == {0: 'Off', 1: 'On', 15: 'Error'}
    assert parse_choices('') is None
    assert format_choices({0: 'Off', 1: 'On'}) == '0=Off; 1=On'
    for text in ('Off', 'x=On', '1='):
        with pytest.raises(EditError):
            parse_choices(text)


def test_signal_bits():
    from cantools.database.can import Signal

    assert signal_bits(Signal('a', 4, 6)) == [4, 5, 6, 7, 8, 9]
    # Motorola: start bit 7 is the most significant bit; 12 bits run through byte 0 and the upper half of byte 1
    assert signal_bits(Signal('b', 7, 12, byte_order='big_endian')) == [7, 6, 5, 4, 3, 2, 1, 0, 15, 14, 13, 12]


def test_messages_and_signals(editor, tmp_path):
    name = editor.add_message('Brake', 0x321)
    editor.update_message(name, length=4, cycle_time='20', senders='ABS, ESP', comment='Brake state')
    signal = editor.add_signal(name, 'Pressure')
    editor.update_signal(
        name,
        signal,
        start=0,
        length=12,
        scale='0.1',
        minimum=0,
        maximum=400,
        unit='bar',
        receivers='ECU',
        choices='4095=Invalid',
        is_signed=False,
    )
    assert editor.dirty and editor.issues() == []
    assert {'ABS', 'ESP', 'ECU'} <= set(editor.node_names)  # Nodes referenced are created

    flag = editor.add_signal(name)
    assert editor.signal(name, flag).start == 16  # The first free byte
    editor.update_signal(name, flag, start=8, length=1)
    assert editor.issues() == ['Brake: Signal1 overlaps Pressure']
    assert editor.bit_layout(name)[8] == [0, 1]
    editor.update_signal(name, flag, start=12, name='Valid')

    for suffix in ('.dbc', '.kcd', '.dbf'):
        target = tmp_path / f'saved{suffix}'
        warnings = editor.save(target)
        reloaded = DatabaseEditor(target)
        message = reloaded.message('Brake')
        pressure = reloaded.signal('Brake', 'Pressure')
        senders = ['ABS'] if suffix == '.dbf' else ['ABS', 'ESP']  # DBF stores one sender, and says so
        assert (message.frame_id, message.length, message.senders) == (0x321, 4, senders)
        assert any('first of several senders' in warning for warning in warnings) == (suffix == '.dbf')
        assert (pressure.length, pressure.scale, pressure.maximum, pressure.unit) == (12, 0.1, 400, 'bar')
        assert dict(pressure.choices) == {4095: 'Invalid'} and reloaded.signal('Brake', 'Valid').start == 12
    assert editor.path.name == 'saved.dbf' and not editor.dirty


def test_deferred_validates_only_the_final_layout():
    editor = DatabaseEditor()  # A new database validates strictly, rejecting overlaps as they appear
    name = editor.add_message('Packed', 0x456)
    editor.update_message(name, length=2)
    low0 = editor.add_signal(name, 'Low0')
    editor.update_signal(name, low0, start=0, length=4)  # Low nibble of byte 0
    low1 = editor.add_signal(name, 'Low1')
    editor.update_signal(name, low1, start=8, length=4)  # Low nibble of byte 1: now no whole byte is free

    # add_signal falls back to bit 0 when no byte is free, so a new signal momentarily overlaps Low0 before it is
    # moved to the free high nibble. deferred() holds strict validation back to the end, so only the result is checked.
    with editor.deferred():
        high = editor.add_signal(name, 'High0')
        editor.update_signal(name, high, start=4, length=4)
    assert editor.issues() == [] and editor.dirty

    # A deferred edit whose final layout is still invalid is rejected when the block ends, and can be undone
    from cantools.database.errors import Error as DatabaseError

    with pytest.raises(DatabaseError):
        with editor.deferred():
            clash = editor.add_signal(name, 'Clash')
            editor.update_signal(name, clash, start=0, length=4)
    editor.remove_signal(name, 'Clash')
    assert editor.issues() == []


def test_multiplexing(tmp_path):
    editor = DatabaseEditor()  # A new database validates the multiplexer tree strictly
    name = editor.add_message('Status', 0x200)
    editor.update_message(name, length=8)
    editor.update_signal(name, editor.add_signal(name, 'Mode'), start=0, length=2)
    editor.update_signal(name, editor.add_signal(name, 'VoltA'), start=8, length=8)
    editor.update_signal(name, editor.add_signal(name, 'VoltB'), start=16, length=8)

    # One signal becomes the multiplexer switch; the others become multiplexed and follow it automatically
    editor.update_signal(name, 'Mode', is_multiplexer=True)
    assert editor.signal(name, 'Mode').is_multiplexer
    editor.update_signal(name, 'VoltA', multiplexer_ids='0')
    editor.update_signal(name, 'VoltB', multiplexer_ids='2, 1, 2')  # Parsed, de-duplicated and sorted
    volt_b = editor.signal(name, 'VoltB')
    assert volt_b.multiplexer_ids == [1, 2] and volt_b.multiplexer_signal == 'Mode'
    editor.update_signal(name, 'VoltB', start=8)  # Multiplexed signals of different switch values may share bits
    assert editor.issues() == []

    # Renaming the switch keeps its multiplexed signals pointing at it, and it round-trips through a DBC
    editor.update_signal(name, 'Mode', name='Selector')
    assert editor.signal(name, 'VoltA').multiplexer_signal == 'Selector' and editor.issues() == []
    target = tmp_path / 'mux.dbc'
    editor.save(target)
    reloaded = DatabaseEditor(target)
    signals = {signal.name: signal for signal in reloaded.message('Status').signals}
    assert signals['Selector'].is_multiplexer
    assert signals['VoltB'].multiplexer_ids == [1, 2] and signals['VoltB'].multiplexer_signal == 'Selector'

    # Guards
    with pytest.raises(EditError):  # A signal cannot be both the switch and multiplexed
        editor.update_signal(name, 'Selector', is_multiplexer=True, multiplexer_ids='0')
    with pytest.raises(EditError):  # The switch is still in use by its multiplexed signals
        editor.update_signal(name, 'Selector', is_multiplexer=False)
    lone = editor.add_message('Lone', 0x201)
    editor.update_message(lone, length=8)
    editor.update_signal(lone, editor.add_signal(lone, 'X'), start=0, length=8)
    with pytest.raises(EditError):  # No multiplexer signal to follow
        editor.update_signal(lone, 'X', multiplexer_ids='0')

    # Clearing the switch values makes a signal plain again
    editor.update_signal(name, 'VoltB', start=24, multiplexer_ids='')
    assert editor.signal(name, 'VoltB').multiplexer_ids is None


def test_invalid_edits_leave_the_database_unchanged(editor):
    message = editor.messages[0].name
    signal = editor.messages[0].signals[0].name
    before = editor.database.as_dbc_string()
    for fields in (
        {'name': '1st'},
        {'frame_id': 0x800},
        {'length': 9},
        {'name': editor.messages[1].name},
        {'senders': 'ABS, 2nd'},
    ):
        with pytest.raises(EditError):
            editor.update_message(message, **fields)
    for fields in (
        {'length': 0},
        {'scale': 0},
        {'minimum': 10, 'maximum': 1},
        {'byte_order': 'middle'},
        {'choices': 'nonsense'},
        {'start': 'x'},
    ):
        with pytest.raises(EditError):
            editor.update_signal(message, signal, **fields)
    assert editor.database.as_dbc_string() == before and not editor.dirty
    editor.update_message(message, frame_id=0x1ABCDEF, is_extended_frame=True)  # Extended allows 29 bits
    editor.update_message(message, is_fd=True, length=64)


def test_remove_and_nodes(editor):
    message = editor.messages[0].name
    editor.add_node('Gateway')
    editor.update_message(message, senders='Gateway')
    editor.remove_node('Gateway')
    assert 'Gateway' not in editor.node_names and editor.message(message).senders == []
    editor.remove_signal(message, editor.messages[0].signals[0].name)
    editor.remove_message(message)
    assert message not in [candidate.name for candidate in editor.messages]


def test_new_and_read_only_formats(data_dir, tmp_path):
    editor = DatabaseEditor()
    assert not editor.writable and editor.messages == []
    with pytest.raises(DatabaseFormatError):
        editor.save()
    editor.add_message()
    editor.save(tmp_path / 'new.sym')
    assert editor.writable and DatabaseEditor(tmp_path / 'new.sym').messages[0].name == 'Message1'

    arxml = DatabaseEditor(data_dir / 'example.arxml')
    assert not arxml.writable
    with pytest.raises(DatabaseFormatError, match='Cannot write .arxml'):
        arxml.save()
