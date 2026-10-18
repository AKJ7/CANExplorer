from array import array
from bisect import bisect_left
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from PyQt6.QtCore import (
    QAbstractTableModel,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    Qt,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QGuiApplication

from can_explorer.core.filters import MessageFilter
from can_explorer.core.frames import Frame, format_hex, frame_type
from can_explorer.core.store import IdKey, IdStatistics
from can_explorer.core.trace_store import TraceStore

ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
ALIGN_CENTER = Qt.AlignmentFlag.AlignHCenter

TEXT_ALIGNMENT_ROLE = Qt.ItemDataRole.TextAlignmentRole
RICH_TEXT_ROLE = Qt.ItemDataRole.UserRole + 1
BOOKMARK_ROLE = Qt.ItemDataRole.UserRole + 2

Describe = Callable[[Frame], str]


def format_id(frame: Frame) -> str:
    msg = frame.message
    return f'{msg.arbitration_id:08X}' if msg.is_extended_id else f'{msg.arbitration_id:03X}'


def _plain_info(frame: Frame) -> str:
    return frame.info


class FrameTableModel(QAbstractTableModel):
    """Base for read-only tables exposing 'display', 'textAlignment' and 'richText' roles to QML."""

    COLUMNS: tuple = ()
    ALIGNMENTS: tuple = ()

    def roleNames(self):
        roles = super().roleNames()
        roles[TEXT_ALIGNMENT_ROLE] = b'textAlignment'
        roles[RICH_TEXT_ROLE] = b'richText'
        roles[BOOKMARK_ROLE] = b'bookmarked'
        return roles

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return super().headerData(section, orientation, role)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def flags(self, index: QModelIndex):
        return Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.display(index.row(), index.column())
        if role == TEXT_ALIGNMENT_ROLE:
            return (self.ALIGNMENTS[index.column()] | Qt.AlignmentFlag.AlignVCenter).value
        if role == RICH_TEXT_ROLE:
            return self.is_rich(index.column())
        if role == BOOKMARK_ROLE:
            return False
        return None

    def display(self, row: int, column: int) -> str:
        raise NotImplementedError

    def plain(self, row: int, column: int) -> str:
        """Cell text without markup, used when copying."""
        return self.display(row, column)

    def is_rich(self, column: int) -> bool:
        return False

    # Selection and clipboard

    @pyqtSlot(QItemSelectionModel)
    def select_all(self, selection_model: QItemSelectionModel):
        if self.rowCount() == 0:
            return
        selection = QItemSelection(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1))
        selection_model.select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    @staticmethod
    def selected_rows(selection_model: QItemSelectionModel) -> List[int]:
        return sorted({index.row() for index in selection_model.selectedIndexes()})

    def rows_as_text(self, rows: Iterable[int]) -> str:
        lines = ['\t'.join(self.COLUMNS)]
        lines += ['\t'.join(self.plain(row, column) for column in range(len(self.COLUMNS))) for row in rows]
        return '\n'.join(lines)

    @pyqtSlot(QItemSelectionModel, result=int)
    def copy_selection(self, selection_model: QItemSelectionModel) -> int:
        """Copy the selected rows as tab separated text. Returns the number of rows copied."""
        rows = self.selected_rows(selection_model)
        if rows:
            QGuiApplication.clipboard().setText(self.rows_as_text(rows))
        return len(rows)


