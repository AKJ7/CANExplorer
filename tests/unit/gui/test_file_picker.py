import os

from PyQt6.QtCore import QMetaObject, QObject, QUrl
from PyQt6.QtQml import QQmlComponent, QQmlEngine
from PyQt6.QtWidgets import QFileDialog

from can_explorer.gui.file_picker import FilePicker
from can_explorer.util.gui import get_res_path


class FakeRun:
    """Stands in for exec(): records the dialog and 'chooses' a file, or cancels when path is None."""

    def __init__(self, path=None, name_filter=None):
        self.path, self.name_filter, self.dialog = path, name_filter, None

    def __call__(self, dialog: QFileDialog) -> bool:
        self.dialog = dialog
        if self.path is None:
            return False
        if self.name_filter is not None:
            dialog.selectNameFilter(self.name_filter)
            dialog.filterSelected.emit(self.name_filter)  # As when the user switches the format
        dialog.selectFile(self.path)
        return True


def test_open_file_returns_the_chosen_file(qapp, tmp_path):
    key = tmp_path / 'key.py'
    key.write_text('')
    run = FakeRun(str(key))
    url = FilePicker(run=run).open_file('Seed/key algorithm', ['Python files (*.py)'])
    assert url == QUrl.fromLocalFile(str(key))
    assert run.dialog.windowTitle() == 'Seed/key algorithm'
    assert run.dialog.nameFilters() == ['Python files (*.py)']
    assert run.dialog.acceptMode() == QFileDialog.AcceptMode.AcceptOpen
    assert run.dialog.fileMode() == QFileDialog.FileMode.ExistingFile


def test_cancelled_dialog_returns_an_empty_url(qapp):
    assert FilePicker(run=FakeRun()).open_file('Open', []).isEmpty()
    assert FilePicker(run=FakeRun()).save_file('Save', ['CSV files (*.csv)'], 'csv').isEmpty()


def test_save_file_appends_the_chosen_filters_suffix(qapp, tmp_path):
    filters = ['ASC files (*.asc)', 'Compressed ASC files (*.asc.gz)']
    run = FakeRun(str(tmp_path / 'trace'), name_filter=filters[1])
    url = FilePicker(run=run).save_file('Export trace', filters, 'blf')
    assert url.toLocalFile() == str(tmp_path / 'trace.asc.gz')
    assert run.dialog.acceptMode() == QFileDialog.AcceptMode.AcceptSave


def test_save_file_falls_back_to_the_default_suffix_and_keeps_a_typed_one(qapp, tmp_path):
    picker = FilePicker(run=FakeRun(str(tmp_path / 'trace')))
    assert picker.save_file('Save', ['All files (*)'], 'csv').toLocalFile() == str(tmp_path / 'trace.csv')
    picker = FilePicker(run=FakeRun(str(tmp_path / 'trace.blf')))
    assert picker.save_file('Save', ['ASC files (*.asc)'], 'asc').toLocalFile() == str(tmp_path / 'trace.blf')


QML = b"""
import QtQuick 2.15
import CanExplorer 1.0
QtObject {
    property string result
    property var chooser: FileChooser {
        title: "Seed/key algorithm"
        fileMode: FileChooser.OpenFile
        nameFilters: ["Python files (*.py)"]
        onAccepted: result = selectedFile.toString()
        onRejected: result = "rejected"
    }
}
"""


def _chooser(run):
    # The Quick Controls style is fixed for the process when first loaded: the application's, as later tests expect
    os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'CanExplorerStyle')
    engine = QQmlEngine()
    engine.addImportPath(str(get_res_path('.')))
    engine.rootContext().setContextProperty('filePicker', FilePicker(parent=engine, run=run))
    component = QQmlComponent(engine)
    component.setData(QML, QUrl())
    root = component.create()
    assert root is not None, component.errorString()
    return engine, root


def test_file_chooser_reports_the_chosen_file(qapp, tmp_path):
    key = tmp_path / 'key.py'
    key.write_text('')
    engine, root = _chooser(FakeRun(str(key)))
    chooser = root.property('chooser')
    assert isinstance(chooser, QObject)
    QMetaObject.invokeMethod(chooser, 'open')
    assert root.property('result') == QUrl.fromLocalFile(str(key)).toString()


def test_file_chooser_reports_a_cancel(qapp):
    engine, root = _chooser(FakeRun())
    chooser = root.property('chooser')
    QMetaObject.invokeMethod(chooser, 'open')
    assert root.property('result') == 'rejected'
