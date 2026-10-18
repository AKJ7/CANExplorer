import heapq
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.filters import MessageFilter
from can_explorer.core.frames import Frame
from can_explorer.core.trace_store import TraceStore
from can_explorer.gui.models import TraceModel

REFRESH_MS = 100
DEFAULT_ROWS = 200_000


@dataclass(slots=True)
class BusFrame(Frame):
    """A frame of one of the merged connections."""

    bus: str = ''


class MergedTraceModel(TraceModel):
    """Trace of several connections on one timeline, with the connection in the 'Bus' column."""

    COLUMNS = ('Time [s]', 'Bus', 'Dir', 'Type', 'ID [hex]', 'DLC', 'Data [hex]', 'Info')
    ALIGNMENTS = (TraceModel.ALIGNMENTS[0], TraceModel.ALIGNMENTS[6], *TraceModel.ALIGNMENTS[1:])
    INFO_COLUMN = 7

    def display(self, row: int, column: int) -> str:
        if column == 1:
            frame = self.frame(row)
            return frame.bus if isinstance(frame, BusFrame) else ''
        return super().display(row, column if column == 0 else column - 1)


class MultiBusController(QObject):
    """Several connections in one trace, ordered by time, e.g. both sides of a gateway."""

    changed = pyqtSignal()
    filter_changed = pyqtSignal()

    def __init__(self, manager, rows: int = DEFAULT_ROWS, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._manager = manager
        self._selected: List[str] = []
        self._cursors: Dict[str, int] = {}  # Connection name -> absolute index of its next frame to merge
        self._store = TraceStore(rows)
        self._trace = MergedTraceModel(self._store, self._start_time, self._describe, parent=self)
        self._paused = False
        self._filter_text = ''
        self._filter_error = ''
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.poll)
        manager.connections_changed.connect(self._connections_changed)

    def _connections(self) -> Dict[str, Any]:
        result = {}
        for connection in self._manager.connections:
            result.setdefault(connection.name, connection)
        return result

    def _start_time(self) -> Optional[float]:
        connections = self._connections()
        starts = [
            connections[name].session.start_time
            for name in self._selected
            if name in connections and connections[name].session.start_time is not None
        ]
        return min(starts) if starts else None

    def _describe(self, frame: Frame) -> str:
        connection = self._connections().get(getattr(frame, 'bus', ''))
        return connection.session.decoder.full_description(frame) if connection is not None else frame.info

    # Properties

    @pyqtProperty(QObject, constant=True)
    def trace(self) -> MergedTraceModel:
        return self._trace

    @pyqtProperty('QVariantList', notify=changed)
    def connections(self) -> List[Dict[str, Any]]:
        return [
            {'name': name, 'selected': name in self._selected, 'description': connection.description}
            for name, connection in self._connections().items()
        ]

    @pyqtProperty('QStringList', notify=changed)
    def selected(self) -> List[str]:
        return list(self._selected)

    @pyqtProperty(int, notify=changed)
    def frame_count(self) -> int:
        return len(self._store)

    @pyqtProperty(bool, notify=changed)
    def paused(self) -> bool:
        return self._paused

    @pyqtProperty(str, notify=filter_changed)
    def filter_text(self) -> str:
        return self._filter_text

    @pyqtProperty(str, notify=filter_changed)
    def filter_error(self) -> str:
        return self._filter_error

    # Selection

    @pyqtSlot(str, bool)
    def set_selected(self, name: str, selected: bool):
        if selected and name not in self._selected and name in self._connections():
            self._selected.append(name)
        elif not selected and name in self._selected:
            self._selected.remove(name)
        else:
            return
        self.rebuild()

    def _connections_changed(self):
        names = self._connections()
        removed = [name for name in self._selected if name not in names]
        for name in removed:
            self._selected.remove(name)
        if removed:
            self.rebuild()
        else:
            self.changed.emit()

    def _frames(self, name: str, store: TraceStore, start: int) -> Iterator[BusFrame]:
        for frame in store.iter_from(start):
            yield BusFrame(frame.message, frame.info, None, name)

    def rebuild(self):
        """Merge the retained frames of the selected connections again (after the selection changed)."""
        self._store.clear()
        connections = self._connections()
        sources = []
        for name in self._selected:
            store = connections[name].session.trace
            start = max(store.first, store.total - self._store.maxlen)  # Only what the merged view can hold
            sources.append(self._frames(name, store, start))
            self._cursors[name] = store.total
        merged = heapq.merge(*sources, key=lambda frame: frame.message.timestamp)
        for frame in merged:
            self._store.append(frame)
        self._trace.rebuild()
        if self._selected:
            self._timer.start()
        else:
            self._timer.stop()
        self.changed.emit()

    @pyqtSlot()
    def poll(self):
        """Merge the frames that arrived since the last poll, ordered by time."""
        if self._paused:
            return
        connections = self._connections()
        batch: List[BusFrame] = []
        for name in self._selected:
            store = connections[name].session.trace
            cursor = self._cursors.get(name, store.total)
            if store.total < cursor:  # The connection's trace was cleared
                cursor = store.first
            batch.extend(self._frames(name, store, max(cursor, store.first)))
            self._cursors[name] = store.total
        if not batch:
            return
        batch.sort(key=lambda frame: frame.message.timestamp)
        for frame in batch:
            self._store.append(frame)
        self._trace.refresh()
        self.changed.emit()

    # Toolbar

    @pyqtSlot(bool)
    def set_paused(self, paused: bool):
        self._paused = paused
        if not paused:
            self.poll()
        self.changed.emit()

    @pyqtSlot()
    def clear(self):
        """Empty the view; it continues with new frames."""
        self._store.clear()
        connections = self._connections()
        for name in self._selected:
            self._cursors[name] = connections[name].session.trace.total
        self._trace.rebuild()
        self.changed.emit()

    @pyqtSlot(str)
    def set_filter(self, text: str):
        try:
            message_filter = MessageFilter.parse(text)
        except ValueError as e:
            self._filter_error = str(e)
        else:
            self._filter_text, self._filter_error = text, ''
            self._trace.set_filter(message_filter)
        self.filter_changed.emit()