class TraceModel(FrameTableModel):
    """View of the trace store: all retained frames, or those matching the display filter.

    Rows refer to frames by their absolute index in the store, so the view can page through frames on disk.
    `refresh()` follows the store (new frames at the end, dropped ones at the front); without it, e.g. while the
    view is paused, the rows stay as they are. Frames dropped from the store meanwhile show empty rows.
    Bookmarks mark frames by absolute index.
    """

    COLUMNS = ('Time [s]', 'Dir', 'Type', 'ID [hex]', 'DLC', 'Data [hex]', 'Info')
    ALIGNMENTS = (ALIGN_RIGHT, ALIGN_CENTER, ALIGN_CENTER, ALIGN_RIGHT, ALIGN_RIGHT, ALIGN_LEFT, ALIGN_LEFT)
    INFO_COLUMN = 6

    bookmarks_changed = pyqtSignal()

    def __init__(
        self,
        store: TraceStore,
        start_time: Callable[[], Optional[float]],
        describe: Describe = _plain_info,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._store = store
        self._start_time = start_time
        self._describe = describe
        self._filter = MessageFilter()
        self._indices: Optional[array] = None  # Absolute indices of the matching frames; None: all frames
        self._first = 0  # Without a filter: the absolute index of row 0 ...
        self._count = 0  # ... and the number of rows
        self._scanned = 0  # With a filter: frames before this absolute index were checked
        self._bookmarks: Dict[int, str] = {}  # Absolute index -> note

    @property
    def store(self) -> TraceStore:
        return self._store

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if role == BOOKMARK_ROLE:
            return index.isValid() and self.absolute_index(index.row()) in self._bookmarks
        return super().data(index, role)

    # Rows

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._indices) if self._indices is not None else self._count

    def absolute_index(self, row: int) -> int:
        if self._indices is not None:
            return self._indices[row] if 0 <= row < len(self._indices) else -1
        return self._first + row if 0 <= row < self._count else -1

    def row_of(self, absolute: int) -> int:
        """The row showing a frame, or -1."""
        if self._indices is not None:
            row = bisect_left(self._indices, absolute)
            return row if row < len(self._indices) and self._indices[row] == absolute else -1
        row = absolute - self._first
        return row if 0 <= row < self._count else -1

    def frame(self, row: int) -> Optional[Frame]:
        index = self.absolute_index(row)
        return self._store.get(index) if index >= 0 else None

    def time(self, frame: Frame) -> float:
        """Seconds since the start of the session."""
        msg = frame.message
        start = self._start_time()
        return msg.timestamp - (msg.timestamp if start is None else start)

    def display(self, row: int, column: int) -> str:
        frame = self.frame(row)
        if frame is None:
            return ''
        msg = frame.message
        match column:
            case 0:
                return f'{self.time(frame):.6f}'
            case 1:
                return 'Rx' if msg.is_rx else 'Tx'
            case 2:
                return frame_type(msg)
            case 3:
                return format_id(frame)
            case 4:
                return str(msg.dlc)
            case 5:
                return format_hex(msg.data)
            case 6:
                return self._describe(frame)
        return ''

    # Following the store

    def set_filter(self, message_filter: MessageFilter) -> None:
        self._filter = message_filter
        self.rebuild()

    def rebuild(self) -> None:
        """Show the store's current frames again (after a filter change or clearing)."""
        self.beginResetModel()
        store = self._store
        if self._filter.is_empty:
            self._indices = None
            self._first, self._count = store.first, len(store)
        else:
            matches = self._filter.matches
            self._indices = array(
                'Q', (index for index, frame in enumerate(store, start=store.first) if matches(frame.message))
            )
        self._scanned = store.total
        if store.total == 0:
            self._bookmarks.clear()
            self.bookmarks_changed.emit()
        self.endResetModel()

    def refresh(self) -> None:
        """Remove the rows of frames dropped from the store and add rows for new frames."""
        store = self._store
        first, total = store.first, store.total
        if total < self._scanned or (self._indices is None and total < self._first + self._count):
            self.rebuild()  # The store was cleared
            return
        if self._indices is None:
            dropped = min(max(0, first - self._first), self._count)
            if dropped:
                self.beginRemoveRows(QModelIndex(), 0, dropped - 1)
                self._first += dropped
                self._count -= dropped
                self.endRemoveRows()
            if self._count == 0:
                self._first = max(self._first, first)
            new = total - (self._first + self._count)
            if new > 0:
                self.beginInsertRows(QModelIndex(), self._count, self._count + new - 1)
                self._count += new
                self.endInsertRows()
        else:
            dropped = bisect_left(self._indices, first)
            if dropped:
                self.beginRemoveRows(QModelIndex(), 0, dropped - 1)
                del self._indices[:dropped]
                self.endRemoveRows()
            matches = self._filter.matches
            new = [
                index
                for index, frame in enumerate(
                    store.iter_from(max(self._scanned, first)), start=max(self._scanned, first)
                )
                if matches(frame.message)
            ]
            if new:
                rows = len(self._indices)
                self.beginInsertRows(QModelIndex(), rows, rows + len(new) - 1)
                self._indices.extend(new)
                self.endInsertRows()
        self._scanned = total

    def refresh_descriptions(self) -> None:
        """The description column changed (e.g. a database was loaded)."""
        rows = self.rowCount()
        if rows:
            self.dataChanged.emit(self.index(0, self.INFO_COLUMN), self.index(rows - 1, self.INFO_COLUMN))

    # Selection and export

    def selected_frames(self, selection_model: Optional[QItemSelectionModel]) -> Iterator[Frame]:
        """The selected frames, or all rows without a selection (frames dropped from the store are skipped)."""
        if selection_model is None or not selection_model.hasSelection():
            rows: Iterable[int] = range(self.rowCount())
        else:
            rows = self.selected_rows(selection_model)
        for row in rows:
            frame = self.frame(row)
            if frame is not None:
                yield frame

    # Bookmarks

    @pyqtSlot(QItemSelectionModel, result=int)
    def toggle_bookmarks(self, selection_model: QItemSelectionModel) -> int:
        """Bookmark the selected rows, or remove their bookmarks when all of them have one. Returns the count."""
        indices = [self.absolute_index(row) for row in self.selected_rows(selection_model)]
        indices = [index for index in indices if index >= 0]
        if not indices:
            return 0
        if all(index in self._bookmarks for index in indices):
            for index in indices:
                del self._bookmarks[index]
        else:
            for index in indices:
                self._bookmarks.setdefault(index, '')
        self._bookmarks_changed(indices)
        return len(indices)

    def set_bookmark_note(self, absolute: int, note: str) -> None:
        if absolute in self._bookmarks:
            self._bookmarks[absolute] = note
            self.bookmarks_changed.emit()

    def remove_bookmark(self, absolute: int) -> None:
        if self._bookmarks.pop(absolute, None) is not None:
            self._bookmarks_changed([absolute])

    def clear_bookmarks(self) -> None:
        indices = list(self._bookmarks)
        self._bookmarks.clear()
        self._bookmarks_changed(indices)

    def _bookmarks_changed(self, indices: Iterable[int]) -> None:
        last = len(self.COLUMNS) - 1
        for index in indices:
            row = self.row_of(index)
            if row >= 0:
                self.dataChanged.emit(self.index(row, 0), self.index(row, last), [BOOKMARK_ROLE])
        self.bookmarks_changed.emit()

    @property
    def bookmarks(self) -> List[Tuple[int, str, Optional[Frame]]]:
        """(absolute index, note, frame or None when no longer in the store), oldest first."""
        return [(index, self._bookmarks[index], self._store.get(index)) for index in sorted(self._bookmarks)]


