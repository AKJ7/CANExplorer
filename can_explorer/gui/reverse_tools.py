"""The tools of the Reverse engineering page working on the frames of the trace: the automatic analysis, finding a
field matching a reference signal, and finding the bits changing at event markers. They run in a worker thread."""

import asyncio
import time
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.reverse.analysis import FoundField, analyse_all, collect
from can_explorer.core.reverse.markers import Marker, MarkerMatch, find_marker_changes
from can_explorer.core.reverse.obd import PIDS, ObdPoller, is_obd_identifier
from can_explorer.core.reverse.reference import (
    Match,
    Reference,
    candidates,
    correlate,
    read_reference_csv,
    reference_from_signal,
)
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

PROGRESS_INTERVAL_MS = 200
WEAK_CORRELATION = 0.8  # Below this, the best match is reported as no match
KIND_NAMES = {
    'checksum': 'Checksum',
    'counter': 'Counter',
    'multiplexer': 'Multiplexer',
    'physical': 'Value',
    'enum': 'State',
    'flag': 'Flag',
    'constant': 'Constant',
    'unknown': 'Unknown',
}


def _local_path(url: QUrl | str) -> str:
    if isinstance(url, QUrl):
        return url.toLocalFile() if url.isLocalFile() else url.toString()
    return str(url)


def _id_text(frame_id: int, extended: bool) -> str:
    return f'{frame_id:08X}' if extended else f'{frame_id:03X}'


def _signal_name(prefix: str, field) -> str:
    return f'{prefix}_{field.id_text}_{field.start}'


class _Tool(QObject):
    """Common to the tools: the page controller, a running flag and a status text. Each tool declares its own
    `changed` signal and the `running` and `status` properties (PyQt needs the notify signal in the same class)."""

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.reverse = reverse
        self._running = False
        self._status = ''
        self._task: Optional[asyncio.Future] = None

    def _get_running(self) -> bool:
        return self._running

    def _get_status(self) -> str:
        return self._status

    def _start(self, coroutine) -> None:
        self._running = True
        self.changed.emit()
        self._task = asyncio.ensure_future(self._guarded(coroutine))

    async def _guarded(self, coroutine) -> None:
        try:
            await coroutine
        except Exception as e:  # A failure must not leave the tool running for ever
            logger.exception('Reverse engineering tool failed')
            self._status = f'Failed: {e}'
        finally:
            self._running = False
            self.changed.emit()


