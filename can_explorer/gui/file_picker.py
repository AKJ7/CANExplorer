"""File open/save dialogs for the QML views, shown with QtWidgets' QFileDialog.

Qt Quick's FileDialog falls back to a QML implementation when the platform theme has no native file dialog (e.g. a
bare Wayland compositor or X11 window manager). That implementation imports Qt.labs.folderlistmodel, which the
PyQt6-Qt6 wheel does not ship, and Qt 6.9 then dereferences the dialog it failed to create: opening any file dialog
crashed the application. QFileDialog uses the native dialog where there is one and its own widget dialog otherwise.
"""

from typing import Callable, List

from PyQt6.QtCore import QObject, QUrl, pyqtSlot
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QDialog, QFileDialog

from can_explorer.core.file_dialog_filters import default_suffix, with_default_suffix


def _exec(dialog: QFileDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


class FilePicker(QObject):
    """The `filePicker` context property behind the FileChooser QML type."""

    def __init__(self, parent: QObject = None, run: Callable[[QFileDialog], bool] = _exec):
        super().__init__(parent)
        self._run = run  # Shows the dialog; True when a file was chosen (replaced in tests)

    @pyqtSlot(str, list, result=QUrl)
    def open_file(self, title: str, name_filters: List[str]) -> QUrl:
        """The existing file the user picks, or an empty URL when cancelled."""
        dialog = self._dialog(title, name_filters)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        return self._show(dialog, '')

    @pyqtSlot(str, list, str, result=QUrl)
    def save_file(self, title: str, name_filters: List[str], fallback_suffix: str) -> QUrl:
        """The file to write, with the chosen filter's extension when none is typed; empty when cancelled."""
        dialog = self._dialog(title, name_filters)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setDefaultSuffix(default_suffix(name_filters[0] if name_filters else '', fallback_suffix))
        dialog.filterSelected.connect(
            lambda selected: dialog.setDefaultSuffix(default_suffix(selected, fallback_suffix))
        )
        return self._show(dialog, fallback_suffix)

    @staticmethod
    def _dialog(title: str, name_filters: List[str]) -> QFileDialog:
        dialog = QFileDialog(None, title)
        if name_filters:
            dialog.setNameFilters(name_filters)
        # Over the window it was opened from, modal to it
        if (window := QGuiApplication.focusWindow()) is not None:
            dialog.winId()  # Creates the window handle
            if (handle := dialog.windowHandle()) is not None:
                handle.setTransientParent(window)
        return dialog

    def _show(self, dialog: QFileDialog, fallback_suffix: str) -> QUrl:
        try:
            if not self._run(dialog) or not dialog.selectedFiles():
                return QUrl()
            path = dialog.selectedFiles()[0]
            if dialog.acceptMode() == QFileDialog.AcceptMode.AcceptSave:
                path = with_default_suffix(path, default_suffix(dialog.selectedNameFilter(), fallback_suffix))
            return QUrl.fromLocalFile(path)
        finally:
            dialog.deleteLater()