class OverviewModel(FrameTableModel):
    """One row per (identifier, direction) with the latest data; bytes that differ from the previous frame of the
    identifier are highlighted. Sorted by identifier, or by the column chosen with `sort_by`."""

    COLUMNS = ('ID [hex]', 'Dir', 'Type', 'Count', 'Cycle [ms]', 'DLC', 'Data [hex]', 'Info')
    ALIGNMENTS = (
        ALIGN_RIGHT,
        ALIGN_CENTER,
        ALIGN_CENTER,
        ALIGN_RIGHT,
        ALIGN_RIGHT,
        ALIGN_RIGHT,
        ALIGN_LEFT,
        ALIGN_LEFT,
    )
    DATA_COLUMN = 6
    INFO_COLUMN = 7
    # Columns whose values change with new frames of a row; sorting by them reorders the rows on updates
    VOLATILE_COLUMNS = frozenset((3, 4, 5, 6, 7))

    highlight_color_changed = pyqtSignal()

    def __init__(self, statistics: IdStatistics, describe: Describe = _plain_info, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._statistics = statistics
        self._describe = describe
        self._keys: List[IdKey] = []
        self._rows_by_key: Dict[IdKey, int] = {}
        self._filter = MessageFilter()
        self._sort_column = -1  # -1: by identifier
        self._sort_descending = False
        self._highlight_color = ''

    # Highlighting

    def get_highlight_color(self) -> str:
        return self._highlight_color

    def set_highlight_color(self, color: str) -> None:
        if color != self._highlight_color:
            self._highlight_color = color
            self.highlight_color_changed.emit()
            if self._keys:
                column = self.DATA_COLUMN
                self.dataChanged.emit(self.index(0, column), self.index(len(self._keys) - 1, column))

    highlight_color = pyqtProperty(
        str, get_highlight_color, set_highlight_color, notify=highlight_color_changed
    )  # Colour of changed bytes, e.g. '#f39c12'; bold only when empty

    def _format_data(self, data: bytes, changed: Iterable[bool]) -> str:
        if self._highlight_color:
            marked = f'<font color="{self._highlight_color}"><b>{{:02X}}</b></font>'
        else:
            marked = '<b>{:02X}</b>'
        return ' '.join(
            marked.format(byte) if is_changed else f'{byte:02X}'
            for byte, is_changed in zip(data, changed, strict=False)
        )

    # Sorting

    @property
    def sort_column(self) -> int:
        return self._sort_column

    @property
    def sort_descending(self) -> bool:
        return self._sort_descending

    @pyqtSlot(int)
    def sort_by(self, column: int) -> None:
        """Header click: sort ascending by the column, then descending, then back to the identifier order."""
        if column != self._sort_column:
            self._sort_column, self._sort_descending = column, False
        elif not self._sort_descending:
            self._sort_descending = True
        else:
            self._sort_column, self._sort_descending = -1, False
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.COLUMNS) - 1)
        self._reorder()

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and section == self._sort_column
        ):
            return f'{self.COLUMNS[section]} {"▼" if self._sort_descending else "▲"}'
        return super().headerData(section, orientation, role)

    def _sort_value(self, key: IdKey) -> Any:
        entry = self._statistics.entries[key]
        msg = entry.last.message
        match self._sort_column:
            case 0:
                return msg.is_extended_id, msg.arbitration_id
            case 1:
                return msg.is_rx
            case 2:
                return frame_type(msg)
            case 3:
                return entry.count
            case 4:  # Unknown cycle times last
                return (entry.cycle_time is None) != self._sort_descending, entry.cycle_time or 0.0
            case 5:
                return msg.dlc
            case 6:
                return bytes(msg.data)
            case 7:
                return self._describe(entry.last)
        return 0

    def _ordered(self, keys: Iterable[IdKey]) -> List[IdKey]:
        keys = sorted(keys)  # Identifier order, also the tie breaker of the other columns
        if self._sort_column >= 0:
            keys.sort(key=self._sort_value, reverse=self._sort_descending)
        return keys

    def _reorder(self) -> None:
        """Apply the sort order, keeping selections (persistent indexes) on their rows."""
        keys = self._ordered(self._keys)
        if keys == self._keys:
            return
        self.layoutAboutToBeChanged.emit()
        old_keys = self._keys
        self._keys = keys
        self._rows_by_key = {key: row for row, key in enumerate(keys)}
        persistent = self.persistentIndexList()
        self.changePersistentIndexList(
            persistent,
            [self.index(self._rows_by_key[old_keys[index.row()]], index.column()) for index in persistent],
        )
        self.layoutChanged.emit()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._keys)

    def is_rich(self, column: int) -> bool:
        return column == self.DATA_COLUMN

    def display(self, row: int, column: int) -> str:
        entry = self._statistics.entries.get(self._keys[row])
        if entry is None:
            return ''
        frame = entry.last
        msg = frame.message
        match column:
            case 0:
                return format_id(frame)
            case 1:
                return 'Rx' if msg.is_rx else 'Tx'
            case 2:
                return frame_type(msg)
            case 3:
                return str(entry.count)
            case 4:
                return '' if entry.cycle_time is None else f'{entry.cycle_time * 1000:.1f}'
            case 5:
                return str(msg.dlc)
            case 6:
                return self._format_data(msg.data, entry.changed)
            case 7:
                return self._describe(frame)
        return ''

    def plain(self, row: int, column: int) -> str:
        if column == self.DATA_COLUMN:
            entry = self._statistics.entries.get(self._keys[row])
            return format_hex(entry.last.message.data) if entry else ''
        return self.display(row, column)

    def set_filter(self, message_filter: MessageFilter) -> None:
        self._filter = message_filter
        self.rebuild()

    def rebuild(self) -> None:
        self.beginResetModel()
        matches = self._filter.matches
        self._keys = self._ordered(
            key for key, entry in self._statistics.entries.items() if matches(entry.last.message)
        )
        self._rows_by_key = {key: row for row, key in enumerate(self._keys)}
        self.endResetModel()

    def refresh(self, added: bool, changed: Set[IdKey]) -> None:
        if added:
            self.rebuild()
            return
        if changed and self._sort_column in self.VOLATILE_COLUMNS:
            self._reorder()
        rows = sorted(row for row in map(self._rows_by_key.get, changed) if row is not None)
        if not rows:
            return
        last_column = len(self.COLUMNS) - 1
        # Emit one dataChanged per run of consecutive rows
        start = previous = rows[0]
        for row in rows[1:] + [None]:
            if row is not None and row == previous + 1:
                previous = row
                continue
            self.dataChanged.emit(self.index(start, 0), self.index(previous, last_column))
            if row is not None:
                start = previous = row

    def refresh_descriptions(self) -> None:
        if self._sort_column == self.INFO_COLUMN:
            self._reorder()
        if self._keys:
            self.dataChanged.emit(self.index(0, 7), self.index(len(self._keys) - 1, 7))
