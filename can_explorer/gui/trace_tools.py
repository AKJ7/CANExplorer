"""Searching the trace and bookmarking frames."""

import asyncio
from typing import Any, Callable, Dict, List, Optional

import can
from PyQt6.QtCore import QItemSelectionModel, QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.conditions import ConditionError, parse_condition
from can_explorer.core.frames import Frame, format_hex
from can_explorer.gui.models import TraceModel, format_id

SEARCH_CHUNK = 2000  # Rows checked between giving the event loop back to the GUI


def frame_matcher(query: str, database: Callable[[], Any], describe: Callable[[Frame], str]) -> Callable[[Frame], bool]:
    """A predicate for a search: a condition (see core/conditions.py), or else text in the ID, data or info.
    Quotes force a text search ('"BEEF"' finds the text, not the identifier 0xBEEF)."""
    query = query.strip()
    quoted = len(query) >= 2 and query[0] == query[-1] and query[0] in '"\''
    if not quoted:
        try:
            condition = parse_condition(query)
        except ConditionError:
            pass
        else:
            return lambda frame: condition.matches(frame.message, database())
    text = (query[1:-1] if quoted else query).lower()

    def contains(frame: Frame) -> bool:
        msg: can.Message = frame.message
        return (
            text in format_id(frame).lower() or text in format_hex(msg.data).lower() or text in describe(frame).lower()
        )

    return contains


class TraceTools(QObject):
    """Search and bookmarks of a connection's trace view."""

    search_changed = pyqtSignal()
    search_finished = pyqtSignal(int, str)  # Row (-1: none) and a message
    bookmarks_changed = pyqtSignal()

    def __init__(self, model: TraceModel, database: Callable[[], Any], describe: Callable[[Frame], str], parent=None):
        super().__init__(parent)
        self.model = model
        self._database = database
        self._describe = describe
        self._task: Optional[asyncio.Task] = None
        self._status = ''
        model.bookmarks_changed.connect(self.bookmarks_changed)

    # Search

    @pyqtProperty(bool, notify=search_changed)
    def searching(self) -> bool:
        return self._task is not None and not self._task.done()

    @pyqtProperty(str, notify=search_changed)
    def status(self) -> str:
        return self._status

    def _set_status(self, text: str) -> None:
        self._status = text
        self.search_changed.emit()

    @pyqtSlot(str, int, bool)
    def find(self, query: str, from_row: int, forward: bool):
        """Search from the row after (or before) `from_row`, wrapping around; the result arrives as
        search_finished. A new search replaces a running one."""
        self.cancel()
        if not query.strip():
            self._set_status('')
            return
        matcher = frame_matcher(query, self._database, self._describe)
        self._task = asyncio.ensure_future(self._search(matcher, from_row, forward))
        self._task.add_done_callback(lambda _: self.search_changed.emit())  # `searching` is over
        self._set_status('Searching…')

    @pyqtSlot()
    def cancel(self):
        if self.searching:
            self._task.cancel()
            self._set_status('Search cancelled')

    async def _search(self, matcher: Callable[[Frame], bool], from_row: int, forward: bool) -> None:
        model = self.model
        rows = model.rowCount()
        if rows == 0:
            self._finish(-1, 'The trace is empty')
            return
        step = 1 if forward else -1
        start = from_row if 0 <= from_row < rows else (-1 if forward else rows)
        checked = 0
        row = start
        try:
            for checked in range(1, rows + 1):
                row = (row + step) % rows
                frame = model.frame(row)
                if frame is not None and matcher(frame):
                    wrapped = (forward and row <= start) or (not forward and row >= start)
                    self._finish(row, f'Row {row + 1:,} of {rows:,}' + (' (wrapped around)' if wrapped else ''))
                    return
                if checked % SEARCH_CHUNK == 0:
                    self._set_status(f'Searching… {checked * 100 // rows} %')
                    await asyncio.sleep(0)
                    rows = min(rows, model.rowCount())  # Rows dropped meanwhile
        except Exception as e:  # E.g. frames that cannot be decoded
            self._finish(-1, f'Search failed: {e}')
            return
        self._finish(-1, 'No match')

    def _finish(self, row: int, message: str) -> None:
        self._status = message
        self.search_changed.emit()
        self.search_finished.emit(row, message)

    # Bookmarks

    @pyqtProperty(int, notify=bookmarks_changed)
    def bookmark_count(self) -> int:
        return len(self.model.bookmarks)

    @pyqtProperty('QVariantList', notify=bookmarks_changed)
    def bookmarks(self) -> List[Dict[str, Any]]:
        result = []
        for index, note, frame in self.model.bookmarks:
            result.append(
                {
                    'index': index,
                    'note': note,
                    'available': frame is not None,
                    'time': f'{self.model.time(frame):.6f}' if frame is not None else '',
                    'id': format_id(frame) if frame is not None else '',
                    'info': self._describe(frame) if frame is not None else 'no longer in the trace',
                    'row': self.model.row_of(index),
                }
            )
        return result

    @pyqtSlot(QItemSelectionModel, result=int)
    def toggle_bookmarks(self, selection: QItemSelectionModel) -> int:
        return self.model.toggle_bookmarks(selection)

    @pyqtSlot(int, str)
    def set_note(self, index: int, note: str):
        self.model.set_bookmark_note(index, note)

    @pyqtSlot(int)
    def remove(self, index: int):
        self.model.remove_bookmark(index)

    @pyqtSlot()
    def clear(self):
        self.model.clear_bookmarks()

    @pyqtSlot(int, result=int)
    def row_of(self, index: int) -> int:
        """The row of a bookmarked frame in the view (-1 when filtered out or dropped)."""
        return self.model.row_of(index)
