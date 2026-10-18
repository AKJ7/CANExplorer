from typing import Any, Callable, Dict, Iterable, List, Optional, Set

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

ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
ALIGN_CENTER = Qt.AlignmentFlag.AlignHCenter

TEXT_ALIGNMENT_ROLE = Qt.ItemDataRole.TextAlignmentRole
RICH_TEXT_ROLE = Qt.ItemDataRole.UserRole + 1

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
    """Chronological list of frames, bounded to `max_rows`. Rows are appended in batches."""

    COLUMNS = ('Time [s]', 'Dir', 'Type', 'ID [hex]', 'DLC', 'Data [hex]', 'Info')
    ALIGNMENTS = (ALIGN_RIGHT, ALIGN_CENTER, ALIGN_CENTER, ALIGN_RIGHT, ALIGN_RIGHT, ALIGN_LEFT, ALIGN_LEFT)

    def __init__(
        self,
        start_time: Callable[[], Optional[float]],
        max_rows: int,
        describe: Describe = _plain_info,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._start_time = start_time
        self._max_rows = max_rows
        self._describe = describe
        self._rows: List[Frame] = []
        self._filter = MessageFilter()

    @property
    def frames(self) -> List[Frame]:
        return self._rows

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def display(self, row: int, column: int) -> str:
        frame = self._rows[row]
        msg = frame.message
        match column:
            case 0:
                return f'{msg.timestamp - (self._start_time() or msg.timestamp):.6f}'
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

    def set_max_rows(self, max_rows: int, frames: Iterable[Frame]) -> None:
        self._max_rows = max_rows
        self.rebuild(frames)

    def set_filter(self, message_filter: MessageFilter, frames: Iterable[Frame]) -> None:
        self._filter = message_filter
        self.rebuild(frames)

    def rebuild(self, frames: Iterable[Frame]) -> None:
        self.beginResetModel()
        if self._filter.is_empty:
            rows = list(frames)
        else:
            matches = self._filter.matches
            rows = [frame for frame in frames if matches(frame.message)]
        self._rows = rows[-self._max_rows :]
        self.endResetModel()

    def refresh_descriptions(self) -> None:
        """The description column changed (e.g. a database was loaded)."""
        if self._rows:
            self.dataChanged.emit(self.index(0, 6), self.index(len(self._rows) - 1, 6))

    def append_frames(self, frames: List[Frame]) -> None:
        if not self._filter.is_empty:
            matches = self._filter.matches
            frames = [frame for frame in frames if matches(frame.message)]
        if not frames:
            return
        frames = frames[-self._max_rows :]
        overflow = len(self._rows) + len(frames) - self._max_rows
        if overflow > 0:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self._rows[:overflow]
            self.endRemoveRows()
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(frames) - 1)
        self._rows.extend(frames)
        self.endInsertRows()

    def selected_frames(self, selection_model: Optional[QItemSelectionModel]) -> List[Frame]:
        if selection_model is None or not selection_model.hasSelection():
            return list(self._rows)
        return [self._rows[row] for row in self.selected_rows(selection_model)]


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