class AnalysisController(_Tool):
    """Finding the fields of every identifier (or of the selected one) and what kind they are."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._found: List[tuple] = []  # (analysis, found field or None for a note), as listed
        self._progress = (0, 0)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(PROGRESS_INTERVAL_MS)
        self._progress_timer.timeout.connect(self._show_progress)

    def _show_progress(self) -> None:
        done, total = self._progress
        if total:
            self._status = f'Analysing… {done} of {total} identifiers'
            self.changed.emit()

    @pyqtSlot(bool)
    def run(self, selected_only: bool):
        """Analyse the frames in the trace, of all identifiers or only the selected one."""
        if not self._running:
            self._start(self._run(selected_only))

    async def _run(self, selected_only: bool) -> None:
        self._status = 'Reading the trace…'
        self.changed.emit()
        messages = await self.reverse.snapshot()
        selected = self.reverse.selected
        if selected_only and selected:
            key = (int(selected, 16), len(selected) > 3)
            messages = [msg for msg in messages if (msg.arbitration_id, msg.is_extended_id) == key]
        frames = await asyncio.to_thread(collect, messages)
        frames = {key: item for key, item in frames.items() if not is_obd_identifier(key)}
        self._progress = (0, len(frames))
        self._progress_timer.start()
        started = time.perf_counter()
        try:
            analyses = await asyncio.to_thread(
                analyse_all, frames, lambda done, total: setattr(self, '_progress', (done, total))
            )
        finally:
            self._progress_timer.stop()
        fields = sum(1 for analysis in analyses for found in analysis.fields if found.kind != 'constant')
        self._status = (
            f'{fields} fields in {len(analyses)} identifiers ({sum(a.frames for a in analyses):,} frames, '
            f'{time.perf_counter() - started:.1f} s)'
        )
        if selected_only and selected:
            keys = {analysis.key for analysis in analyses}
            analyses = [analysis for analysis in self.reverse.analyses if analysis.key not in keys] + analyses
            analyses.sort(key=lambda analysis: (analysis.key[1], analysis.key[0]))
        self.reverse.analyses = analyses
        self._found = []
        for analysis in analyses:
            if analysis.note:
                self._found.append((analysis, None))
            for found in analysis.fields:
                self._found.append((analysis, found))

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        rows = []
        for index, (analysis, found) in enumerate(self._found):
            id_text = _id_text(*analysis.key)
            if found is None:
                rows.append(
                    {
                        'index': index,
                        'id': id_text,
                        'key': '',
                        'kind': '',
                        'kind_name': '',
                        'confidence': 0,
                        'details': analysis.note,
                        'condition': '',
                    }
                )
                continue
            rows.append(
                {
                    'index': index,
                    'id': id_text,
                    'key': found.field.key,
                    'kind': found.kind,
                    'kind_name': KIND_NAMES.get(found.kind, found.kind),
                    'confidence': round(found.confidence * 100),
                    'details': found.details,
                    'condition': f'if {found.multiplexer.key} = {found.mux_value}' if found.multiplexer else '',
                }
            )
        return rows

    def _get(self, index: int) -> Optional[FoundField]:
        return self._found[index][1] if 0 <= index < len(self._found) else None

    @pyqtSlot(int)
    def use(self, index: int):
        """Show a found field in the bit field form."""
        found = self._get(index)
        if found is not None:
            name = _signal_name(KIND_NAMES.get(found.kind, 'Signal'), found.field)
            self.reverse.use_field(found.field, '', name, found.multiplexer, found.mux_value)

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        found = self._get(index)
        if found is None:
            return 'Nothing to plot'
        self.reverse.connection.track_field(found.field, '', found.multiplexer, found.mux_value)
        return ''

    def clear(self) -> None:
        self._found = []
        self.reverse.analyses = []
        self._status = ''
        self.changed.emit()


class ReferenceController(_Tool):
    """Finding the field matching a reference: a database signal, a CSV column or OBD-II values polled live."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    obd_changed = pyqtSignal()

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._csv: List[Reference] = []
        self._csv_file = ''
        self._poller: Optional[ObdPoller] = None
        self._matches: List[Match] = []
        self._reference_name = ''
        self._unit = ''
        self._obd_timer = QTimer(self)
        self._obd_timer.setInterval(500)
        self._obd_timer.timeout.connect(self.obd_changed)
        reverse.connection.database_changed.connect(self.changed)

    # Sources

    @pyqtProperty('QVariantList', notify=changed)
    def sources(self) -> List[Dict[str, Any]]:
        """References to choose from: key ('signal:Message.Signal', 'csv:3', 'obd:12'), name and unit."""
        result = []
        for index, reference in enumerate(self._csv):
            result.append(
                {
                    'key': f'csv:{index}',
                    'name': f'{reference.name} ({self._csv_file})',
                    'unit': reference.unit,
                    'group': 'CSV',
                }
            )
        if self._poller is not None:
            for pid in PIDS:
                if pid.number in self._poller.series:
                    result.append(
                        {'key': f'obd:{pid.number}', 'name': f'OBD {pid.name}', 'unit': pid.unit, 'group': 'OBD-II'}
                    )
        for info in self.reverse.connection.database_signals:
            result.append(
                {'key': f'signal:{info["key"]}', 'name': info['key'], 'unit': info['unit'], 'group': 'Database'}
            )
        return result

    @pyqtSlot(QUrl, result=str)
    def open_csv(self, url: QUrl) -> str:
        """Read a CSV file: time in seconds in the first column (relative to the first frame, or absolute), values
        in the others. Returns an error or ''."""
        path = _local_path(url)
        try:
            self._csv = read_reference_csv(path, self.reverse.session.start_time or 0.0)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            return str(e)
        self._csv_file = path.replace('\\', '/').split('/')[-1]
        self.changed.emit()
        return ''

    # OBD-II

    @pyqtProperty('QVariantList', constant=True)
    def obd_pids(self) -> List[Dict[str, Any]]:
        return [{'number': pid.number, 'name': pid.name, 'unit': pid.unit} for pid in PIDS]

    @pyqtProperty(bool, notify=obd_changed)
    def obd_running(self) -> bool:
        return self._poller is not None and self._poller.running

    @pyqtProperty(str, notify=obd_changed)
    def obd_status(self) -> str:
        poller = self._poller
        if poller is None:
            return ''
        if poller.error:
            return f'Stopped: {poller.error}'
        answered = sum(poller.answers(pid) for pid in poller.pids)
        return f'{poller.requests:,} requests, {answered:,} answers'

    @pyqtSlot(int, result=int)
    def obd_answers(self, pid: int) -> int:
        return self._poller.answers(pid) if self._poller is not None else 0

    @pyqtSlot('QVariantList', int, bool, result=str)
    def start_obd(self, pids: List[Any], interval_ms: int, extended: bool) -> str:
        """Poll OBD-II values while recording. Returns an error or ''."""
        connection = self.reverse.connection
        if not connection.can_transmit or not connection.connected:
            return 'Connect to a bus that can transmit first'
        self.stop_obd()
        try:
            self._poller = ObdPoller(
                self.reverse.session.send,
                [int(pid) for pid in pids],
                interval_ms / 1000,
                extended,
                on_error=lambda _: self.obd_changed.emit(),
            )
        except ValueError as e:
            return str(e)
        self.reverse.session.add_message_listener(self._poller.on_message)
        self._poller.start()
        self._obd_timer.start()
        self.obd_changed.emit()
        self.changed.emit()
        return ''

    @pyqtSlot()
    def stop_obd(self):
        if self._poller is not None:
            self._poller.stop()
            self.reverse.session.remove_message_listener(self._poller.on_message)
        self._obd_timer.stop()
        self.obd_changed.emit()

    # Matching

    def _reference(self, key: str, messages) -> Optional[Reference]:
        kind, _, name = key.partition(':')
        if kind == 'csv':
            index = int(name)
            return self._csv[index] if 0 <= index < len(self._csv) else None
        if kind == 'obd' and self._poller is not None:
            return self._poller.reference(int(name))
        if kind == 'signal':
            database = self.reverse.session.decoder.database
            message, _, signal = name.partition('.')
            if database is None or not signal:
                return None
            return reference_from_signal(database, messages, message, signal)
        return None

    def clear(self) -> None:
        self._matches = []
        self._status = ''
        self.changed.emit()

    @pyqtSlot(str, float)
    def find(self, source: str, max_lag: float):
        """Rank the fields of the trace by how well they follow the reference `source` (a key of `sources`)."""
        if not self._running:
            self._start(self._find(source, max(0.0, max_lag)))

    async def _find(self, source: str, max_lag: float) -> None:
        self._status = 'Reading the trace…'
        self._matches = []
        self.changed.emit()
        messages = await self.reverse.snapshot()
        reference = await asyncio.to_thread(self._reference, source, messages)
        if reference is None or len(reference) == 0:
            self._status = 'Choose a reference with values'
            return
        self._reference_name = reference.name
        self._status = f'Comparing {reference.name} ({len(reference):,} values) with the fields…'
        self.changed.emit()
        frames = await asyncio.to_thread(collect, messages)
        frames = {key: item for key, item in frames.items() if not is_obd_identifier(key)}
        candidate_fields = candidates(frames, self.reverse.analyses)
        self._matches = await asyncio.to_thread(correlate, reference, frames, candidate_fields, max_lag)
        self._unit = reference.unit
        best = max((abs(match.correlation) for match in self._matches), default=0.0)
        if best >= WEAK_CORRELATION:
            self._status = f'{len(candidate_fields):,} fields compared with {reference.name}'
        elif self._matches:
            self._status = (
                f'No field follows {reference.name} closely (best correlation {best:.2f}): the value may not be '
                'on this bus, or it was too steady while recording'
            )
        else:
            self._status = f'No field follows {reference.name} (do the frames and the reference overlap in time?)'

    @pyqtProperty('QVariantList', notify=changed)
    def matches(self) -> List[Dict[str, Any]]:
        return [
            {
                'index': index,
                'id': match.field.id_text,
                'key': match.field.key,
                'correlation': round(match.correlation, 3),
                'scale': f'{match.field.scale:.6g}',
                'offset': f'{match.field.offset:.6g}',
                'lag': f'{match.lag * 1000:.0f} ms',
                'samples': match.samples,
                'condition': f'if {match.multiplexer.key} = {match.mux_value}' if match.multiplexer else '',
            }
            for index, match in enumerate(self._matches)
        ]

    def _match(self, index: int) -> Optional[Match]:
        return self._matches[index] if 0 <= index < len(self._matches) else None

    def _name(self) -> str:
        name = ''.join(part.capitalize() for part in self._reference_name.replace('.', ' ').split() if part.isalnum())
        return name or 'Signal'

    @pyqtSlot(int)
    def use(self, index: int):
        match = self._match(index)
        if match is not None:
            self.reverse.use_field(match.field, self._unit, self._name(), match.multiplexer, match.mux_value)

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        match = self._match(index)
        if match is None:
            return 'Nothing to plot'
        self.reverse.connection.track_field(match.field, self._unit, match.multiplexer, match.mux_value)
        return ''


