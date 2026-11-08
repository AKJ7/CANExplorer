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
