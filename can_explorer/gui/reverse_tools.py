"""The tools of the Reverse engineering page working on the frames of the trace: the automatic analysis, finding a
field matching a reference signal, and finding the bits changing at event markers. They run in a worker thread."""

import asyncio
import time
from typing import Any, Dict, List, Optional

import can
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.reverse.analysis import FoundField, analyse_all, collect
from can_explorer.core.reverse.console import HELP, build_namespace
from can_explorer.core.reverse.console import run as run_console
from can_explorer.core.reverse.raster import Raster, build_raster
from can_explorer.core.reverse.markers import Marker, MarkerMatch, find_marker_changes
from can_explorer.core.reverse.obd import PIDS, ObdPoller, is_obd_identifier
from can_explorer.core.reverse.reactions import IdReaction, capture_reaction
from can_explorer.core.reverse.related import SignalGroup, signal_groups
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
    'freshness': 'SecOC freshness',
    'mac': 'SecOC MAC',
    'random': 'Random (MAC?)',
    'keystream_reuse': 'Keystream reuse',
    'linear': 'Linear relation',
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
                analyse_all,
                frames,
                lambda done, total: setattr(self, '_progress', (done, total)),
                self.reverse.session.decoder.secured_pdus,  # SecOC freshness and MAC at the ARXML's bits
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

    @pyqtProperty('QVariantList', notify=changed)
    def found_signals(self) -> List[Dict[str, Any]]:
        """Every found field (except constants) as a signal definition, for promoting the results to a database
        (DatabaseEditorController.add_found_signals)."""
        definitions = []
        taken: Dict[tuple, set] = {}  # Bits already used per identifier, so promoted signals never overlap (a DBC
        found_fields = [(a, f) for a, f in self._found if f is not None and f.kind != 'constant']
        found_fields.sort(key=lambda item: -item[1].confidence)  # The strongest interpretation wins an overlap
        for analysis, found in found_fields:
            field = found.field
            key = (field.frame_id, field.extended)
            bits = set(field.bits)
            used = taken.setdefault(key, set())
            if bits & used:
                continue
            used |= bits
            comment = [KIND_NAMES.get(found.kind, found.kind)]
            if found.details:
                comment.append(found.details)
            if found.multiplexer is not None:
                comment.append(f'only in frames with {found.multiplexer.key} = {found.mux_value}')
            definitions.append(
                {
                    'frame_id': field.frame_id,
                    'extended': field.extended,
                    'frame_length': max(analysis.length, field.min_length),
                    'name': _signal_name(KIND_NAMES.get(found.kind, 'Signal'), field),
                    'start': field.start,
                    'length': field.length,
                    'byte_order': field.byte_order,
                    'is_signed': field.signed,
                    'scale': field.scale,
                    'offset': field.offset,
                    'unit': '',
                    'comment': '; '.join(comment),
                }
            )
        return definitions

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


