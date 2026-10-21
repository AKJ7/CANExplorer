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
from can_explorer.core.ldf_editor import LIN_SPEEDS, LdfEditor
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

FORMAT_NAMES = {
    '.dbc': 'Vector DBC',
    '.dbf': 'BUSMASTER DBF',
    '.kcd': 'Kayak KCD',
    '.sym': 'PCAN Symbol',
    '.arxml': 'AUTOSAR ARXML',
    '.ldf': 'LIN description file',
}
EDIT_SUFFIXES = READ_SUFFIXES + ('.ldf',)
SAVE_SUFFIXES = WRITE_SUFFIXES + ('.ldf',)


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
        self._editor: Optional[DatabaseEditor | LdfEditor] = None
        self._message = ''
        self._signal = ''
        self._notes: List[str] = []  # Warnings of the last load or save

    # Files

    @pyqtProperty('QStringList', constant=True)
    def open_filters(self) -> List[str]:
        patterns = ' '.join(f'*{suffix}' for suffix in EDIT_SUFFIXES)
        return [f'Signal databases ({patterns})', *(f'{FORMAT_NAMES[suffix]} (*{suffix})' for suffix in EDIT_SUFFIXES)]

    @pyqtProperty('QStringList', notify=changed)
    def save_filters(self) -> List[str]:
        """LIN databases are saved as LDF, CAN databases in the CAN formats."""
        suffixes = ('.ldf',) if self.lin else WRITE_SUFFIXES
        return [f'{FORMAT_NAMES[suffix]} (*{suffix})' for suffix in suffixes]

    @property
    def lin(self) -> bool:
        return isinstance(self._editor, LdfEditor)

    @pyqtProperty(str, notify=changed)
    def kind(self) -> str:
        """'can' or 'lin' (or '' without a database)."""
        return '' if self._editor is None else 'lin' if self.lin else 'can'

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

    def _set_editor(self, editor: DatabaseEditor | LdfEditor, notes: List[str]) -> None:
        self._editor, self._notes = editor, notes
        names = self._message_names()
        self._message = names[0] if names else ''
        signals = self._signal_names(self._message)
        self._signal = signals[0] if signals else ''
        self.changed.emit()
        self.selection_changed.emit()

    def _message_names(self) -> List[str]:
        if self.lin:
            return [frame['name'] for frame in self._editor.frames]
        return [message.name for message in self._editor.messages] if self._editor else []

    def _signal_names(self, message: str) -> List[str]:
        if not message or self._editor is None:
            return []
        try:
            if self.lin:
                return [signal['name'] for signal, _ in self._editor.frame_signals(message)]
            return [signal.name for signal in self._editor.message(message).signals]
        except EditError:
            return []

    @pyqtSlot()
    def new_database(self):
        self._set_editor(DatabaseEditor(), [])

    @pyqtSlot()
    def new_lin_database(self):
        self._set_editor(LdfEditor(), [])

    @pyqtSlot(QUrl, result=str)
    def open_file(self, url: QUrl) -> str:
        path = _local_path(url)
        try:
            editor = LdfEditor(path) if path.suffix.lower() == '.ldf' else DatabaseEditor(path)
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
        except (DatabaseFormatError, EditError, OSError) as e:
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
        if self.lin:
            return [
                {
                    'name': frame['name'],
                    'id': f'{frame["frame_id"]:02X}',
                    'extended': False,
                    'fd': False,
                    'length': frame.get('length') or 8,
                    'signals': len(frame['signals']),
                    'senders': frame['publisher'],
                    'issue': bool(self._editor.frame_issues(frame)),
                }
                for frame in self._editor.frames
            ]
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

    @pyqtProperty('QVariantList', constant=True)
    def lin_speeds(self) -> List[int]:
        return list(LIN_SPEEDS)

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
            signals = self._signal_names(name)
            self._signal = signals[0] if signals else ''
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
            return self._editor.frame(self._message) if self.lin else self._editor.message(self._message)
        except EditError:
            return None

    @pyqtProperty('QVariantMap', notify=selection_changed)
    def message(self) -> Dict[str, Any]:
        """Properties of the selected message (a frame in LIN databases)."""
        message = self._current_message_object()
        if message is None:
            return {}
        if self.lin:
            return {
                'name': message['name'],
                'frame_id': f'{message["frame_id"]:02X}',
                'length': message.get('length') or 8,
                'lengths': [str(length) for length in range(1, 9)],
                'publisher': message['publisher'],
            }
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
        if self.lin:
            rows = []
            for signal, offset in self._editor.frame_signals(message['name']):
                initial = signal['init_value']
                rows.append(
                    {
                        'name': signal['name'],
                        'start': offset,
                        'length': signal['width'],
                        'byte_order': 'little_endian',  # LIN sends the least significant bit first
                        'initial': ', '.join(map(str, initial)) if isinstance(initial, list) else str(initial),
                        'receivers': ', '.join(signal.get('subscribers', [])),
                        'publisher': signal['publisher'],
                        'encoding': self._editor.signal_encoding(signal['name']),
                    }
                )
            return rows
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
            for owners in self._editor.bit_layout(self._message)
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
            self._message = self._editor.add_frame() if self.lin else self._editor.add_message()
            self._signal = ''

        return self._apply(edit)

    @pyqtSlot('QVariantMap', result=str)
    def update_message(self, fields: Dict[str, Any]) -> str:
        def edit():
            update = self._editor.update_frame if self.lin else self._editor.update_message
            self._message = update(self._message, **fields)

        return self._apply(edit)

    @pyqtSlot(result=str)
    def remove_message(self) -> str:
        def edit():
            names = self._message_names()
            index = names.index(self._message)
            (self._editor.remove_frame if self.lin else self._editor.remove_message)(self._message)
            names.pop(index)
            self._message = names[min(index, len(names) - 1)] if names else ''
            signals = self._signal_names(self._message)
            self._signal = signals[0] if signals else ''

        return self._apply(edit)

    @pyqtSlot(result=str)
    def add_signal(self) -> str:
        def edit():
            self._signal = self._editor.add_signal(self._message)

        return self._apply(edit)

    @pyqtSlot('QVariantMap', result=str)
    def update_signal(self, fields: Dict[str, Any]) -> str:
        if self.lin and 'receivers' in fields:  # The form's field; LIN calls them subscribers
            fields = {**fields, 'subscribers': fields['receivers']}
            del fields['receivers']

        def edit():
            self._signal = self._editor.update_signal(self._message, self._signal, **fields)

        return self._apply(edit)

    @pyqtSlot('QVariantMap', str, result=str)
    def add_found_signal(self, definition: Dict[str, Any], database_file: str = '') -> str:
        """Add a signal found with the reverse engineering tools (ReverseController.signal_definition), and its
        message when the database has none with that identifier, and select it.

        Without an open database, the connection's `database_file` is opened (or a new database started)."""
        if self._editor is None:
            suffix = Path(database_file).suffix.lower() if database_file else ''
            if suffix in READ_SUFFIXES:
                error = self.open_path(database_file)
                if error:
                    return error
            else:
                self.new_database()
        if self.lin:
            return 'The editor has a LIN database open; close it or open a CAN database first'
        editor = self._editor
        try:
            frame_id, extended = int(definition['frame_id']), bool(definition.get('extended', False))
            needed = int(definition.get('frame_length', 8))
        except (KeyError, TypeError, ValueError) as e:
            return f'Invalid signal: {e}'

        def edit():
            message = next(
                (
                    message
                    for message in editor.messages
                    if message.frame_id == frame_id and message.is_extended_frame == extended
                ),
                None,
            )
            if message is None:
                id_text = f'{frame_id:08X}' if extended else f'{frame_id:03X}'
                names = {message.name for message in editor.messages}
                name, index = f'Message_{id_text}', 2
                while name in names:
                    name, index = f'Message_{id_text}_{index}', index + 1
                name = editor.add_message(name, frame_id, extended)
                current_length = 0
            else:
                name, current_length = message.name, message.length
            if needed > current_length:
                fd = needed > 8 or (message is not None and message.is_fd)
                length = min(length for length in (CAN_FD_LENGTHS if fd else CAN_LENGTHS) if length >= min(needed, 64))
                name = editor.update_message(name, length=length, is_fd=fd)
            signal = editor.add_signal(name, definition.get('name') or None)
            fields = {
                key: definition[key]
                for key in ('start', 'length', 'byte_order', 'is_signed', 'scale', 'offset', 'unit', 'comment')
                if key in definition
            }
            try:
                signal = editor.update_signal(name, signal, **fields)
            except (EditError, ValueError):
                editor.remove_signal(name, signal)
                raise
            self._message, self._signal = name, signal

        return self._apply(edit)

    @pyqtSlot(result=str)
    def remove_signal(self) -> str:
        def edit():
            names = self._signal_names(self._message)
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

    # LIN databases: header, nodes, encodings and schedule tables

    @pyqtProperty('QVariantMap', notify=changed)
    def lin_header(self) -> Dict[str, Any]:
        if not self.lin:
            return {}
        data, master = self._editor.data, self._editor.master
        return {
            'speed': data.get('speed', 19200),
            'protocol_version': data.get('protocol_version', ''),
            'language_version': data.get('language_version', ''),
            'channel_name': data.get('channel_name') or '',
            'master': master.get('name', ''),
            'timebase': f'{master.get("timebase", 0) * 1000:g}',
            'jitter': f'{master.get("jitter", 0) * 1000:g}',
        }

    @pyqtSlot('QVariantMap', result=str)
    def set_lin_header(self, fields: Dict[str, Any]) -> str:
        """speed, protocol_version, language_version, channel_name, master, timebase (ms), jitter (ms)."""

        def edit():
            header = {
                key: fields[key]
                for key in ('speed', 'protocol_version', 'language_version', 'channel_name')
                if key in fields
            }
            if header:
                self._editor.set_header(**header)
            if any(key in fields for key in ('master', 'timebase', 'jitter')):
                self._editor.set_master(fields.get('master'), fields.get('timebase'), fields.get('jitter'))

        return self._apply(edit)

    @pyqtProperty('QVariantList', notify=changed)
    def slave_nodes(self) -> List[Dict[str, Any]]:
        if not self.lin:
            return []
        rows = []
        for name in self._editor.slaves:
            attributes = self._editor.node_attributes(name)
            product = attributes.get('product_id') or {}
            rows.append(
                {
                    'name': name,
                    'configured_nad': f'{attributes.get("configured_nad", 0):02X}',
                    'initial_nad': f'{attributes.get("initial_nad", 0):02X}',
                    'supplier_id': f'{product.get("supplier_id", 0):04X}',
                    'function_id': f'{product.get("function_id", 0):04X}',
                    'variant': str(product.get('variant', 0)),
                    'lin_protocol': attributes.get('lin_protocol', ''),
                }
            )
        return rows

    @pyqtSlot(str, str, result=str)
    def rename_node(self, old: str, new: str) -> str:
        return self._apply(lambda: self._editor.rename_node(old, new))

    @pyqtSlot(str, 'QVariantMap', result=str)
    def update_node(self, name: str, fields: Dict[str, Any]) -> str:
        """Node attributes: configured_nad, initial_nad, supplier_id, function_id (hexadecimal), variant, lin_protocol."""
        hexadecimal = ('configured_nad', 'initial_nad', 'supplier_id', 'function_id')
        values = {
            key: f'0x{value}' if key in hexadecimal and not str(value).lower().startswith('0x') else value
            for key, value in fields.items()
        }
        return self._apply(lambda: self._editor.update_node_attributes(name, **values))

    @pyqtProperty('QVariantList', notify=changed)
    def encodings(self) -> List[Dict[str, Any]]:
        if not self.lin:
            return []
        return [
            {
                'name': entry['name'],
                'text': self._editor.encoding_text(entry['name']),
                'signals': ', '.join(self._editor.encoding_signals(entry['name'])),
            }
            for entry in self._editor.encodings
        ]

    @pyqtProperty('QStringList', notify=changed)
    def encoding_names(self) -> List[str]:
        return [entry['name'] for entry in self._editor.encodings] if self.lin else []

    @pyqtSlot(str, result=str)
    def add_encoding(self, name: str) -> str:
        return self._apply(lambda: self._editor.add_encoding(name))

    @pyqtSlot(str, str, result=str)
    def rename_encoding(self, old: str, new: str) -> str:
        return self._apply(lambda: self._editor.rename_encoding(old, new))

    @pyqtSlot(str, result=str)
    def remove_encoding(self, name: str) -> str:
        return self._apply(lambda: self._editor.remove_encoding(name))

    @pyqtSlot(str, str, result=str)
    def set_encoding_text(self, name: str, text: str) -> str:
        return self._apply(lambda: self._editor.set_encoding_text(name, text))

    @pyqtProperty('QVariantList', notify=changed)
    def schedules(self) -> List[Dict[str, Any]]:
        if not self.lin:
            return []
        return [
            {'name': table['name'], 'text': self._editor.schedule_text(table['name'])}
            for table in self._editor.schedules
        ]

    @pyqtSlot(str, result=str)
    def add_schedule(self, name: str) -> str:
        return self._apply(lambda: self._editor.add_schedule(name))

    @pyqtSlot(str, str, result=str)
    def rename_schedule(self, old: str, new: str) -> str:
        return self._apply(lambda: self._editor.rename_schedule(old, new))

    @pyqtSlot(str, result=str)
    def remove_schedule(self, name: str) -> str:
        return self._apply(lambda: self._editor.remove_schedule(name))

    @pyqtSlot(str, str, result=str)
    def set_schedule_text(self, name: str, text: str) -> str:
        return self._apply(lambda: self._editor.set_schedule_text(name, text))
