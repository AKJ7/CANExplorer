from typing import Callable, Iterable, List, Optional, Set

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt

from can_explorer.core.filters import MessageFilter
from can_explorer.core.frames import Frame, format_hex, frame_type
from can_explorer.core.store import IdKey, IdStatistics

ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
ALIGN_CENTER = Qt.AlignmentFlag.AlignHCenter

TEXT_ALIGNMENT_ROLE = Qt.ItemDataRole.TextAlignmentRole
RICH_TEXT_ROLE = Qt.ItemDataRole.UserRole + 1


def format_id(frame: Frame) -> str:
    msg = frame.message
    return f'{msg.arbitration_id:08X}' if msg.is_extended_id else f'{msg.arbitration_id:03X}'


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

    def is_rich(self, column: int) -> bool:
        return False


class TraceModel(FrameTableModel):
    """Chronological list of frames, bounded to `max_rows`. Rows are appended in batches."""

    COLUMNS = ('Time [s]', 'Dir', 'Type', 'ID [hex]', 'DLC', 'Data [hex]', 'Info')
    ALIGNMENTS = (ALIGN_RIGHT, ALIGN_CENTER, ALIGN_CENTER, ALIGN_RIGHT, ALIGN_RIGHT, ALIGN_LEFT, ALIGN_LEFT)

    def __init__(self, start_time: Callable[[], Optional[float]], max_rows: int, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._start_time = start_time
        self._max_rows = max_rows
        self._rows: List[Frame] = []
        self._filter = MessageFilter()

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
                return frame.info
        return ''

    def set_filter(self, message_filter: MessageFilter, frames: Iterable[Frame]) -> None:
        self._filter = message_filter
        self.rebuild(frames)

    def rebuild(self, frames: Iterable[Frame]) -> None:
        self.beginResetModel()
        self._rows = [frame for frame in frames if self._filter.matches(frame.message)][-self._max_rows :]
        self.endResetModel()

    def append_frames(self, frames: List[Frame]) -> None:
        if not self._filter.is_empty:
            frames = [frame for frame in frames if self._filter.matches(frame.message)]
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


class OverviewModel(FrameTableModel):
    """One row per (identifier, direction) with the latest data; bytes changed by the last update are bold."""

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

    def __init__(self, statistics: IdStatistics, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._statistics = statistics
        self._keys: List[IdKey] = []
        self._filter = MessageFilter()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._keys)

    def is_rich(self, column: int) -> bool:
        return column == 6

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
                return ' '.join(
                    f'<b>{byte:02X}</b>' if changed else f'{byte:02X}'
                    for byte, changed in zip(msg.data, entry.changed, strict=False)
                )
            case 7:
                return frame.info
        return ''

    def set_filter(self, message_filter: MessageFilter) -> None:
        self._filter = message_filter
        self.rebuild()

    def rebuild(self) -> None:
        self.beginResetModel()
        self._keys = sorted(
            key for key, entry in self._statistics.entries.items() if self._filter.matches(entry.last.message)
        )
        self.endResetModel()

    def refresh(self, added: bool, changed: Set[IdKey]) -> None:
        if added:
            self.rebuild()
        elif changed and self._keys:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._keys) - 1, len(self.COLUMNS) - 1))