class RelatedController(_Tool):
    """Finding fields that carry the same signal across the trace, grouped, without an external reference."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._groups: List[SignalGroup] = []
        self._members: List[tuple] = []  # (field, unit, name, multiplexer, mux_value) per results row

    @pyqtSlot(float, float)
    def find(self, min_correlation: float, max_lag: float):
        if not self._running:
            self._start(self._find(min_correlation, max(0.0, max_lag)))

    async def _find(self, min_correlation: float, max_lag: float) -> None:
        self._status = 'Reading the trace…'
        self._groups, self._members = [], []
        self.changed.emit()
        messages = await self.reverse.snapshot()
        frames = await asyncio.to_thread(collect, messages)
        frames = {key: item for key, item in frames.items() if not is_obd_identifier(key)}
        analyses = self.reverse.analyses or await asyncio.to_thread(
            analyse_all, frames, None, self.reverse.session.decoder.secured_pdus
        )
        self._status = 'Correlating the value fields with each other…'
        self.changed.emit()
        self._groups = await asyncio.to_thread(signal_groups, frames, analyses, min_correlation, max_lag)
        for group in self._groups:
            for member in group.members:
                name = _signal_name('Signal', member.field)
                self._members.append((member.field, '', name, member.multiplexer, member.mux_value))
        if self._groups:
            fields = sum(len(group.members) for group in self._groups)
            self._status = f'{len(self._groups)} group(s) of fields carrying the same signal ({fields} fields)'
        else:
            self._status = 'No two fields follow one another closely (no signal is repeated across messages)'

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        index = 0
        for group_number, group in enumerate(self._groups):
            for member in group.members:
                rows.append(
                    {
                        'index': index,
                        'group': group_number + 1,
                        'representative': member.representative,
                        'id': member.field.id_text,
                        'key': member.field.key,
                        'correlation': 1.0 if member.representative else round(abs(member.correlation), 3),
                        'scale': f'{member.field.scale:.6g}',
                        'offset': f'{member.field.offset:.6g}',
                        'lag': '' if member.representative else f'{member.lag * 1000:.0f} ms',
                        'condition': f'if {member.multiplexer.key} = {member.mux_value}' if member.multiplexer else '',
                    }
                )
                index += 1
        return rows

    def _member(self, index: int) -> Optional[tuple]:
        return self._members[index] if 0 <= index < len(self._members) else None

    @pyqtSlot(int)
    def use(self, index: int):
        member = self._member(index)
        if member is not None:
            field, unit, name, multiplexer, mux_value = member
            self.reverse.use_field(field, unit, name, multiplexer, mux_value)

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        member = self._member(index)
        if member is None:
            return 'Nothing to plot'
        field, unit, _name, multiplexer, mux_value = member
        self.reverse.connection.track_field(field, unit, multiplexer, mux_value)
        return ''

    def clear(self) -> None:
        self._groups, self._members = [], []
        self._status = ''
        self.changed.emit()


class ReactionController(_Tool):
    """Stimulus-response: record a baseline, do an action, and diff what changed on the bus (a one-click version
    of markers for the "I did X, what changed?" loop). Passive -- it only reads captured frames."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._phase = 'idle'  # idle -> baseline -> action -> done
        # Phase boundaries are frame indices (counts), not timestamps: a coarse clock (e.g. Windows' ~15 ms) can
        # stamp a whole rapid burst with one value, which would collapse a timestamp-based window to nothing.
        self._baseline_index: Optional[int] = None
        self._action_index: Optional[int] = None
        self._rows: List[tuple] = []  # (IdReaction, FieldReaction or None)

    def _count(self) -> int:
        return self.reverse.session.trace.total

    @pyqtProperty(str, notify=changed)
    def phase(self) -> str:
        return self._phase

    @pyqtSlot(result=str)
    def start_baseline(self) -> str:
        """Begin recording the baseline window (keep the bus steady). Returns an error or ''."""
        if self._count() == 0:
            return 'No frames yet'
        self._baseline_index = self._count()
        self._action_index = None
        self._rows = []
        self._phase = 'baseline'
        self._status = 'Recording the baseline — keep the bus steady, then start the action'
        self.changed.emit()
        return ''

    @pyqtSlot(result=str)
    def mark_action(self) -> str:
        """End the baseline and start the action window (now perform the action). Returns an error or ''."""
        if self._phase != 'baseline':
            return 'Record a baseline first'
        if self._count() == 0:
            return 'No frames yet'
        self._action_index = self._count()
        self._phase = 'action'
        self._status = 'Now perform the action, then capture the reaction'
        self.changed.emit()
        return ''

    @pyqtSlot()
    def capture(self):
        """End the action window and diff it against the baseline."""
        if self._phase == 'action' and not self._running:
            self._start(self._capture(self._count()))

    async def _capture(self, end_index: Optional[int]) -> None:
        if end_index is None or self._baseline_index is None or self._action_index is None:
            self._status = 'No frames to analyse'
            return
        trace = self.reverse.session.trace
        baseline_index = max(self._baseline_index, trace.first)  # Clamp in case older frames were paged off
        action_index = max(self._action_index, baseline_index)
        self._status = 'Reading the trace…'
        self._rows = []
        self.changed.emit()
        baseline: List[can.Message] = []
        action: List[can.Message] = []
        index = baseline_index
        for frame in trace.iter_from(baseline_index):
            if index >= end_index:
                break
            (baseline if index < action_index else action).append(frame.message)
            index += 1
            if index % 50_000 == 0:
                await asyncio.sleep(0)
        exclude = {key for key in {(m.arbitration_id, m.is_extended_id) for m in action} if is_obd_identifier(key)}
        report = await asyncio.to_thread(capture_reaction, baseline, action, exclude)
        rows: List[tuple] = []
        for reaction in report.reactions:
            if reaction.appeared and not reaction.fields:
                rows.append((reaction, None))
            for item in reaction.fields:
                rows.append((reaction, item))
        self._rows = rows
        self._phase = 'done'
        fields = sum(1 for _, item in rows if item is not None)
        self._status = (
            f'{len(report.reactions)} identifiers reacted ({fields} fields) — baseline {report.baseline_frames}, '
            f'action {report.action_frames} frames'
            if report.reactions
            else 'Nothing changed during the action'
        )

    @pyqtSlot()
    def reset(self):
        self._phase = 'idle'
        self._baseline_index = self._action_index = None
        self._rows = []
        self._status = ''
        self.changed.emit()

    clear = reset

    @staticmethod
    def _change_text(reaction: IdReaction, item) -> str:
        if item is None:
            return '(new identifier)'
        before = '—' if item.before is None else f'0x{item.before:X}'
        return f'{before} → 0x{item.after:X}'

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        return [
            {
                'index': index,
                'id': reaction.id_text,
                'key': item.field.key if item is not None else '',
                'kind': 'new' if reaction.appeared else 'changed',
                'change': self._change_text(reaction, item),
                'flips': item.action_flips if item is not None else 0,
            }
            for index, (reaction, item) in enumerate(self._rows)
        ]

    def _field(self, index: int):
        if 0 <= index < len(self._rows):
            return self._rows[index][1]
        return None

    @pyqtSlot(int)
    def use(self, index: int):
        item = self._field(index)
        if item is not None:
            self.reverse.use_field(item.field, '', _signal_name('Reaction', item.field))

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        item = self._field(index)
        if item is None:
            return 'Nothing to plot'
        self.reverse.connection.track_field(item.field)
        return ''


