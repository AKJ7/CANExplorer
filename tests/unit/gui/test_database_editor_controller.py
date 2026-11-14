"""The database editor controller: a read-only format (ARXML) must still have a working output path."""

from pathlib import Path

from PyQt6.QtCore import QUrl

from can_explorer.gui.database_editor import DatabaseEditorController


def test_read_only_database_can_be_saved_as_a_writable_format(qapp, data_dir, tmp_path):
    controller = DatabaseEditorController()
    assert controller.open_file(QUrl.fromLocalFile(str(data_dir / 'example.arxml'))) == ''
    assert not controller.writable  # ARXML cannot be written back

    # The user is told the file is read-only and pointed at Save as.
    assert any('Save as' in note for note in controller.notes)

    # The Save-as dialog is prefilled with a writable name (ARXML -> DBC), next to the source, so it is not a dead end.
    suggested = Path(controller.suggested_save_file.toLocalFile())
    assert suggested.suffix == '.dbc' and suggested.stem == 'example'

    # Saving as a writable format works and notifies connections to reload.
    saved: list = []
    controller.saved.connect(saved.append)
    target = tmp_path / 'from_arxml.kcd'
    assert controller.save_as(QUrl.fromLocalFile(str(target))) == ''
    assert target.is_file() and saved == [str(target)] and controller.writable


def test_new_database_suggests_a_writable_name(qapp, tmp_path):
    controller = DatabaseEditorController()
    controller.new_database()
    assert Path(controller.suggested_save_file.toLocalFile()).suffix == '.dbc'
    controller.new_lin_database()
    assert Path(controller.suggested_save_file.toLocalFile()).suffix == '.ldf'


def _open(data_dir, tmp_path, name='complex.dbc'):
    import shutil

    path = tmp_path / name
    shutil.copy(data_dir / name, path)
    controller = DatabaseEditorController()
    assert controller.open_file(QUrl.fromLocalFile(str(path))) == ''
    return controller