class MarkerController(_Tool):
    """Event markers set while something happens, and the bits changing at them."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._markers: List[Marker] = []
        self._matches: List[MarkerMatch] = []

    def _now(self) -> Optional[float]:
        """The timestamp of the newest frame: the bus's clock, also when replaying a log file."""
        trace = self.reverse.session.trace
        frame = trace.get(trace.total - 1)
        return frame.message.timestamp if frame is not None else None

    @pyqtSlot(str, result=str)
    def add(self, label: str) -> str:
        """Mark this moment. Returns an error or ''."""
        now = self._now()
        if now is None:
            return 'No frames yet'
        self._markers.append(Marker(now, label.strip()))
        self.changed.emit()
        return ''

    @pyqtSlot(int)
    def remove(self, index: int):
        if 0 <= index < len(self._markers):
            del self._markers[index]
            self.changed.emit()

    @pyqtSlot()
    def clear(self):
        self._markers.clear()
        self._matches.clear()
        self._status = ''
        self.changed.emit()

    @pyqtProperty('QVariantList', notify=changed)
    def markers(self) -> List[Dict[str, Any]]:
        return [
            {'index': index, 'time': self.reverse.time_text(marker.time), 'label': marker.label}
            for index, marker in enumerate(self._markers)
        ]

    @pyqtProperty('QStringList', notify=changed)
    def labels(self) -> List[str]:
        return sorted({marker.label for marker in self._markers if marker.label})

    @pyqtSlot(str, float, float)
    def find(self, label: str, before: float, after: float):
        """Rank the bits changing around the markers (with `label`, or all for '')."""
        if not self._running:
            self._start(self._find(label, max(0.0, before), max(0.01, after)))

    async def _find(self, label: str, before: float, after: float) -> None:
        markers = [marker for marker in self._markers if not label or marker.label == label]
        if not markers:
            self._status = 'Set markers first'
            return
        self._status = 'Reading the trace…'
        self._matches = []
        self.changed.emit()
        messages = await self.reverse.snapshot()
        frames = await asyncio.to_thread(collect, messages)
        frames = {key: item for key, item in frames.items() if not is_obd_identifier(key)}
        self._matches = await asyncio.to_thread(find_marker_changes, frames, markers, before, after)
        if self._matches:
            self._status = f'{len(self._matches)} fields changing at {len(markers)} markers'
        else:
            self._status = f'No bits change at most of the {len(markers)} markers more than elsewhere'

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        return [
            {
                'index': index,
                'id': match.field.id_text,
                'key': match.field.key,
                'hits': f'{match.hits}/{match.markers}',
                'background': f'{match.background:.2g}/s',
                'score': round(match.score, 2),
                'transitions': ', '.join(match.transitions[:4]) + (' …' if len(match.transitions) > 4 else ''),
            }
            for index, match in enumerate(self._matches)
        ]

    def _match(self, index: int) -> Optional[MarkerMatch]:
        return self._matches[index] if 0 <= index < len(self._matches) else None

    @pyqtSlot(int)
    def use(self, index: int):
        match = self._match(index)
        if match is not None:
            self.reverse.use_field(match.field, '', _signal_name('Event', match.field))

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        match = self._match(index)
        if match is None:
            return 'Nothing to plot'
        self.reverse.connection.track_field(match.field)
        return ''