class ConsoleController(_Tool):
    """An interactive Python console over the captured frames, with the reverse-engineering primitives (collect,
    analyse, BitField, candidates, signal_groups). Variables persist between runs; the frames, found fields and
    helpers are refreshed from the trace on each run. It runs the snippet in a worker thread."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._namespace: Dict[str, Any] = {}
        self._entries: List[Dict[str, str]] = []

    @pyqtProperty(str, constant=True)
    def help_text(self) -> str:
        return HELP

    @pyqtProperty('QVariantList', notify=changed)
    def entries(self) -> List[Dict[str, str]]:
        return self._entries

    @pyqtSlot(str)
    def run(self, source: str):
        """Run a snippet over the current trace. Its output and value (or error) are appended to the transcript."""
        if not self._running and source.strip():
            self._start(self._run(source))

    async def _run(self, source: str) -> None:
        self._status = 'Running…'
        self.changed.emit()
        messages = await self.reverse.snapshot()
        analyses = list(self.reverse.analyses)
        result = await asyncio.to_thread(self._execute, messages, analyses, source)
        self._entries.append(
            {'source': source.strip(), 'output': result.output, 'value': result.value, 'error': result.error}
        )
        self._status = '' if result.ok else 'Error'

    def _execute(self, messages, analyses, source: str):
        self._namespace.update(build_namespace(messages, analyses))
        return run_console(source, self._namespace)

    @pyqtSlot()
    def clear(self):
        """Clear the transcript and forget all variables."""
        self._entries = []
        self._namespace = {}
        self._status = ''
        self.changed.emit()


class CryptoController(_Tool):
    """Cryptanalysis: randomness tests, freshness analysis, linear relations and keystream reuse for the frames of
    each identifier (or the selected one). Runs the same assessments as the MCP ``assess_frame_security`` tool."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._rows: List[Dict[str, Any]] = []

    @pyqtSlot(bool)
    def run(self, selected_only: bool):
        if not self._running:
            self._start(self._run(selected_only))

    async def _run(self, selected_only: bool) -> None:
        from can_explorer.core.reverse.analysis import analyse as analyse_id
        from can_explorer.core.reverse.crypto import detect_keystream_reuse, test_randomness
        from can_explorer.core.reverse.e2e_reverse import detect_e2e
        from can_explorer.core.reverse.linear import find_linear_relations

        self._status = 'Reading the trace…'
        self._rows = []
        self.changed.emit()
        messages = await self.reverse.snapshot()
        selected = self.reverse.selected
        if selected_only and selected:
            key = (int(selected, 16), len(selected) > 3)
            messages = [msg for msg in messages if (msg.arbitration_id, msg.is_extended_id) == key]

        frames = await asyncio.to_thread(collect, messages)
        frames = {k: item for k, item in frames.items() if not is_obd_identifier(k)}
        rows: List[Dict[str, Any]] = []

        total = len(frames)
        done = 0

        for frame_key, id_frames in sorted(frames.items()):
            done += 1
            if done % 10 == 0:
                self._status = f'Analysing… {done} of {total} identifiers'
                self.changed.emit()
                await asyncio.sleep(0)

            frame_id, extended = frame_key
            id_text = _id_text(frame_id, extended)
            payloads = id_frames.payloads

            if len(payloads) < 20:
                rows.append(
                    {
                        'id': id_text,
                        'test': 'Overall',
                        'verdict': 'inconclusive',
                        'confidence': 0,
                        'details': f'Too few frames ({len(payloads)}); need at least 20',
                    }
                )
                continue

            length = len(payloads[0]) if payloads else 0
            if length < 2:
                continue

            # Randomness tests on trailing bytes
            trailing_start = max(0, length - max(2, length // 3))
            randomness = test_randomness(payloads, (trailing_start, length - 1))
            rows.append(
                {
                    'id': id_text,
                    'test': 'Randomness',
                    'verdict': randomness.verdict,
                    'confidence': round(randomness.confidence * 100),
                    'details': (
                        f'bytes {trailing_start}-{length - 1}: monobit p={randomness.monobit_p:.3f}, '
                        f'runs p={randomness.runs_p:.3f}, chi2 p={randomness.chi_square_p:.3f}, '
                        f'serial r={randomness.serial_correlation:.3f}'
                    ),
                }
            )

            # Linear relations
            try:
                relations = find_linear_relations(payloads)
                if relations:
                    for rel in relations[:3]:
                        rows.append(
                            {
                                'id': id_text,
                                'test': 'Linear relation',
                                'verdict': 'finding',
                                'confidence': round(rel.confidence * 100),
                                'details': rel.description,
                            }
                        )
            except Exception:
                pass

            # Keystream reuse: run the automatic analysis to find a counter field, then check for reuse
            try:
                analysis = analyse_id(id_frames, multiplexers=False)
                counter_field = None
                for found in analysis.fields:
                    if found.kind == 'counter':
                        counter_field = found.field
                        break
                if counter_field is not None and length >= 3:
                    counter_bytes = {b // 8 for b in counter_field.bits}
                    non_counter = [b for b in range(length) if b not in counter_bytes]
                    if non_counter:
                        enc_first = min(non_counter)
                        enc_last = max(non_counter)
                        reuse = detect_keystream_reuse(payloads, counter_field, (enc_first, enc_last))
                        if reuse.structured_xor_pairs > 0:
                            rows.append(
                                {
                                    'id': id_text,
                                    'test': 'Keystream reuse',
                                    'verdict': 'finding',
                                    'confidence': 80,
                                    'details': '; '.join(reuse.findings),
                                }
                            )
                        elif reuse.reused_counter_values:
                            rows.append(
                                {
                                    'id': id_text,
                                    'test': 'Keystream reuse',
                                    'verdict': 'inconclusive',
                                    'confidence': 40,
                                    'details': '; '.join(reuse.findings),
                                }
                            )
            except Exception:
                pass

            # E2E profile detection
            try:
                e2e_result = await asyncio.to_thread(detect_e2e, list(payloads))
                if e2e_result.profiles:
                    best = e2e_result.profiles[0]
                    data_id_desc = ''
                    if best.data_ids is not None:
                        if isinstance(best.data_ids, list) and len(best.data_ids) > 1:
                            data_id_desc = f', {len(best.data_ids)} Data IDs recovered'
                        elif isinstance(best.data_ids, list) and best.data_ids:
                            data_id_desc = f', Data ID 0x{best.data_ids[0]:04X}'
                        elif isinstance(best.data_ids, int):
                            data_id_desc = f', Data ID 0x{best.data_ids:04X}'
                    rows.append(
                        {
                            'id': id_text,
                            'test': 'E2E',
                            'verdict': 'finding',
                            'confidence': round(best.confidence * 100),
                            'details': (
                                f'Profile {best.profile}'
                                f'{data_id_desc}'
                                f', counter at byte {best.counter_position}'
                                f', CRC at byte {best.crc_position}'
                                f' ({best.match_rate * 100:.0f}% match)'
                            ),
                        }
                    )
                if e2e_result.bypasses:
                    for bp in e2e_result.bypasses:
                        rows.append(
                            {
                                'id': id_text,
                                'test': 'E2E bypass',
                                'verdict': 'warning' if bp.severity == 'warning' else 'finding',
                                'confidence': 90,
                                'details': bp.description,
                            }
                        )
            except Exception:
                pass

        self._rows = rows
        verdicts = [r['verdict'] for r in rows]
        findings = verdicts.count('finding')
        random_count = verdicts.count('random')
        structured = verdicts.count('structured')
        self._status = (
            f'{len(rows)} results for {total} identifiers: '
            f'{findings} findings, {random_count} random, {structured} structured'
        )

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        return self._rows

    @pyqtSlot(int)
    def use(self, index: int):
        """Navigate to the identifier of the selected crypto result in the Bits tab."""
        if 0 <= index < len(self._rows):
            id_text = self._rows[index].get('id', '')
            if id_text:
                self.reverse.select(id_text)

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        """Plot the identifier's inter-frame interval for the selected crypto result."""
        if 0 <= index < len(self._rows):
            id_text = self._rows[index].get('id', '')
            if id_text:
                frame_id = int(id_text, 16)
                extended = len(id_text) > 3
                self.reverse.connection.track_interval(frame_id, extended)
                return ''
        return 'Nothing to plot'

    def clear(self) -> None:
        self._rows = []
        self._status = ''
        self.changed.emit()


class GatewaySecurityController(_Tool):
    """Gateway security analysis: authentication patterns, bypass detection and cross-ECU seed/key correlation
    from the captured UDS traffic."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._rows: List[Dict[str, Any]] = []
        self._messages: List[Any] = []  # The capture snapshot of the last run, for the disclosure/DBC exports

    @pyqtSlot()
    def run(self):
        if not self._running:
            self._start(self._run())

    async def _run(self) -> None:
        from can_explorer.core.reverse.gateway_security import (
            analyse_auth_timing,
            analyse_gateway_auth,
            analyse_gateway_filtering,
            analyse_reset_behaviour,
            build_access_matrix,
            correlate_ecu_seeds,
            detect_bus_segments,
            map_gateway,
        )
        from can_explorer.core.seedkey_crack import assess_seed_quality, crack

        self._status = 'Reading the trace…'
        self._rows = []
        self.changed.emit()
        messages = await self.reverse.snapshot()
        self._messages = messages

        self._status = 'Analysing gateway authentication patterns…'
        self.changed.emit()
        auth_patterns = await asyncio.to_thread(analyse_gateway_auth, messages)

        self._status = 'Analysing gateway filtering…'
        self.changed.emit()
        bypass_findings = await asyncio.to_thread(analyse_gateway_filtering, messages)

        # Gateway mapping (item 1)
        self._status = 'Mapping gateway topology…'
        self.changed.emit()
        gw_map = await asyncio.to_thread(map_gateway, messages)

        # Access matrix (item 2)
        self._status = 'Building access matrix…'
        self.changed.emit()
        access = await asyncio.to_thread(build_access_matrix, messages)

        # Auth timing (item 8)
        self._status = 'Analysing authentication timing…'
        self.changed.emit()
        auth_timing = await asyncio.to_thread(analyse_auth_timing, messages)

        # Bus segmentation (item 11)
        self._status = 'Detecting bus segments…'
        self.changed.emit()
        bus_segments = await asyncio.to_thread(detect_bus_segments, messages)

        # Reset behaviour (item 12)
        self._status = 'Analysing reset behaviour…'
        self.changed.emit()
        reset_findings = await asyncio.to_thread(analyse_reset_behaviour, messages)

        # Extract observed seed/key pairs from UDS 0x27 exchanges for cross-ECU correlation
        self._status = 'Checking cross-ECU seed/key correlations…'
        self.changed.emit()
        seed_key_pairs = self._extract_seed_key_pairs(messages)
        correlations = []
        if len(seed_key_pairs) >= 2:
            try:
                correlations = await asyncio.to_thread(correlate_ecu_seeds, seed_key_pairs)
            except Exception:
                pass

        # Item 10: run crack() on each ECU's seed/key pairs + seed quality
        self._status = 'Cracking seed/key algorithms…'
        self.changed.emit()
        crack_results: Dict[int, Any] = {}
        seed_quality: Dict[int, Any] = {}
        for ecu_id, pairs in seed_key_pairs.items():
            if len(pairs) >= 3:
                try:
                    crack_results[ecu_id] = await asyncio.to_thread(crack, pairs)
                except Exception:
                    pass
            seeds = [s for s, _k in pairs]
            if seeds:
                try:
                    seed_quality[ecu_id] = await asyncio.to_thread(assess_seed_quality, seeds)
                except Exception:
                    pass

        rows: List[Dict[str, Any]] = []

        # Gateway map rows (item 1)
        for ecu in gw_map.ecus:
            sessions = ', '.join(str(s) for s in ecu.supported_sessions) if ecu.supported_sessions else 'default'
            rows.append(
                {
                    'category': 'Topology',
                    'id': f'0x{ecu.request_id:03X}',
                    'finding': f'{ecu.auth_type.replace("_", " ")} ({len(ecu.services_seen)} services)',
                    'severity': 'info',
                    'details': f'Sessions: {sessions}, response: {ecu.mean_response_ms:.1f} ms',
                }
            )

        for pattern in auth_patterns:
            rows.append(
                {
                    'category': 'Authentication',
                    'id': f'0x{pattern.request_id:03X}',
                    'finding': pattern.auth_type.replace('_', ' '),
                    'severity': 'info',
                    'details': pattern.details,
                }
            )

        for finding in bypass_findings:
            rows.append(
                {
                    'category': 'Filtering',
                    'id': '',
                    'finding': finding.finding_type.replace('_', ' '),
                    'severity': finding.severity,
                    'details': finding.description,
                }
            )

        # Access matrix summary rows (item 2)
        denied_count = sum(1 for r in access.rows if r.status == 'denied')
        conditional_count = sum(1 for r in access.rows if r.status == 'conditional')
        if denied_count or conditional_count:
            rows.append(
                {
                    'category': 'Access matrix',
                    'id': '',
                    'finding': f'{denied_count} denied, {conditional_count} conditional',
                    'severity': 'warning' if denied_count else 'info',
                    'details': (
                        f'{len(access.rows)} service/session combinations across {len(access.ecus)} ECU(s), '
                        f'{len(access.sessions)} session(s)'
                    ),
                }
            )

        # Auth timing rows (item 8)
        for timing in auth_timing:
            severity = 'warning' if timing.findings else 'info'
            detail_parts = []
            if timing.request_to_seed_ms is not None:
                detail_parts.append(f'seed: {timing.request_to_seed_ms:.1f} ms')
            if timing.key_to_response_ms is not None:
                detail_parts.append(f'key verify: {timing.key_to_response_ms:.1f} ms')
            detail_parts.append(f'total: {timing.total_ms:.1f} ms')
            if timing.findings:
                detail_parts.append(timing.findings[0])
            rows.append(
                {
                    'category': 'Auth timing',
                    'id': f'0x{timing.ecu_id:03X}',
                    'finding': timing.auth_type.replace('_', ' '),
                    'severity': severity,
                    'details': '; '.join(detail_parts),
                }
            )

        for corr in correlations:
            severity = (
                'finding' if corr.finding == 'shared_key' else 'warning' if corr.finding == 'same_algorithm' else 'info'
            )
            pairs_text = ', '.join(f'0x{a:03X}-0x{b:03X}' for a, b in corr.ecu_pairs[:5])
            rows.append(
                {
                    'category': 'Cross-ECU',
                    'id': pairs_text,
                    'finding': corr.finding.replace('_', ' '),
                    'severity': severity,
                    'details': corr.details,
                }
            )

        # Item 10: crack results
        for ecu_id, report in crack_results.items():
            if report.matches:
                for m in report.matches:
                    rows.append(
                        {
                            'category': 'Seed/key crack',
                            'id': f'0x{ecu_id:03X}',
                            'finding': m.algorithm,
                            'severity': 'finding',
                            'details': m.description,
                        }
                    )
            else:
                rows.append(
                    {
                        'category': 'Seed/key crack',
                        'id': f'0x{ecu_id:03X}',
                        'finding': 'no match',
                        'severity': 'info',
                        'details': f'{len(seed_key_pairs[ecu_id])} pairs tested, no algorithm recovered',
                    }
                )

        # NRC fingerprinting (roadmap 1.2 / 4.1)
        from can_explorer.core.reverse.gateway_security import fingerprint_ecu, plan_probing

        self._status = 'Fingerprinting ECUs…'
        self.changed.emit()
        for ecu in gw_map.ecus:
            fp = await asyncio.to_thread(fingerprint_ecu, messages, ecu.request_id)
            nrc_text = ', '.join(f'0x{n:02X}' for n in fp.nrc_set[:8])
            timing_text = f'mean {fp.timing_profile["mean_ms"]:.1f} ms, std {fp.timing_profile["std_ms"]:.1f} ms'
            rows.append(
                {
                    'category': 'NRC fingerprint',
                    'id': f'0x{fp.request_id:03X}',
                    'finding': ', '.join(fp.vendor_hints[:2]) if fp.vendor_hints else 'no vendor match',
                    'severity': 'info',
                    'details': f'NRCs: [{nrc_text}]; timing: {timing_text}',
                }
            )

        # Probing planner (roadmap 1.2 / 4.1)
        self._status = 'Planning next probing steps…'
        self.changed.emit()
        probe_steps = await asyncio.to_thread(plan_probing, access)
        if probe_steps:
            high_steps = [s for s in probe_steps if s.priority == 'high']
            medium_steps = [s for s in probe_steps if s.priority == 'medium']
            rows.append(
                {
                    'category': 'Probe plan',
                    'id': '',
                    'finding': f'{len(probe_steps)} steps ({len(high_steps)} high, {len(medium_steps)} medium)',
                    'severity': 'info' if not high_steps else 'warning',
                    'details': probe_steps[0].rationale if probe_steps else '',
                }
            )

        # Item 11: bus segments
        for seg in bus_segments:
            rows.append(
                {
                    'category': 'Bus segment',
                    'id': f'0x{seg.id_range[0]:03X}-0x{seg.id_range[1]:03X}',
                    'finding': f'{len(seg.ids)} IDs, {seg.periodic_ratio:.0%} periodic',
                    'severity': 'info',
                    'details': f'{seg.mean_rate_hz:.1f} Hz avg, {len(seg.event_driven_ids)} event-driven',
                }
            )

        # Item 12: reset behaviour
        for rf in reset_findings:
            rows.append(
                {
                    'category': 'Reset bypass',
                    'id': f'0x{rf.ecu_id:03X}',
                    'finding': rf.service_name,
                    'severity': rf.severity,
                    'details': rf.finding,
                }
            )

        # Item 9: seed quality
        for ecu_id, quality in seed_quality.items():
            if quality.predictable:
                rows.append(
                    {
                        'category': 'Seed quality',
                        'id': f'0x{ecu_id:03X}',
                        'finding': 'weak seeds',
                        'severity': 'finding',
                        'details': quality.findings[0] if quality.findings else 'Predictable seeds',
                    }
                )

        self._rows = rows
        auth_count = len(auth_patterns)
        finding_count = sum(1 for f in bypass_findings if f.severity in ('warning', 'finding'))
        corr_count = len(correlations)
        crack_count = sum(1 for r in crack_results.values() if r.matches)
        self._status = (
            f'{auth_count} ECU(s) analysed, {finding_count} filtering finding(s), '
            f'{corr_count} cross-ECU correlation(s), {crack_count} algorithm(s) cracked, '
            f'{len(bypass_findings)} total observations'
        )

    @staticmethod
    def _extract_seed_key_pairs(messages) -> Dict[int, list]:
        """Extract observed (seed, key) pairs from UDS 0x27 SecurityAccess exchanges in the captured traffic."""
        by_time = sorted(messages, key=lambda m: m.timestamp)
        seed_key_pairs: Dict[int, list] = {}
        pending_seeds: Dict[int, bytes] = {}  # request_id -> last seed received

        for i, msg in enumerate(by_time):
            data = bytes(msg.data)
            if len(data) < 2:
                continue
            resp_id = msg.arbitration_id
            # Look for positive seed response: 0x67 sub seed_bytes
            if data[0] == 0x67 and data[1] % 2 == 1 and len(data) > 2:
                req_id = resp_id - 8
                pending_seeds[req_id] = data[2:]
            # Look for positive key response: 0x67 sub (even sub = sendKey accepted)
            elif data[0] == 0x67 and data[1] % 2 == 0:
                req_id = resp_id - 8
                # Find the sendKey request that preceded this response
                for j in range(i - 1, max(i - 50, -1), -1):
                    prev = by_time[j]
                    if prev.arbitration_id == req_id and msg.timestamp - prev.timestamp < 5.0:
                        prev_data = bytes(prev.data)
                        if len(prev_data) >= 2 and prev_data[0] == 0x27 and prev_data[1] == data[1]:
                            key_bytes = prev_data[2:]
                            seed = pending_seeds.get(req_id)
                            if seed is not None and len(seed) == len(key_bytes) and len(seed) > 0:
                                seed_key_pairs.setdefault(req_id, []).append((seed, key_bytes))
                            break

        return seed_key_pairs

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        return self._rows

    @pyqtSlot()
    def generateReport(self):
        """Generate a consolidated gateway report and add its summary to the results."""
        if not self._running:
            self._start(self._generate_report())

    async def _generate_report(self) -> None:
        from can_explorer.core.reverse.gateway_security import gateway_report

        self._status = 'Generating consolidated gateway report…'
        self.changed.emit()
        messages = await self.reverse.snapshot()
        report = await asyncio.to_thread(gateway_report, messages)
        self._rows.append(
            {
                'category': 'Report',
                'id': '',
                'finding': f'severity: {report.overall_severity}',
                'severity': 'finding' if report.overall_severity == 'critical' else 'warning',
                'details': '; '.join(report.summary[:3]) if report.summary else 'No findings',
            }
        )
        self._status = f'Report generated — overall severity: {report.overall_severity}'
        self.changed.emit()

    @pyqtSlot()
    def crackAll(self):
        """Re-run seed/key cracking on all extracted ECU pairs."""
        if not self._running:
            self._start(self._crack_all())

    async def _crack_all(self) -> None:
        from can_explorer.core.seedkey_crack import crack

        self._status = 'Cracking seed/key algorithms…'
        self.changed.emit()
        messages = await self.reverse.snapshot()
        seed_key_pairs = self._extract_seed_key_pairs(messages)
        new_rows: List[Dict[str, Any]] = []
        for ecu_id, pairs in seed_key_pairs.items():
            if len(pairs) >= 3:
                try:
                    result = await asyncio.to_thread(crack, pairs)
                    for m in result.matches:
                        new_rows.append(
                            {
                                'category': 'Seed/key crack',
                                'id': f'0x{ecu_id:03X}',
                                'finding': m.algorithm,
                                'severity': 'finding',
                                'details': m.description,
                            }
                        )
                    if not result.matches:
                        new_rows.append(
                            {
                                'category': 'Seed/key crack',
                                'id': f'0x{ecu_id:03X}',
                                'finding': 'no match',
                                'severity': 'info',
                                'details': f'{len(pairs)} pairs tested, no algorithm recovered',
                            }
                        )
                except Exception:
                    pass
        self._rows.extend(new_rows)
        cracked = sum(1 for r in new_rows if r['severity'] == 'finding')
        self._status = f'Cracking complete — {cracked} algorithm(s) recovered from {len(seed_key_pairs)} ECU(s)'
        self.changed.emit()

    @pyqtSlot(str, result=str)
    def exportReport(self, fmt: str) -> str:
        """Export the current results as JSON or CSV and return the content."""
        import csv as csv_mod
        import io
        import json as json_mod

        if not self._rows:
            return ''
        if fmt == 'json':
            return json_mod.dumps(self._rows, indent=2)
        if fmt == 'csv':
            buf = io.StringIO()
            writer = csv_mod.DictWriter(buf, fieldnames=['category', 'id', 'finding', 'severity', 'details'])
            writer.writeheader()
            writer.writerows(self._rows)
            return buf.getvalue()
        return ''

    @pyqtSlot(str, result=str)
    def disclosureReport(self, fmt: str) -> str:
        """A responsible-disclosure document (Markdown or JSON) from the last run's capture. Run the analysis
        first; returns '' when there is no capture yet."""
        from can_explorer.core.reverse.gateway_security import disclosure_report

        if not self._messages:
            return ''
        pairs = self._extract_seed_key_pairs(self._messages)
        return disclosure_report(
            self._messages, seed_key_pairs=pairs or None, fmt=fmt if fmt in ('markdown', 'json') else 'markdown'
        )

    @pyqtSlot(result=str)
    def exportDiscoveredDbc(self) -> str:
        """A minimal DBC naming each discovered ECU's request/response message, from the last run's capture."""
        from can_explorer.core.reverse.findings_artifacts import gateway_map_to_dbc
        from can_explorer.core.reverse.gateway_security import map_gateway

        if not self._messages:
            return ''
        return gateway_map_to_dbc(map_gateway(self._messages))

    @pyqtSlot(str, result='QVariantMap')
    def previewProbePlan(self, plan: str) -> Dict[str, Any]:
        """Preview a probing plan as a dry run (transmits nothing): how many steps, and how many state-changing
        services would be skipped. plan is 'enumerate', 'brute' or 'scan'."""
        from can_explorer.core.reverse import active_prober
        from can_explorer.core.reverse.gateway_security import brute_plan, enumerate_plan, scan_plan_non_standard

        if not self._messages:
            return {'error': 'Run the analysis first'}
        builders = {
            'enumerate': lambda: active_prober.steps_from_enumeration(enumerate_plan(self._messages)),
            'brute': lambda: active_prober.steps_from_brute(brute_plan(self._messages)),
            'scan': lambda: active_prober.steps_from_scan(scan_plan_non_standard(self._messages)),
        }
        if plan not in builders:
            return {'error': "plan must be 'enumerate', 'brute' or 'scan'"}
        steps = builders[plan]()
        skip = sum(1 for s in steps if s.service in active_prober.DANGEROUS_SERVICES)
        return {'plan': plan, 'steps_planned': len(steps), 'would_skip_dangerous': skip}

    @pyqtSlot(int)
    def use(self, index: int):
        """Navigate to the ECU identifier of the selected gateway result in the Bits tab."""
        if 0 <= index < len(self._rows):
            id_text = self._rows[index].get('id', '')
            # Strip '0x' prefix for identifiers like '0x7E0'
            clean = id_text.replace('0x', '').strip()
            if clean and all(c in '0123456789ABCDEFabcdef' for c in clean):
                self.reverse.select(clean)

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        """Plot the inter-frame interval of the ECU identifier from the selected gateway result."""
        if 0 <= index < len(self._rows):
            id_text = self._rows[index].get('id', '')
            clean = id_text.replace('0x', '').strip()
            if clean and all(c in '0123456789ABCDEFabcdef' for c in clean):
                frame_id = int(clean, 16)
                extended = len(clean) > 3
                self.reverse.connection.track_interval(frame_id, extended)
                return ''
        return 'Nothing to plot'

    def clear(self) -> None:
        self._rows = []
        self._messages = []
        self._status = ''
        self.changed.emit()


class RasterController(_Tool):
    """Builds the bit-transition raster of the selected identifier — the value of every bit across the whole
    capture — for the activity map. Built in a worker thread; the painter (BitRaster) reads the result."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._raster: Optional[Raster] = None

    def raster(self) -> Optional[Raster]:
        """The current raster, read by the BitRaster painter (not a QML-exposed value)."""
        return self._raster

    @pyqtProperty(bool, notify=changed)
    def has_data(self) -> bool:
        return self._raster is not None and self._raster.columns > 0

    @pyqtProperty(int, notify=changed)
    def frames(self) -> int:
        return self._raster.frames if self._raster is not None else 0

    @pyqtProperty(str, notify=changed)
    def span(self) -> str:
        if self._raster is None or self._raster.frames == 0:
            return ''
        return f'{self._raster.end - self._raster.start:.1f} s'

    @pyqtSlot()
    def refresh(self):
        """Rebuild the raster of the selected identifier from the trace so far."""
        if not self._running:
            self._start(self._refresh())

    async def _refresh(self) -> None:
        selected = self.reverse.selected
        if not selected:
            self._raster = None
            self._status = 'Select an identifier'
            self.changed.emit()
            return
        key = (int(selected, 16), len(selected) > 3)
        self._status = 'Reading the trace…'
        self.changed.emit()
        messages = await self.reverse.snapshot()
        frames = [
            msg
            for msg in messages
            if (msg.arbitration_id, msg.is_extended_id) == key and not msg.is_remote_frame and not msg.is_error_frame
        ]
        times = [msg.timestamp for msg in frames]
        payloads = [bytes(msg.data) for msg in frames]
        self._raster = await asyncio.to_thread(build_raster, times, payloads)
        self._status = f'{self._raster.frames:,} frames, {self._raster.length} bytes' if self._raster.frames else ''
        self.changed.emit()


class SecOcReverseController(_Tool):
    """SecOC reverse engineering: layout detection, freshness reconstruction, strength assessment, key recovery
    and bypass detection from the captured frames of each identifier."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._rows: List[Dict[str, Any]] = []

    @pyqtSlot(bool)
    def run(self, selected_only: bool):
        if not self._running:
            self._start(self._run(selected_only))

    async def _run(self, selected_only: bool) -> None:
        from can_explorer.core.reverse.crypto import (
            detect_secoc_bypass,
            detect_secoc_layout,
            reconstruct_freshness,
            secoc_strength_assessment,
            try_secoc_key_recovery,
        )

        self._status = 'Reading the trace…'
        self._rows = []
        self.changed.emit()
        messages = await self.reverse.snapshot()
        selected = self.reverse.selected
        if selected_only and selected:
            key = (int(selected, 16), len(selected) > 3)
            messages = [msg for msg in messages if (msg.arbitration_id, msg.is_extended_id) == key]

        frames = await asyncio.to_thread(collect, messages)
        frames = {k: item for k, item in frames.items() if not is_obd_identifier(k)}
        rows: List[Dict[str, Any]] = []
        total = len(frames)
        done = 0

        for frame_key, id_frames in sorted(frames.items()):
            done += 1
            if done % 5 == 0:
                self._status = f'Analysing SecOC… {done} of {total} identifiers'
                self.changed.emit()
                await asyncio.sleep(0)

            frame_id, extended = frame_key
            id_text = _id_text(frame_id, extended)
            payloads = id_frames.payloads
            timestamps = id_frames.times

            if len(payloads) < 30:
                continue

            length = len(payloads[0]) if payloads else 0
            if length < 4:
                continue

            layout = await asyncio.to_thread(detect_secoc_layout, list(payloads), identifier=frame_id)
            if layout is None:
                continue

            rows.append(
                {
                    'id': id_text,
                    'category': 'Layout',
                    'finding': (
                        f'MAC bytes {layout.mac_start}-{layout.mac_end - 1}, '
                        f'freshness bytes {layout.freshness_start}-{layout.freshness_end - 1}'
                    ),
                    'severity': 'info',
                    'confidence': round(layout.confidence * 100),
                    'details': '; '.join(layout.findings[:3]),
                }
            )

            # Freshness reconstruction
            try:
                fresh = await asyncio.to_thread(
                    reconstruct_freshness,
                    list(payloads),
                    list(timestamps),
                    layout.freshness_start,
                    layout.freshness_end,
                    identifier=frame_id,
                )
                rows.append(
                    {
                        'id': id_text,
                        'category': 'Freshness',
                        'finding': (
                            f'{fresh.counter_width_bits}-bit {fresh.freshness_type}, rate {fresh.counter_rate:.1f}/s'
                        ),
                        'severity': 'warning' if fresh.wrap_count > 0 else 'info',
                        'confidence': round(fresh.confidence * 100),
                        'details': '; '.join(fresh.findings[:3]),
                    }
                )
            except Exception:
                pass

            # Strength assessment
            try:
                strength = await asyncio.to_thread(
                    secoc_strength_assessment,
                    list(payloads),
                    list(timestamps),
                    layout.mac_start,
                    layout.mac_end,
                    layout.freshness_start,
                    layout.freshness_end,
                    identifier=frame_id,
                )
                sev = 'finding' if strength.severity == 'critical' else strength.severity
                rows.append(
                    {
                        'id': id_text,
                        'category': 'Strength',
                        'finding': f'{strength.mac_bits}-bit MAC, {strength.freshness_bits}-bit freshness',
                        'severity': sev,
                        'confidence': 100,
                        'details': '; '.join(strength.findings[:3]),
                    }
                )
            except Exception:
                pass

            # Key recovery
            try:
                recovery = await asyncio.to_thread(
                    try_secoc_key_recovery,
                    list(payloads),
                    layout.mac_start,
                    layout.mac_end,
                    layout.freshness_start,
                    layout.freshness_end,
                    identifier=frame_id,
                )
                if recovery.recovered_key is not None:
                    rows.append(
                        {
                            'id': id_text,
                            'category': 'Key recovery',
                            'finding': f'Key recovered ({recovery.key_source})',
                            'severity': 'finding',
                            'confidence': round(recovery.confidence * 100),
                            'details': f'{recovery.verified_frames} frames verified; '
                            + '; '.join(recovery.findings[:2]),
                        }
                    )
            except Exception:
                pass

            # Bypass detection
            try:
                bypasses = await asyncio.to_thread(
                    detect_secoc_bypass,
                    list(payloads),
                    list(timestamps),
                    mac_start=layout.mac_start,
                    mac_end=layout.mac_end,
                    freshness_start=layout.freshness_start,
                    freshness_end=layout.freshness_end,
                    identifier=frame_id,
                )
                for bp in bypasses:
                    rows.append(
                        {
                            'id': id_text,
                            'category': 'Bypass',
                            'finding': bp.bypass_type.replace('_', ' '),
                            'severity': bp.severity,
                            'confidence': 100,
                            'details': bp.description,
                        }
                    )
            except Exception:
                pass

        self._rows = rows
        secoc_ids = len({r['id'] for r in rows if r['category'] == 'Layout'})
        finding_count = sum(1 for r in rows if r['severity'] in ('warning', 'finding'))
        self._status = f'{secoc_ids} SecOC-protected identifier(s), {finding_count} finding(s)'

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        return self._rows

    @pyqtSlot(int)
    def use(self, index: int):
        if 0 <= index < len(self._rows):
            id_text = self._rows[index].get('id', '')
            clean = id_text.replace('0x', '').strip()
            if clean and all(c in '0123456789ABCDEFabcdef' for c in clean):
                self.reverse.select(clean)

    @pyqtSlot(int, result=str)
    def plot(self, index: int) -> str:
        if 0 <= index < len(self._rows):
            id_text = self._rows[index].get('id', '')
            clean = id_text.replace('0x', '').strip()
            if clean and all(c in '0123456789ABCDEFabcdef' for c in clean):
                frame_id = int(clean, 16)
                extended = len(clean) > 3
                self.reverse.connection.track_interval(frame_id, extended)
                return ''
        return 'Nothing to plot'

    def clear(self) -> None:
        self._rows = []
        self._status = ''
        self.changed.emit()


class SecurityDashboardController(_Tool):
    """Attack surface dashboard: combined security posture from gateway analysis, SecOC analysis, seed quality
    and NRC fingerprints. Runs all analyses and produces a summary with per-ECU scores."""

    changed = pyqtSignal()
    running = pyqtProperty(bool, fget=_Tool._get_running, notify=changed)
    status = pyqtProperty(str, fget=_Tool._get_status, notify=changed)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(reverse, parent)
        self._rows: List[Dict[str, Any]] = []
        self._stats: Dict[str, Any] = {}

    @pyqtSlot()
    def run(self):
        if not self._running:
            self._start(self._run())

    async def _run(self) -> None:
        from can_explorer.core.reverse.gateway_security import (
            attack_surface_report,
            map_gateway,
        )

        self._status = 'Collecting data for the security dashboard…'
        self._rows = []
        self._stats = {}
        self.changed.emit()
        messages = await self.reverse.snapshot()

        self._status = 'Mapping ECU topology…'
        self.changed.emit()
        gw_map = await asyncio.to_thread(map_gateway, messages)

        self._status = 'Building attack surface report…'
        self.changed.emit()
        report = await asyncio.to_thread(attack_surface_report, messages)

        rows: List[Dict[str, Any]] = []

        bypass_by_severity = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'info': 0}
        for entry in report.entries:
            sev = entry.severity if entry.severity in bypass_by_severity else 'info'
            bypass_by_severity[sev] += 1
            rows.append(
                {
                    'ecu_id': f'0x{entry.ecu_id:03X}',
                    'category': entry.category,
                    'finding': entry.finding,
                    'severity': entry.severity,
                    'details': entry.details,
                }
            )

        ecu_scores: List[Dict[str, Any]] = []
        for ecu_id, score in sorted(report.ecu_scores.items()):
            ecu_scores.append(
                {
                    'ecu_id': f'0x{ecu_id:03X}',
                    'score': round(score, 1),
                    'grade': 'A'
                    if score <= 2
                    else 'B'
                    if score <= 4
                    else 'C'
                    if score <= 6
                    else 'D'
                    if score <= 8
                    else 'F',
                }
            )

        self._stats = {
            'ecu_count': len(gw_map.ecus),
            'overall_score': round(report.overall_score, 1),
            'overall_grade': (
                'A'
                if report.overall_score <= 2
                else 'B'
                if report.overall_score <= 4
                else 'C'
                if report.overall_score <= 6
                else 'D'
                if report.overall_score <= 8
                else 'F'
            ),
            'bypass_by_severity': bypass_by_severity,
            'ecu_scores': ecu_scores,
            'summary': report.summary[:5],
            'total_findings': len(report.entries),
        }

        self._rows = rows
        self._status = (
            f'{len(gw_map.ecus)} ECU(s), score {report.overall_score:.1f}/10, {len(report.entries)} finding(s)'
        )

    @pyqtProperty('QVariantList', notify=changed)
    def results(self) -> List[Dict[str, Any]]:
        return self._rows

    @pyqtProperty('QVariant', notify=changed)
    def stats(self) -> Dict[str, Any]:
        return self._stats

    @pyqtSlot(int)
    def use(self, index: int):
        if 0 <= index < len(self._rows):
            id_text = self._rows[index].get('ecu_id', '')
            clean = id_text.replace('0x', '').strip()
            if clean and all(c in '0123456789ABCDEFabcdef' for c in clean):
                self.reverse.select(clean)

    def clear(self) -> None:
        self._rows = []
        self._stats = {}
        self._status = ''
        self.changed.emit()