def test_undo_and_redo_select_what_changed(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    assert not controller.can_undo and not controller.can_redo
    controller.select('VehicleSpeed', 'Direction')
    assert controller.update_signal({'unit': 'deg'}) == ''
    controller.select('FloatData', 'Ratio')
    assert controller.can_undo and controller.dirty
    assert controller.undo() == ''
    # The undone change is shown: its message is selected and the selected signal is kept where it exists
    assert controller.current_message == 'VehicleSpeed' and controller.current_signal == 'Direction'
    assert next(row for row in controller.signals if row['name'] == 'Direction')['unit'] == ''
    assert not controller.dirty and controller.can_redo
    assert controller.redo() == '' and controller.current_message == 'VehicleSpeed'
    assert controller.undo() == '' and controller.undo() == 'Nothing to undo'


def test_failed_edits_leave_no_undo_step(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    controller.select('EngineData', 'Torque')
    assert controller.update_signal({'length': '99'}) != ''
    assert not controller.can_undo and not controller.dirty


def test_undo_a_removed_message(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    controller.select('FloatData', '')
    assert controller.remove_message() == ''
    assert 'FloatData' not in [row['name'] for row in controller.messages]
    assert controller.undo() == ''
    assert controller.current_message == 'FloatData'
    assert 'FloatData' in [row['name'] for row in controller.messages]


def test_search_signals(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    assert controller.search_signals('value') == {'Multiplexed': ['ValueA', 'ValueB']}
    assert controller.search_signals('  SPEED ') == {'EngineData': ['EngineSpeed'], 'VehicleSpeed': ['WheelSpeed']}
    assert controller.search_signals('') == {} and controller.search_signals('nothing') == {}
    controller.select('Multiplexed', 'ValueB')
    assert controller.update_signal({'name': 'Velocity'}) == ''
    assert controller.search_signals('value') == {'Multiplexed': ['ValueA']}  # Follows edits
    assert controller.search_signals('veloc') == {'Multiplexed': ['Velocity']}
    controller.select('Multiplexed', 'Unknown')  # Falls back to the first signal
    assert controller.current_signal == 'Mux'
    lin = _open(data_dir, tmp_path, 'example.ldf')
    assert lin.search_signals('seat') == {
        'SeatStatus': ['SeatPosition', 'SeatTemperature'],
        'SeatControl': ['SeatHeating'],
    }


def test_sender_tree(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    model = controller.sender_model
    roles = {name.decode(): role for role, name in model.roleNames().items()}

    def rows():
        def row(index):
            return {name: model.data(model.index(index), roles[name]) for name in roles}

        return [
            (
                r['kind'],
                r['depth'],
                r['node'] if r['kind'] == 'node' else r['message'] if r['kind'] == 'message' else r['signalName'],
                r['expanded'],
                r['count'],
            )
            for r in map(row, range(model.rowCount()))
        ]

    def find(kind, name):
        return next(i for i, r in enumerate(rows()) if r[0] == kind and r[2] == name)

    assert model.rowCount() == 0  # Built only while shown
    model.set_active(True)
    # Nodes start unfolded and messages folded
    assert rows() == [
        ('node', 0, 'Engine', True, 2),
        ('message', 1, 'EngineData', False, 5),
        ('message', 1, 'FloatData', False, 2),
        ('node', 0, 'Gateway', True, 3),
        ('message', 1, 'FdFrame', False, 2),
        ('message', 1, 'Multiplexed', False, 3),
        ('message', 1, 'VehicleSpeed', False, 2),
    ]
    assert model.signal_count == 14
    model.toggle(find('message', 'FloatData'))  # Unfold a message: its signals
    assert rows()[2:5] == [
        ('message', 1, 'FloatData', True, 2),
        ('signal', 2, 'Pressure', False, 0),
        ('signal', 2, 'Ratio', False, 0),
    ]
    model.toggle(find('node', 'Engine'))  # Fold a node: its messages go
    assert rows()[:2] == [('node', 0, 'Engine', False, 2), ('node', 0, 'Gateway', True, 3)]
    model.toggle(find('node', 'Engine'))
    assert rows()[2][3] is True  # FloatData stayed unfolded

    # Searching unfolds the matches; folding there lasts until the search changes
    model.set_query('wheel')
    assert rows() == [
        ('node', 0, 'Gateway', True, 1),
        ('message', 1, 'VehicleSpeed', True, 1),
        ('signal', 2, 'WheelSpeed', False, 0),
    ]
    model.toggle(1)
    assert rows()[1][3] is False and model.rowCount() == 2
    model.set_query('engine')  # A node name matches all its messages, a message name all its signals
    assert model.signal_count == 7 and all(r[3] for r in rows() if r[0] != 'signal')
    model.set_query('')
    assert rows()[2][3] is True  # Back to the folding of before the search

    # Fold and unfold everything
    model.set_all(True)
    assert model.rowCount() == 2 + 5 + 14
    model.set_all(False)
    assert rows() == [('node', 0, 'Engine', False, 2), ('node', 0, 'Gateway', False, 3)]
    model.set_all(True)

    # Follows edits, keeping the folding
    controller.select('FloatData', 'Ratio')
    assert controller.duplicate_signal() == ''
    assert model.signal_count == 15 and rows()[find('message', 'FloatData')][4] == 3
    assert controller.undo() == '' and model.signal_count == 14
    model.set_active(False)
    assert model.rowCount() == 0


def test_messages_without_signals_are_in_the_tree(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    assert controller.add_message() == ''
    controller.update_message({'senders': 'Engine'})
    group = next(group for group in controller.signals_by_sender if group['node'] == 'Engine')
    assert {'message': controller.current_message, 'id': '101', 'signals': []} in group['messages']
    controller.sender_model.set_active(True)
    assert controller.sender_model.rowCount() == 8  # A new message row, with no signals


def test_signals_grouped_by_sender(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    groups = {group['node']: group['signals'] for group in controller.signals_by_sender}
    assert list(groups) == ['Engine', 'Gateway']
    assert [(row['message'], row['signal']) for row in groups['Engine']][:2] == [
        ('EngineData', 'EngineSpeed'),
        ('EngineData', 'Flags'),
    ]
    assert {row['message'] for row in groups['Gateway']} == {'FdFrame', 'Multiplexed', 'VehicleSpeed'}
    assert next(row for row in groups['Gateway'] if row['signal'] == 'WheelSpeed')['id'] == '18FEF1FE'
    # A message with several senders is listed under each; one without any under "no sender" ('') last
    controller.select('FloatData', '')
    assert controller.update_message({'senders': 'Engine, Dashboard'}) == ''
    controller.select('VehicleSpeed', '')
    assert controller.update_message({'senders': ''}) == ''
    groups = {group['node']: group['signals'] for group in controller.signals_by_sender}
    assert list(groups) == ['Dashboard', 'Engine', 'Gateway', '']
    assert {row['message'] for row in groups['Dashboard']} == {'FloatData'}
    assert {row['message'] for row in groups['']} == {'VehicleSpeed'}


def test_lin_signals_grouped_by_publisher(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path, 'example.ldf')
    groups = {group['node']: [row['signal'] for row in group['signals']] for group in controller.signals_by_sender}
    assert groups == {
        'BCM': ['LightOn', 'SeatHeating'],
        'LightECU': ['LightLevel'],
        'SeatECU': ['SeatPosition', 'SeatTemperature'],
    }
    assert controller.duplicate_message() == 'Frames of LIN databases cannot be duplicated'
    assert controller.copy_signal() == 'Signals of LIN databases cannot be copied'


def test_duplicate_copy_and_paste(qapp, data_dir, tmp_path):
    controller = _open(data_dir, tmp_path)
    controller.select('EngineData', 'Torque')
    assert controller.duplicate_message() == ''
    assert (controller.current_message, controller.current_signal) == ('EngineData_copy', 'Torque')
    assert controller.duplicate_signal() == '' and controller.current_signal == 'Torque_copy'
    assert controller.paste_signal() == 'Copy a signal first'
    assert controller.copy_signal() == '' and controller.copied_signal == 'Torque_copy'
    controller.select('FloatData', 'Ratio')
    assert controller.paste_signal() == ''
    assert (controller.current_message, controller.current_signal) == ('FloatData', 'Torque_copy')
    assert controller.undo() == '' and controller.current_message == 'FloatData'
    assert 'Torque_copy' not in [row['name'] for row in controller.signals]
