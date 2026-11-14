import time
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
    QTimer,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QGuiApplication

from can_explorer.core.filters import MessageFilter
from can_explorer.core.frames import Frame, format_hex, frame_type
from can_explorer.core.store import IdKey, IdStatistics
from can_explorer.core.timestamps import DEFAULT_TIME_FORMAT, DELTA, format_time, normalize_time_format, time_header
from can_explorer.core.trace_store import TraceStore

ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
ALIGN_CENTER = Qt.AlignmentFlag.AlignHCenter


def _parse_hex_color(color: str) -> Tuple[int, int, int]:
    """'#rrggbb' (or '#rgb') to an (r, g, b) tuple; mid-grey on anything unparseable."""
    text = color.lstrip('#')
    try:
        if len(text) == 3:
            return int(text[0] * 2, 16), int(text[1] * 2, 16), int(text[2] * 2, 16)
        if len(text) >= 6:
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        pass
    return (128, 128, 128)


def _lerp_color(start: Tuple[int, int, int], end: Tuple[int, int, int], fraction: float) -> str:
    fraction = max(0.0, min(1.0, fraction))
    r, g, b = (round(s + (e - s) * fraction) for s, e in zip(start, end, strict=True))
    return f'#{r:02x}{g:02x}{b:02x}'


def _fade_byte_markup(byte: int, fraction: float, highlight: Tuple[int, int, int], text: Tuple[int, int, int]) -> str:
    """HTML for one data byte at fade `fraction` (0 = just changed, 1 = fully cooled): the colour is interpolated
    from the highlight colour to the text colour, and the byte is bold while still fresh."""
    if fraction >= 1.0:
        return f'{byte:02X}'  # Fully cooled: inherit the plain text colour of the cell
    color = _lerp_color(highlight, text, fraction)
    hexbyte = f'{byte:02X}'
    return (
        f'<font color="{color}"><b>{hexbyte}</b></font>'
        if fraction < 0.5
        else f'<font color="{color}">{hexbyte}</font>'
    )


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

    @pyqtSlot(QItemSelectionModel, int, int, bool)
    def select_rows(self, selection_model: QItemSelectionModel, first: int, last: int, extend: bool = False):
        """Select the rows from `first` to `last` (in either order), replacing the selection unless `extend`."""
        first, last = sorted((first, last))
        first, last = max(first, 0), min(last, self.rowCount() - 1)
        if first > last:
            return
        selection = QItemSelection(self.index(first, 0), self.index(last, self.columnCount() - 1))
        flag = QItemSelectionModel.SelectionFlag
        selection_model.select(selection, flag.Select if extend else flag.ClearAndSelect)

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
    time_format_changed = pyqtSignal()

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
        self._time_format = DEFAULT_TIME_FORMAT

    @property
    def store(self) -> TraceStore:
        return self._store

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if section == 0 and orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return time_header(self._time_format)
        return super().headerData(section, orientation, role)

    # Time format of the first column (see core.timestamps)

    def _get_time_format(self) -> str:
        return self._time_format

    @pyqtSlot(str)
    def set_time_format(self, time_format: str) -> None:
        time_format = normalize_time_format(time_format)
        if time_format == self._time_format:
            return
        self._time_format = time_format
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, 0)
        rows = self.rowCount()
        if rows:
            self.dataChanged.emit(self.index(0, 0), self.index(rows - 1, 0))
        self.time_format_changed.emit()

    time_format = pyqtProperty(str, fget=_get_time_format, fset=set_time_format, notify=time_format_changed)

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

    def time_text(self, row: int, frame: Frame) -> str:
        """The first column: the frame's time in the chosen format."""
        previous = None
        if self._time_format == DELTA and row > 0:
            before = self.frame(row - 1)
            previous = before.message.timestamp if before is not None else None
        return format_time(self._time_format, frame.message.timestamp, self._start_time(), previous)

    def display(self, row: int, column: int) -> str:
        frame = self.frame(row)
        if frame is None:
            return ''
        msg = frame.message
        match column:
            case 0:
                return self.time_text(row, frame)
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
        if dropped and self._time_format == DELTA and self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(0, 0))  # The new first row has no previous frame

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
        self._text_color = ''  # The cell's normal text colour, for interpolating the fade
        self._fade = False
        self._fade_window = 1.5  # Seconds for a changed byte to cool from the highlight back to the text colour
        self._glow: Dict[IdKey, List[float]] = {}  # Per-byte monotonic time of last change, while fade is on
        self._newest_glow = 0.0
        self._clock = time.monotonic  # Indirection so tests can supply a deterministic clock
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(66)  # ~15 Hz repaint while bytes are cooling
        self._fade_timer.timeout.connect(self._tick_fade)

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

    def _format_data(self, data: bytes, changed: Iterable[bool], key: Optional[IdKey] = None) -> str:
        glow = self._glow.get(key) if self._fade and key is not None else None
        if glow is not None and self._highlight_color and self._text_color:
            now = self._clock()
            highlight = _parse_hex_color(self._highlight_color)
            text = _parse_hex_color(self._text_color)
            parts = []
            for index, byte in enumerate(data):
                stamped = glow[index] if index < len(glow) else 0.0
                fraction = 1.0 if stamped <= 0.0 else min(1.0, (now - stamped) / self._fade_window)
                parts.append(_fade_byte_markup(byte, fraction, highlight, text))
            return ' '.join(parts)
        # Instant highlight (fade off, or colours/glow not yet available): changed bytes in the highlight colour
        if self._highlight_color:
            marked = f'<font color="{self._highlight_color}"><b>{{:02X}}</b></font>'
        else:
            marked = '<b>{:02X}</b>'
        return ' '.join(
            marked.format(byte) if is_changed else f'{byte:02X}'
            for byte, is_changed in zip(data, changed, strict=False)
        )

    # Fade mode: changed bytes glow and cool over a window, rather than a binary highlight

    def get_text_color(self) -> str:
        return self._text_color

    def set_text_color(self, color: str) -> None:
        self._text_color = color

    text_color = pyqtProperty(str, get_text_color, set_text_color)

    fade_changed = pyqtSignal()

    def get_fade(self) -> bool:
        return self._fade

    fade = pyqtProperty(bool, get_fade, notify=fade_changed)

    @pyqtSlot(bool)
    def set_fade(self, on: bool) -> None:
        """Turn the fading change-highlight on or off."""
        on = bool(on)
        if on == self._fade:
            return
        self._fade = on
        self._glow.clear()  # Start fresh: bytes light up as they change from now on
        if on:
            self._fade_timer.start()
        else:
            self._fade_timer.stop()
        self._repaint_data()
        self.fade_changed.emit()

    def _repaint_data(self) -> None:
        if self._keys:
            column = self.DATA_COLUMN
            self.dataChanged.emit(self.index(0, column), self.index(len(self._keys) - 1, column))

    def _stamp_glow(self, key: IdKey, changed: Tuple[bool, ...], now: float) -> None:
        glow = self._glow.get(key)
        if glow is None or len(glow) != len(changed):
            glow = self._glow[key] = [0.0] * len(changed)
        for index, is_changed in enumerate(changed):
            if is_changed:
                glow[index] = now
        self._newest_glow = now

    def _tick_fade(self) -> None:
        if not self._fade:
            self._fade_timer.stop()
            return
        self._repaint_data()
        if self._clock() - self._newest_glow > self._fade_window:
            self._fade_timer.stop()  # Everything has cooled; stop repainting until the next change

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
                return self._format_data(msg.data, entry.changed, self._keys[row])
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
        if self._fade and changed:
            now = self._clock()
            for key in changed:
                entry = self._statistics.entries.get(key)
                if entry is not None:
                    self._stamp_glow(key, entry.changed, now)
            if not self._fade_timer.isActive():
                self._fade_timer.start()
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
