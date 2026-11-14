"""The signal database editor for QML: one database at a time, with a selected message and signal."""

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.database_editor import (
    BYTE_ORDERS,
    CAN_FD_LENGTHS,
    CAN_LENGTHS,
    DatabaseEditor,
    EditError,
    format_choices,
)
from can_explorer.core.database_io import (
    DEFAULT_WRITE_SUFFIX,
    READ_SUFFIXES,
    WRITE_SUFFIXES,
    DatabaseFormatError,
    output_suffix,
)
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


NODE_ROW, MESSAGE_ROW, SIGNAL_ROW = 'node', 'message', 'signal'


class SenderRowsModel(QAbstractListModel):
    """The "By sender" tree: each sending node, its messages under it and their signals under them, as the rows of a
    list (`depth` 0, 1 and 2), filtered by the search. Nodes and messages fold and unfold (`toggle`): nodes start
    unfolded and messages folded, so a large database opens as a short list. While searching, the matching nodes and
    messages are unfolded; folding one then lasts until the search changes.

    A list model rather than a QML array: a database of 1500 messages has some 24000 signals, and converting them to
    QML objects on every change took a quarter of a second; the view only reads the rows it shows. It is built only
    while the list is shown (`active`)."""

    FIELDS = ('kind', 'depth', 'node', 'message', 'identifier', 'signalName', 'expanded', 'count')
    ROLES = {Qt.ItemDataRole.UserRole + 1 + index: name.encode() for index, name in enumerate(FIELDS)}
    signal_count_changed = pyqtSignal()

    def __init__(self, groups, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._groups = groups  # Returns the groups of DatabaseEditorController.signals_by_sender
        self._rows: List[tuple] = []
        self._query = ''
        self._active = False
        self._signal_count = 0
        self._folded_nodes: set = set()  # Nodes folded (they start unfolded)
        self._unfolded_messages: set = set()  # (node, message) unfolded (they start folded)
        self._search_folded: set = set()  # While searching, what was folded: node or (node, message)

    def roleNames(self) -> Dict[int, bytes]:
        return self.ROLES

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows) or role not in self.ROLES:
            return None
        return self._rows[index.row()][role - Qt.ItemDataRole.UserRole - 1]

    @pyqtProperty(int, notify=signal_count_changed)
    def signal_count(self) -> int:
        """The signals matching the search, folded or not."""
        return self._signal_count

    @pyqtSlot(bool)
    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.refresh()

    @pyqtSlot(str)
    def set_query(self, query: str) -> None:
        query = query.strip().lower()
        if query != self._query:
            self._query = query
            self._search_folded.clear()
            self.refresh()

    # Folding

    def _node_open(self, node: str) -> bool:
        return node not in (self._search_folded if self._query else self._folded_nodes)

    def _message_open(self, node: str, message: str) -> bool:
        if self._query:
            return (node, message) not in self._search_folded
        return (node, message) in self._unfolded_messages

    @pyqtSlot(int)
    def toggle(self, row: int) -> None:
        """Fold or unfold the node or message of a row."""
        if not 0 <= row < len(self._rows):
            return
        kind, _, node, message = self._rows[row][:4]
        if kind == NODE_ROW:
            key, opened, unfolded = node, self._node_open(node), None
        elif kind == MESSAGE_ROW:
            key, opened, unfolded = (node, message), self._message_open(node, message), self._unfolded_messages
        else:
            return
        if self._query:
            (self._search_folded.add if opened else self._search_folded.discard)(key)
        elif unfolded is None:
            (self._folded_nodes.add if opened else self._folded_nodes.discard)(key)
        else:
            (unfolded.discard if opened else unfolded.add)(key)
        self.refresh()

    @pyqtSlot(bool)
    def set_all(self, unfold: bool) -> None:
        """Unfold every node and message, or fold them all."""
        groups = self._groups() if self._active else []
        nodes = {group['node'] for group in groups}
        messages = {(group['node'], entry['message']) for group in groups for entry in group['messages']}
        if self._query:
            self._search_folded = set() if unfold else nodes | messages
        else:
            self._folded_nodes = set() if unfold else nodes
            self._unfolded_messages = messages if unfold else set()
        self.refresh()

    def refresh(self) -> None:
        rows: List[tuple] = []
        signal_count = 0
        if self._active:
            query = self._query
            for group in self._groups():
                node = group['node']
                if not query or query in node.lower():
                    messages = [(entry, entry['signals']) for entry in group['messages']]
                else:
                    messages = []
                    for entry in group['messages']:
                        if query in entry['message'].lower() or query in entry['id'].lower():
                            messages.append((entry, entry['signals']))  # The message matches: all its signals
                        else:
                            found = [signal for signal in entry['signals'] if query in signal.lower()]
                            if found:
                                messages.append((entry, found))
                if not messages:
                    continue
                signal_count += sum(len(signals) for _, signals in messages)
                node_open = self._node_open(node)
                rows.append((NODE_ROW, 0, node, '', '', '', node_open, len(messages)))
                if not node_open:
                    continue
                for entry, signals in messages:
                    message_open = self._message_open(node, entry['message'])
                    rows.append((MESSAGE_ROW, 1, node, entry['message'], entry['id'], '', message_open, len(signals)))
                    if message_open:
                        rows.extend(
                            (SIGNAL_ROW, 2, node, entry['message'], entry['id'], signal, False, 0) for signal in signals
                        )
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()
        if signal_count != self._signal_count:
            self._signal_count = signal_count
            self.signal_count_changed.emit()


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
        # The message-list and issue-list rows are expensive to build for a large database and are read by several
        # QML bindings on every change (and on every search keystroke); cache them and rebuild only when an edit
        # actually changes the content, not on selection changes or searches.
        self._messages_cache: Optional[List[Dict[str, Any]]] = None
        self._issues_cache: Optional[List[str]] = None
        self._senders_cache: Optional[List[Dict[str, Any]]] = None
        self._search_cache: Optional[List[tuple]] = None  # (message, [(lowercase signal name, signal name)])
        self._copied_signal = None  # A copy of the signal of "Copy signal", pasted with "Paste signal"
        self._sender_model = SenderRowsModel(lambda: self.signals_by_sender, self)
        self.changed.connect(self._sender_model.refresh)  # After every change of the content (when shown)

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

    @pyqtProperty(QUrl, notify=changed)
    def suggested_save_file(self) -> QUrl:
        """The file to prefill the Save-as dialog with, so a read-only format is never a dead end: the opened file
        with a writable extension (ARXML → DBC), next to the source; 'Untitled' in the home folder for a new one."""
        if self._editor is None:
            return QUrl()
        path = self._editor.path
        folder = path.parent if path else Path.home()
        stem = path.stem if path else 'Untitled'
        suffix = '.ldf' if self.lin else output_suffix(path.suffix if path else '')
        return QUrl.fromLocalFile(str(folder / f'{stem}{suffix}'))

    def _invalidate(self) -> None:
        """Drop the cached message and issue rows; the next read rebuilds them."""
        self._messages_cache = None
        self._issues_cache = None
        self._senders_cache = None
        self._search_cache = None

    def _set_editor(self, editor: DatabaseEditor | LdfEditor, notes: List[str]) -> None:
        self._invalidate()
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
            formats = ', '.join(suffix.upper()[1:] for suffix in WRITE_SUFFIXES)
            notes.append(
                f'{path.suffix.upper()[1:]} files cannot be written; use "Save as" to save the changes as '
                f'{formats} (a {DEFAULT_WRITE_SUFFIX.upper()[1:]} name is suggested)'
            )
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
        if self._messages_cache is None:
            self._messages_cache = self._build_messages()
        return self._messages_cache

    def _build_messages(self) -> List[Dict[str, Any]]:
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
        if self._editor is None:
            return []
        if self._issues_cache is None:
            self._issues_cache = self._editor.issues()
        return self._issues_cache

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
                'is_multiplexer': signal.is_multiplexer,
                'multiplexer_ids': ', '.join(map(str, signal.multiplexer_ids)) if signal.multiplexer_ids else '',
                'multiplexer_signal': signal.multiplexer_signal or '',
            }
            for signal in message.signals
        ]

    @pyqtProperty('QStringList', notify=selection_changed)
    def multiplexers(self) -> List[str]:
        """Names of the multiplexer (switch) signals of the selected message, to say which switch a signal follows."""
        if self.lin:
            return []
        message = self._current_message_object()
        return [signal.name for signal in message.signals if signal.is_multiplexer] if message else []

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

    @pyqtProperty('QVariantList', notify=changed)
    def signals_by_sender(self) -> List[Dict[str, Any]]:
        """Every message and signal, grouped by the node sending it: [{node, messages: [{message, id, signals: [name]}],
        signals: [{message, id, signal}]}], nodes sorted by name and messages by name, with the messages without a
        sender last (node ''). A message with several senders is listed under each of them. LIN frames and signals
        are grouped by the publisher of the signal (of the frame for a frame without signals)."""
        if self._editor is None:
            return []
        if self._senders_cache is None:
            self._senders_cache = self._build_senders()
        return self._senders_cache

    def _build_senders(self) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Dict[str, Any]]] = {}  # node -> message -> {message, id, signals}

        def add(node: str, message: str, frame_id: str, signal: Optional[str]) -> None:
            entry = groups.setdefault(node, {}).setdefault(message, {'message': message, 'id': frame_id, 'signals': []})
            if signal is not None:
                entry['signals'].append(signal)

        if self.lin:
            for frame in self._editor.frames:
                frame_id = f'{frame["frame_id"]:02X}'
                signals = self._editor.frame_signals(frame['name'])
                if not signals:
                    add(frame['publisher'] or '', frame['name'], frame_id, None)
                for signal, _ in signals:
                    add(signal.get('publisher') or frame['publisher'] or '', frame['name'], frame_id, signal['name'])
        else:
            for message in self._editor.messages:
                frame_id = f'{message.frame_id:08X}' if message.is_extended_frame else f'{message.frame_id:03X}'
                for sender in [name for name in message.senders if name != 'Vector__XXX'] or ['']:
                    add(sender, message.name, frame_id, None)
                    for signal in message.signals:
                        add(sender, message.name, frame_id, signal.name)
        order = sorted((name for name in groups if name), key=str.lower) + ([''] if '' in groups else [])
        result = []
        for name in order:
            messages = sorted(groups[name].values(), key=lambda entry: entry['message'].lower())
            for entry in messages:
                entry['signals'].sort()
            result.append(
                {
                    'node': name,
                    'messages': messages,
                    'signals': [
                        {'message': entry['message'], 'id': entry['id'], 'signal': signal}
                        for entry in messages
                        for signal in entry['signals']
                    ],
                }
            )
        return result

    @pyqtProperty(QObject, constant=True)
    def sender_model(self) -> SenderRowsModel:
        """The rows of the "By sender" list (see SenderRowsModel)."""
        return self._sender_model

    @pyqtSlot(str, result='QVariantMap')
    def search_signals(self, query: str) -> Dict[str, List[str]]:
        """{message: [its signals whose name contains `query`]} (ignoring case), for the search field. Done here
        rather than in QML: a database of 1500 messages has some 24000 signal names."""
        query = query.strip().lower()
        if self._editor is None or not query:
            return {}
        if self._search_cache is None:
            if self.lin:
                messages = [
                    (frame['name'], [signal['name'] for signal, _ in self._editor.frame_signals(frame['name'])])
                    for frame in self._editor.frames
                ]
            else:
                messages = [
                    (message.name, [signal.name for signal in message.signals]) for message in self._editor.messages
                ]
            self._search_cache = [
                (name, [(signal.lower(), signal) for signal in signals]) for name, signals in messages
            ]
        matches = {}
        for message, signals in self._search_cache:
            found = [signal for lowered, signal in signals if query in lowered]
            if found:
                matches[message] = found
        return matches

    @pyqtSlot(str, str)
    def select(self, message: str, signal: str):
        """Select a message and one of its signals (e.g. from the search results or the list by sender)."""
        self._message = message
        signals = self._signal_names(message)
        self._signal = signal if signal in signals else signals[0] if signals else ''
        self.selection_changed.emit()

    # Undo and redo

    @pyqtProperty(bool, notify=changed)
    def can_undo(self) -> bool:
        return bool(self._editor and self._editor.history.can_undo)

    @pyqtProperty(bool, notify=changed)
    def can_redo(self) -> bool:
        return bool(self._editor and self._editor.history.can_redo)

    @pyqtSlot(result=str)
    def undo(self) -> str:
        return self._step_history(forward=False)

    @pyqtSlot(result=str)
    def redo(self) -> str:
        return self._step_history(forward=True)

    def _step_history(self, forward: bool) -> str:
        editor = self._editor
        if editor is None:
            return ''
        before = editor.state()
        if not (editor.redo() if forward else editor.undo()):
            return 'Nothing to redo' if forward else 'Nothing to undo'
        # Show what changed: select the message the step changed, else keep the selection where it still exists
        focus = editor.changed_message(before, editor.state())
        self._invalidate()
        names = self._message_names()
        message = focus if focus in names else self._message if self._message in names else names[0] if names else ''
        signals = self._signal_names(message)
        self._message = message
        self._signal = self._signal if self._signal in signals else signals[0] if signals else ''
        self.changed.emit()
        self.selection_changed.emit()
        return ''

    # Edits

    def _apply(self, edit) -> str:
        if self._editor is None:
            return 'Open or create a database first'
        try:
            with self._editor.edit_step():  # One undo step; an edit that fails is rolled back
                edit()
        except (EditError, ValueError) as e:
            return str(e)
        self._invalidate()
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
            int(definition['frame_id'])
        except (KeyError, TypeError, ValueError) as e:
            return f'Invalid signal: {e}'

        def edit():
            self._message, self._signal = self._add_found(editor, definition)

        return self._apply(edit)

    def _add_found(self, editor, definition: Dict[str, Any]):
        """Add one reverse-engineering signal (and its message if needed) to the editor; returns (message, signal)."""
        frame_id, extended = int(definition['frame_id']), bool(definition.get('extended', False))
        needed = int(definition.get('frame_length', 8))
        message = next((m for m in editor.messages if m.frame_id == frame_id and m.is_extended_frame == extended), None)
        if message is None:
            id_text = f'{frame_id:08X}' if extended else f'{frame_id:03X}'
            names = {m.name for m in editor.messages}
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
        fields = {
            key: definition[key]
            for key in ('start', 'length', 'byte_order', 'is_signed', 'scale', 'offset', 'unit', 'comment')
            if key in definition
        }
        signal = None
        try:
            # add_signal places the signal at a default position before update_signal moves it to its real one;
            # defer validation so the (possibly overlapping) default position is never checked, only the result.
            with editor.deferred():
                signal = editor.add_signal(name, definition.get('name') or None)
                signal = editor.update_signal(name, signal, **fields)
        except Exception as e:  # Invalid or overlapping signal (cantools validates on refresh): undo and report
            if signal is not None:
                editor.remove_signal(name, signal)
            raise EditError(str(e)) from e
        return name, signal

    @pyqtSlot('QVariantList', str, result=str)
    def add_found_signals(self, definitions: List[Dict[str, Any]], database_file: str = '') -> str:
        """Promote reverse-engineering results in bulk: add every signal (and its message) in one undo step, into
        the open database or, with none open, the connection's database or a new one. Returns a summary or error."""
        if not definitions:
            return 'No signals to promote'
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
        added = {'signals': 0, 'messages': set(), 'skipped': 0}

        def edit():
            for definition in definitions:
                try:
                    int(definition['frame_id'])
                except (KeyError, TypeError, ValueError):
                    added['skipped'] += 1
                    continue
                try:
                    name, signal = self._add_found(editor, dict(definition))
                    added['signals'] += 1
                    added['messages'].add(name)
                    self._message, self._signal = name, signal
                except (EditError, ValueError):
                    added['skipped'] += 1

        error = self._apply(edit)
        if error:
            return error
        if not added['signals']:
            return 'Could not promote any signal'
        summary = f'Promoted {added["signals"]} signal{"s" if added["signals"] != 1 else ""} to {len(added["messages"])} message{"s" if len(added["messages"]) != 1 else ""}'
        if added['skipped']:
            summary += f' ({added["skipped"]} skipped)'
        return summary

    @pyqtSlot(result=str)
    def remove_signal(self) -> str:
        def edit():
            names = self._signal_names(self._message)
            index = names.index(self._signal)
            self._editor.remove_signal(self._message, self._signal)
            names.pop(index)
            self._signal = names[min(index, len(names) - 1)] if names else ''

        return self._apply(edit)

    @pyqtSlot(result=str)
    def duplicate_message(self) -> str:
        """Add a copy of the selected message (CAN) after it, with the next free identifier, and select it."""
        if self.lin:
            return 'Frames of LIN databases cannot be duplicated'

        def edit():
            self._message = self._editor.duplicate_message(self._message)
            signals = self._signal_names(self._message)
            # The same signal, in the copy
            self._signal = self._signal if self._signal in signals else signals[0] if signals else ''

        return self._apply(edit)

    @pyqtSlot(result=str)
    def duplicate_signal(self) -> str:
        """Add a copy of the selected signal to its message, at the first free bits, and select it."""
        if self.lin:
            return 'Signals of LIN databases cannot be duplicated'

        def edit():
            self._signal = self._editor.copy_signal(self._message, self._signal)

        return self._apply(edit)

    @pyqtProperty(str, notify=changed)
    def copied_signal(self) -> str:
        """The name of the signal "Copy signal" holds, pasted with paste_signal ('' when none)."""
        return self._copied_signal.name if self._copied_signal is not None else ''

    @pyqtSlot(result=str)
    def copy_signal(self) -> str:
        """Hold a copy of the selected signal, to paste it into another message (of this or another database)."""
        if self.lin:
            return 'Signals of LIN databases cannot be copied'
        if self._editor is None or not self._signal:
            return 'Select a signal first'
        try:
            self._copied_signal = copy.deepcopy(self._editor.signal(self._message, self._signal))
        except EditError as e:
            return str(e)
        self.changed.emit()
        return ''

    @pyqtSlot(result=str)
    def paste_signal(self) -> str:
        """Add the copied signal to the selected message, at the first free bits, and select it."""
        if self.lin:
            return 'Signals cannot be pasted into LIN databases'
        if self._copied_signal is None:
            return 'Copy a signal first'
        if not self._message:
            return 'Select a message first'

        def edit():
            self._signal = self._editor.add_signal_copy(self._message, self._copied_signal)

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
