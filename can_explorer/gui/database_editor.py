"""The signal database editor for QML: one database at a time, with a selected message and signal."""

from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.database_editor import (
    BYTE_ORDERS,
    CAN_FD_LENGTHS,
    CAN_LENGTHS,
    DatabaseEditor,
    EditError,
    format_choices,
)
from can_explorer.core.database_io import READ_SUFFIXES, WRITE_SUFFIXES, DatabaseFormatError
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

FORMAT_NAMES = {
    '.dbc': 'Vector DBC',
    '.dbf': 'BUSMASTER DBF',
    '.kcd': 'Kayak KCD',
    '.sym': 'PCAN Symbol',
    '.arxml': 'AUTOSAR ARXML',
}


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


def _number_text(value: Any) -> str:
    return '' if value is None else f'{value:g}'


class DatabaseEditorController(QObject):
    """Every edit returns an error text, or '' when it was applied."""

    changed = pyqtSignal()  # Database content or file
    selection_changed = pyqtSignal()
    saved = pyqtSignal(str)  # Path; connections using the file reload it

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._editor: Optional[DatabaseEditor] = None
        self._message = ''
        self._signal = ''
        self._notes: List[str] = []  # Warnings of the last load or save

    # Files

    @pyqtProperty('QStringList', constant=True)
    def open_filters(self) -> List[str]:
        patterns = ' '.join(f'*{suffix}' for suffix in READ_SUFFIXES)
        return [f'Signal databases ({patterns})', *(f'{FORMAT_NAMES[suffix]} (*{suffix})' for suffix in READ_SUFFIXES)]

    @pyqtProperty('QStringList', constant=True)
    def save_filters(self) -> List[str]:
        return [f'{FORMAT_NAMES[suffix]} (*{suffix})' for suffix in WRITE_SUFFIXES]

    @pyqtProperty(bool, notify=changed)
    def open(self) -> bool:
        return self._editor is not None

    @pyqtProperty(str, notify=changed)
    def file_name(self) -> str:
        if self._editor is None:
            return ''
        return self._editor.path.name if self._editor.path else 'Untitled'

    @pyqtProperty(str, notify=changed)
    def file_path(self) -> str:
        return str(self._editor.path) if self._editor and self._editor.path else ''

    @pyqtProperty(str, notify=changed)
    def format(self) -> str:
        if self._editor is None or self._editor.path is None:
            return ''
        return FORMAT_NAMES.get(self._editor.path.suffix.lower(), self._editor.path.suffix)

    @pyqtProperty(bool, notify=changed)
    def dirty(self) -> bool:
        return bool(self._editor and self._editor.dirty)

    @pyqtProperty(bool, notify=changed)
    def writable(self) -> bool:
        """Whether Save can write back to the file (otherwise Save as is needed)."""
        return bool(self._editor and self._editor.writable)

    @pyqtProperty('QStringList', notify=changed)
    def notes(self) -> List[str]:
        return list(self._notes)

    def _set_editor(self, editor: DatabaseEditor, notes: List[str]) -> None:
        self._editor, self._notes = editor, notes
        messages = editor.messages
        self._message = messages[0].name if messages else ''
        self._signal = messages[0].signals[0].name if messages and messages[0].signals else ''
        self.changed.emit()
        self.selection_changed.emit()

    @pyqtSlot()
    def new_database(self):
        self._set_editor(DatabaseEditor(), [])

    @pyqtSlot(QUrl, result=str)
    def open_file(self, url: QUrl) -> str:
        path = _local_path(url)
        try:
            editor = DatabaseEditor(path)
        except Exception as e:
            return f'Could not open {path.name}: {e}'
        notes = list(editor.warnings)
        if not editor.writable:
            notes.append(f'{path.suffix.upper()[1:]} files cannot be written; use "Save as" to save the changes')
        self._set_editor(editor, notes)
        return ''

    @pyqtSlot(str, result=str)
    def open_path(self, path: str) -> str:
        return self.open_file(QUrl.fromLocalFile(path))

    @pyqtSlot(result=str)
    def revert(self) -> str:
        if self._editor is None or self._editor.path is None:
            return ''
        return self.open_file(QUrl.fromLocalFile(str(self._editor.path)))

    @pyqtSlot(result=str)
    def save(self) -> str:
        if self._editor is None:
            return ''
        if not self._editor.writable:
            return 'Choose a file to save to ("Save as")'
        return self._save(None)

    @pyqtSlot(QUrl, result=str)
    def save_as(self, url: QUrl) -> str:
        return self._save(_local_path(url))

    def _save(self, path: Optional[Path]) -> str:
        try:
            self._notes = self._editor.save(path)
        except (DatabaseFormatError, OSError) as e:
            return str(e)
        except Exception as e:  # The writers reject content they cannot express
            logger.exception('Saving the database failed')
            return f'Could not save: {e}'
        self.changed.emit()
        self.selection_changed.emit()
        self.saved.emit(str(self._editor.path))
        return ''

    # Content

    @pyqtProperty('QVariantList', notify=changed)
    def messages(self) -> List[Dict[str, Any]]:
        if self._editor is None:
            return []
        issues = self._issue_messages()
        return [
            {
                'name': message.name,
                'id': f'{message.frame_id:08X}' if message.is_extended_frame else f'{message.frame_id:03X}',
                'extended': message.is_extended_frame,
                'fd': message.is_fd,
                'length': message.length,
                'signals': len(message.signals),
                'senders': ', '.join(message.senders),
                'issue': message.name in issues,
            }
            for message in self._editor.messages
        ]

    def _issue_messages(self) -> set:
        return {message.name for message in self._editor.messages if self._editor.message_issues(message)}

    @pyqtProperty('QStringList', notify=changed)
    def nodes(self) -> List[str]:
        return self._editor.node_names if self._editor else []

    @pyqtProperty('QStringList', notify=changed)
    def issues(self) -> List[str]:
        return self._editor.issues() if self._editor else []

    @pyqtProperty('QStringList', constant=True)
    def byte_orders(self) -> List[str]:
        return list(BYTE_ORDERS)

    # Selection

    @pyqtProperty(str, notify=selection_changed)
    def current_message(self) -> str:
        return self._message

    @pyqtProperty(str, notify=selection_changed)
    def current_signal(self) -> str:
        return self._signal

    @pyqtSlot(str)
    def select_message(self, name: str):
        if name != self._message:
            self._message = name
            signals = self._current_message_object()
            self._signal = signals.signals[0].name if signals and signals.signals else ''
            self.selection_changed.emit()

    @pyqtSlot(str)
    def select_signal(self, name: str):
        if name != self._signal:
            self._signal = name
            self.selection_changed.emit()

    def _current_message_object(self):
        if self._editor is None or not self._message:
            return None
        try:
            return self._editor.message(self._message)
        except EditError:
            return None

    @pyqtProperty('QVariantMap', notify=selection_changed)
    def message(self) -> Dict[str, Any]:
        """Properties of the selected message."""
        message = self._current_message_object()
        if message is None:
            return {}
        return {
            'name': message.name,
            'frame_id': f'{message.frame_id:X}',
            'extended': message.is_extended_frame,
            'fd': message.is_fd,
            'length': message.length,
            'lengths': [str(length) for length in (CAN_FD_LENGTHS if message.is_fd else CAN_LENGTHS)],
            'cycle_time': '' if message.cycle_time is None else str(message.cycle_time),
            'senders': ', '.join(message.senders),
            'comment': message.comment or '',
            'bus': message.bus_name or '',
            'container': message.is_container,
        }

    @pyqtProperty('QVariantList', notify=selection_changed)
    def signals(self) -> List[Dict[str, Any]]:
        """Signals of the selected message."""
        message = self._current_message_object()
        if message is None:
            return []
        return [
            {
                'name': signal.name,
                'start': signal.start,
                'length': signal.length,
                'byte_order': signal.byte_order,
                'is_signed': signal.is_signed,
                'scale': _number_text(signal.scale),
                'offset': _number_text(signal.offset),
                'minimum': _number_text(signal.minimum),
                'maximum': _number_text(signal.maximum),
                'unit': signal.unit or '',
                'initial': _number_text(signal.raw_initial),
                'receivers': ', '.join(signal.receivers),
                'comment': signal.comment or '',
                'choices': format_choices(signal.choices),
                'multiplex': (
                    'multiplexer'
                    if signal.is_multiplexer
                    else (
                        f'when {signal.multiplexer_signal} = {", ".join(map(str, signal.multiplexer_ids))}'
                        if signal.multiplexer_ids
                        else ''
                    )
                ),
            }
            for signal in message.signals
        ]

    @pyqtProperty('QVariantList', notify=selection_changed)
    def layout(self) -> List[int]:
        """Per bit of the selected message (byte * 8 + bit): the index of its signal, -1 if free, -2 if several."""
        message = self._current_message_object()
        if message is None:
            return []
        return [
            owners[0] if len(owners) == 1 else -1 if not owners else -2
            for owners in self._editor.bit_layout(message.name)
        ]

    # Edits

    def _apply(self, edit) -> str:
        if self._editor is None:
            return 'Open or create a database first'
        try:
            edit()
        except (EditError, ValueError) as e:
            return str(e)
        self.changed.emit()
        self.selection_changed.emit()
        return ''

    @pyqtSlot(result=str)
    def add_message(self) -> str:
        def edit():
            self._message = self._editor.add_message()
            self._signal = ''

        return self._apply(edit)

    @pyqtSlot('QVariantMap', result=str)
    def update_message(self, fields: Dict[str, Any]) -> str:
        def edit():
            self._message = self._editor.update_message(self._message, **fields)

        return self._apply(edit)

    @pyqtSlot(result=str)
    def remove_message(self) -> str:
        def edit():
            names = [message.name for message in self._editor.messages]
            index = names.index(self._message)
            self._editor.remove_message(self._message)
            names.pop(index)
            self._message = names[min(index, len(names) - 1)] if names else ''
            message = self._current_message_object()
            self._signal = message.signals[0].name if message and message.signals else ''

        return self._apply(edit)

    @pyqtSlot(result=str)
    def add_signal(self) -> str:
        def edit():
            self._signal = self._editor.add_signal(self._message)

        return self._apply(edit)

    @pyqtSlot('QVariantMap', result=str)
    def update_signal(self, fields: Dict[str, Any]) -> str:
        def edit():
            self._signal = self._editor.update_signal(self._message, self._signal, **fields)

        return self._apply(edit)

    @pyqtSlot(result=str)
    def remove_signal(self) -> str:
        def edit():
            names = [signal.name for signal in self._editor.message(self._message).signals]
            index = names.index(self._signal)
            self._editor.remove_signal(self._message, self._signal)
            names.pop(index)
            self._signal = names[min(index, len(names) - 1)] if names else ''

        return self._apply(edit)

    @pyqtSlot(str, result=str)
    def add_node(self, name: str) -> str:
        return self._apply(lambda: self._editor.add_node(name))

    @pyqtSlot(str, result=str)
    def remove_node(self, name: str) -> str:
        return self._apply(lambda: self._editor.remove_node(name))
