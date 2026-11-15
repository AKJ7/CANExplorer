"""The FlexRay tool: frames from a simulated cluster, a capture or log file (pcap, BLF, ASC), or a Vector or Intrepid
interface — watching the bus or taking part in the cluster and sending frames —, decoded with a FIBEX or ARXML
cluster description, checked against its schedule, plotted, and recorded as pcap (core/flexray*.py do the work)."""

import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Deque, Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core import flexray
from can_explorer.core.flexray import FlexRayError, FlexRayEvent, FlexRayFrame
from can_explorer.core.flexray_analysis import FlexRayAnalyzer, SignalRecorder
from can_explorer.core.flexray_database import ClusterSimulator, FlexRayDatabase, encode_frame, load_database
from can_explorer.gui.plot_view import PlotView
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_FRAMES = 20000  # Kept for the trace
SHOWN_FRAMES = 500  # Rows the trace shows (the newest)
MAX_EVENTS = 2000
SHOWN_EVENTS = 300
POLL_MS = 50
ANALYSIS_MS = 500  # The analysis shown is refreshed at most this often


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


def _parse_hex(text: str) -> bytes:
    cleaned = text.replace(' ', '').replace(',', '').replace('0x', '')
    try:
        return bytes.fromhex(cleaned)
    except ValueError as e:
        raise FlexRayError(f'Not hex bytes: {text!r}') from e


class _PlotSource(QObject):
    """What a PlotView needs of a connection, for the FlexRay signals."""

    signals_updated = pyqtSignal()
    tracked_signals_changed = pyqtSignal()

    def __init__(self, recorder: SignalRecorder, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.session = SimpleNamespace(signals=recorder, start_time=None)


class FlexRayController(QObject):
    changed = pyqtSignal()
    frames_changed = pyqtSignal()
    analysis_changed = pyqtSignal()
    transmit_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None, vector_factory=None, intrepid_factory=None):
        super().__init__(parent)
        self._database: Optional[FlexRayDatabase] = None
        self._frames: Deque[FlexRayFrame] = deque(maxlen=MAX_FRAMES)
        self._events: Deque[FlexRayEvent] = deque(maxlen=MAX_EVENTS)
        self._slots: Dict[Tuple[int, str], Dict[str, Any]] = {}
        self._source = ''  # '', 'simulation', 'capture', 'vector' or 'intrepid'
        self._source_name = ''
        self._mode = ''  # 'spy', 'listen' or 'node' for interfaces
        self._simulator: Optional[ClusterSimulator] = None
        self._interface = None  # A VectorFlexRaySpy / VectorFlexRayNode / IntrepidFlexRay
        self._vector_factory = vector_factory  # For tests: callables returning a Vector spy (or node, given 'node')
        self._intrepid_factory = intrepid_factory
        self._vector_channels: List[dict] = []
        self._interfaces: List[dict] = []
        self._writer: Optional[flexray.CaptureWriter] = None
        self._error = ''
        self._paused = False
        self._filter: Optional[set] = None
        self._analyzer = FlexRayAnalyzer()
        self._analysis: Dict[str, Any] = self._analyzer.report()
        self._analysis_due = False
        self._transmissions: Dict[int, Dict[str, Any]] = {}
        self._signals = SignalRecorder()
        self._plot_source = _PlotSource(self._signals, self)
        self._plot_view = PlotView(self._plot_source, parent=self)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._analysis_timer = QTimer(self)
        self._analysis_timer.setInterval(ANALYSIS_MS)
        self._analysis_timer.timeout.connect(self._refresh_analysis)
        self._analysis_timer.start()

    # State for QML

    @pyqtProperty(str, notify=changed)
    def source(self) -> str:
        return self._source

    @pyqtProperty(str, notify=changed)
    def source_name(self) -> str:
        return self._source_name

    @pyqtProperty(str, notify=changed)
    def mode(self) -> str:
        return self._mode

    @pyqtProperty(bool, notify=changed)
    def running(self) -> bool:
        return self._timer.isActive()

    @pyqtProperty(bool, notify=changed)
    def paused(self) -> bool:
        return self._paused

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._error

    @pyqtProperty(str, notify=changed)
    def database_name(self) -> str:
        return self._database.path.name if self._database else ''

    @pyqtProperty(str, notify=changed)
    def cluster(self) -> str:
        return self._database.cluster if self._database else ''

    @pyqtProperty('QStringList', notify=changed)
    def clusters(self) -> List[str]:
        return list(self._database.clusters) if self._database else []

    @pyqtProperty('QStringList', notify=changed)
    def controllers(self) -> List[str]:
        """The ECUs whose FlexRay controller the description gives (for joining the cluster as one)."""
        return sorted(self._database.controllers) if self._database else []

    @pyqtProperty('QVariantList', notify=changed)
    def parameters(self) -> List[Dict[str, str]]:
        if self._database is None:
            return []
        return [{'label': label, 'value': value} for label, value in self._database.parameters.rows()]

    @pyqtProperty('QVariantList', notify=changed)
    def definitions(self) -> List[Dict[str, Any]]:
        if self._database is None:
            return []
        return [
            {
                'name': d.name,
                'slot': d.slot,
                'cycles': d.cycles,
                'channels': '+'.join(sorted(d.channels)),
                'length': d.length,
                'signals': ', '.join(d.signal_names),
            }
            for d in self._database.definitions
        ]

    @pyqtProperty('QVariantList', notify=changed)
    def pdus(self) -> List[Dict[str, Any]]:
        """One row per PDU of the cluster's frames: frame, slot, PDU, offset, length, update bit, signals."""
        if self._database is None:
            return []
        return [
            {
                'frame': d.name,
                'slot': d.slot,
                'pdu': pdu.name,
                'offset': pdu.offset,
                'length': pdu.length,
                'update_bit': '' if pdu.update_bit is None else f'{pdu.update_bit // 8}.{pdu.update_bit % 8}',
                'signals': ', '.join(pdu.signal_names),
            }
            for d in self._database.definitions
            for pdu in d.pdus
        ]

    @pyqtProperty(bool, notify=changed)
    def recording(self) -> bool:
        return self._writer is not None

    @pyqtProperty(str, notify=changed)
    def recording_file(self) -> str:
        return self._writer.path.name if self._writer else ''

    @pyqtProperty('QVariantList', notify=changed)
    def vector_channels(self) -> List[dict]:
        return list(self._vector_channels)

    @pyqtProperty('QVariantList', notify=changed)
    def interfaces(self) -> List[dict]:
        """The FlexRay channels of the Vector and Intrepid interfaces found: {'name', 'kind', …}."""
        return [{key: value for key, value in entry.items() if key != 'mask'} for entry in self._interfaces]

    @pyqtProperty('QVariantMap', notify=frames_changed)
    def statistics(self) -> Dict[str, Any]:
        frames = self._frames
        return {
            'frames': len(frames),
            'slots': len({slot for slot, _ in self._slots}),
            'cycle': frames[-1].cycle if frames else -1,
            'errors': sum(1 for frame in frames if frame.errors),
            'null': sum(1 for frame in frames if frame.null_frame),
            'events': len(self._events),
        }

    def _row(self, frame: FlexRayFrame, start: float) -> Dict[str, Any]:
        description = self._database.describe(frame) if self._database else ''
        flags = ''.join(letter for letter, on in (('S', frame.sync), ('U', frame.startup)) if on)
        return {
            'time': f'{frame.timestamp - start:.6f}',
            'channel': frame.channel,
            'slot': frame.slot,
            'cycle': frame.cycle,
            'length': len(frame.payload),
            'flags': flags + (' Tx' if frame.transmitted else ''),
            'data': frame.payload.hex(' ').upper() if not frame.null_frame else '(null frame)',
            'info': description or ('', ', '.join(frame.errors) + ' error')[bool(frame.errors)],
            'error': bool(frame.errors),
        }

    @pyqtProperty('QVariantList', notify=frames_changed)
    def frames(self) -> List[Dict[str, Any]]:
        """The newest frames (matching the slot filter), oldest first."""
        if not self._frames:
            return []
        start = self._frames[0].timestamp
        shown = [f for f in self._frames if self._filter is None or f.slot in self._filter]
        return [self._row(frame, start) for frame in shown[-SHOWN_FRAMES:]]

    @pyqtProperty('QVariantList', notify=frames_changed)
    def slots(self) -> List[Dict[str, Any]]:
        """One row per slot and channel: the frame name, how often it came, its cycles and the last payload."""
        rows = []
        dynamic = self._database.parameters.is_dynamic if self._database else (lambda slot: None)
        for (slot, channel), entry in sorted(self._slots.items()):
            frame: FlexRayFrame = entry['last']
            segment = dynamic(slot)
            rows.append(
                {
                    'slot': slot,
                    'channel': channel,
                    'segment': '' if segment is None else ('dynamic' if segment else 'static'),
                    'count': entry['count'],
                    'cycles': len(entry['cycles']),
                    'null': entry['null'],
                    'data': frame.payload.hex(' ').upper() if not frame.null_frame else '(null frame)',
                    'info': (self._database.describe(frame) if self._database else '') or '',
                    'errors': entry['errors'],
                }
            )
        return rows

    @pyqtProperty('QVariantList', notify=frames_changed)
    def events(self) -> List[Dict[str, Any]]:
        """The newest symbols, controller states and errors, oldest first."""
        if not self._events:
            return []
        start = self._frames[0].timestamp if self._frames else self._events[0].timestamp
        return [
            {
                'time': f'{event.timestamp - start:.6f}',
                'kind': event.kind,
                'channel': event.channel,
                'cycle': '' if event.cycle is None else event.cycle,
                'text': event.text,
            }
            for event in list(self._events)[-SHOWN_EVENTS:]
        ]

    @pyqtProperty('QVariantMap', notify=analysis_changed)
    def analysis(self) -> Dict[str, Any]:
        """The schedule and health check of the frames held (see core/flexray_analysis.py), for QML."""
        report = self._analysis

        def ms(value):
            return f'{value * 1000:.4g} ms' if value else '–'

        channels = [
            {
                'channel': channel,
                'frames': health['frames'],
                'null': health['null'],
                'with_errors': health['with_errors'],
                'errors': ', '.join(f'{name} {count}' for name, count in health['errors'].items()),
                'cycles': health['cycles'],
                'cycle_jumps': health['cycle_jumps'],
                'sync': '–' if health['sync_min'] is None else f'{health["sync_min"]}–{health["sync_max"]}',
                'few_sync_cycles': health['few_sync_cycles'],
                'cycle_time': ms(health['cycle_time']),
            }
            for channel, health in report['channels'].items()
        ]
        timing = report['timing']
        return {
            'channels': channels,
            'issues': [
                {
                    'slot': issue['slot'],
                    'channel': issue['channel'],
                    'kind': issue['kind'],
                    'name': issue.get('name', ''),
                    'count': issue['count'],
                    'cycle': issue.get('cycle', ''),
                    'detail': issue.get('detail', ''),
                }
                for issue in report['issues']
            ],
            'timing': f'{timing["checked"]} static frames checked, largest deviation '
            f'{timing["max_deviation"] * 1e6:.1f} µs'
            if timing['checked']
            else '',
            'comparison': f'{report["channel_comparison"]["compared"]} frames on both channels compared, '
            f'{report["channel_comparison"]["mismatches"]} differ',
            'events': [f'{event["text"]} ({event["kind"]}): {event["count"]}' for event in report['events']],
            'described': self._database is not None,
            'timed': bool(self._database and self._database.parameters.static_slot_time),
        }

    @pyqtProperty(bool, notify=changed)
    def can_transmit(self) -> bool:
        return self._source == 'simulation' or self._mode == 'node'

    @pyqtProperty('QVariantList', notify=transmit_changed)
    def transmissions(self) -> List[Dict[str, Any]]:
        return [
            {
                'slot': slot,
                'name': entry['name'],
                'channels': entry['channels'],
                'cycles': entry['cycles'],
                'data': entry['payload'].hex(' ').upper(),
            }
            for slot, entry in sorted(self._transmissions.items())
        ]

    @pyqtProperty('QVariantList', notify=changed)
    def signals(self) -> List[Dict[str, Any]]:
        """The cluster's signals, one row per frame and signal, for choosing what to plot or send."""
        if self._database is None:
            return []
        rows, seen = [], set()
        for index, d in enumerate(self._database.definitions):
            for pdu in d.pdus:
                if pdu.message is None:
                    continue
                for signal in pdu.message.signals:
                    if (d.name, signal.name) in seen:
                        continue
                    seen.add((d.name, signal.name))
                    rows.append(
                        {
                            'frame': d.name,
                            'definition': index,
                            'signal': signal.name,
                            'unit': signal.unit or '',
                            'choices': ', '.join(f'{k}={v}' for k, v in (signal.choices or {}).items()),
                        }
                    )
        return rows

    @pyqtProperty(QObject, constant=True)
    def plot_view(self) -> PlotView:
        return self._plot_view

    @pyqtProperty('QStringList', notify=changed)
    def tracked_signals(self) -> List[str]:
        return self._signals.tracked

    # Database

    @pyqtSlot(QUrl, result=str)
    def load_database(self, url: QUrl) -> str:
        return self._load(_local_path(url), None)

    @pyqtSlot(str, result=str)
    def choose_cluster(self, name: str) -> str:
        """Use another FlexRay cluster of the loaded file. Returns an error, or ''."""
        if self._database is None:
            return 'Load the cluster description first'
        return self._load(self._database.path, name)

    def _load(self, path: Path, cluster: Optional[str]) -> str:
        try:
            self._database = load_database(path, cluster)
        except FlexRayError as e:
            return str(e)
        self._signals.untrack_all()
        self._plot_source.tracked_signals_changed.emit()
        self._reanalyse()
        self.changed.emit()
        self.frames_changed.emit()
        return ''

    @pyqtSlot()
    def unload_database(self):
        self._database = None
        self._signals.untrack_all()
        self._plot_source.tracked_signals_changed.emit()
        self._reanalyse()
        self.changed.emit()
        self.frames_changed.emit()

    # Sources

    def _stop_source(self) -> None:
        self._timer.stop()
        if self._interface is not None:
            try:
                self._interface.close()
            except FlexRayError as e:
                logger.warning(f'FlexRay: closing the interface: {e}')
            self._interface = None
        self._simulator = None
        self._mode = ''
        if self._transmissions:
            self._transmissions.clear()
            self.transmit_changed.emit()

    def _set_error(self, error: str) -> str:
        self._error = error
        self.changed.emit()
        return error

    @pyqtSlot(int, result=str)
    def start_simulation(self, cycle_ms: int) -> str:
        """Simulate the loaded cluster (cycle time in milliseconds; 0: the description's). Returns an error, or ''."""
        if self._database is None:
            return self._set_error('Load the cluster description (FIBEX or ARXML) first')
        self._stop_source()
        self._simulator = ClusterSimulator(self._database, max(0, cycle_ms) / 1000 or None)
        self._source, self._source_name, self._error = (
            'simulation',
            f'Simulated {self._database.cluster or "cluster"}',
            '',
        )
        self._analyzer.reset()
        self._timer.start()
        self.changed.emit()
        return ''

    @pyqtSlot(QUrl, result=str)
    def open_capture(self, url: QUrl) -> str:
        """Show the FlexRay frames and events of a pcap, pcapng, BLF or ASC file. Returns an error, or ''."""
        path = _local_path(url)
        try:
            log = flexray.read_log(path)
        except FlexRayError as e:
            return self._set_error(str(e))
        self._stop_source()
        self.clear()
        self._add(log.frames)
        self._events.extend(log.events)
        self._analyzer.add_events(log.events)
        self._analyzer.flush()
        self._source, self._source_name, self._error = 'capture', f'{path.name} ({log.format})', ''
        self._refresh_analysis(force=True)
        self.changed.emit()
        self.frames_changed.emit()
        return ''

    def _open_vector(self, mode: str = 'spy'):
        if self._vector_factory is not None:
            return self._vector_factory() if mode == 'spy' else self._vector_factory(mode)
        from can_explorer.core.flexray_vector import VectorFlexRayNode, VectorFlexRaySpy

        return VectorFlexRaySpy() if mode == 'spy' else VectorFlexRayNode()

    def _open_intrepid(self):
        if self._intrepid_factory is not None:
            return self._intrepid_factory()
        from can_explorer.core.flexray_intrepid import IntrepidFlexRay

        return IntrepidFlexRay()

    @pyqtSlot(result=str)
    def refresh_vector_channels(self) -> str:
        """List the Vector channels with FlexRay. Returns an error, or ''."""
        try:
            spy = self._open_vector()
            try:
                self._vector_channels = spy.channels()
            finally:
                spy.close()
        except FlexRayError as e:
            self._vector_channels = []
            return self._set_error(str(e))
        self.changed.emit()
        return '' if self._vector_channels else self._set_error('No Vector channel with FlexRay was found')

    @pyqtSlot(int, result=str)
    def start_vector(self, index: int) -> str:
        """Spy on a Vector FlexRay channel (an index into vector_channels). Returns an error, or ''."""
        if not 0 <= index < len(self._vector_channels):
            return self._set_error('Choose a Vector channel (Find channels first)')
        channel = self._vector_channels[index]
        self._stop_source()
        try:
            self._interface = self._open_vector()
            self._interface.start(channel['mask'])
        except FlexRayError as e:
            self._stop_source()
            return self._set_error(str(e))
        self._started('vector', channel['name'], 'spy')
        return ''

    @pyqtSlot(result=str)
    def refresh_interfaces(self) -> str:
        """Find the FlexRay channels of the Vector and Intrepid interfaces. Returns an error, or ''."""
        found, errors = [], []
        try:
            spy = self._open_vector()
            try:
                found += [{**channel, 'kind': 'vector'} for channel in spy.channels()]
            finally:
                spy.close()
        except FlexRayError as e:
            errors.append(f'Vector: {e}')
        try:
            intrepid = self._open_intrepid()
            try:
                found += [{**channel, 'kind': 'intrepid'} for channel in intrepid.channels()]
            finally:
                intrepid.close()
        except FlexRayError as e:
            errors.append(f'Intrepid: {e}')
        self._interfaces = found
        self._vector_channels = [entry for entry in found if entry['kind'] == 'vector']
        self.changed.emit()
        if found:
            return ''
        return self._set_error('No FlexRay interface was found. ' + ' '.join(errors))

    @pyqtSlot(int, str, str, bool, QUrl, result=str)
    def start_interface(self, index: int, mode: str, ecu: str, coldstart: bool, config: QUrl) -> str:
        """Start an interface channel (an index into interfaces): `mode` 'spy' (Vector) or 'listen' (Intrepid) to
        watch the bus, 'node' to take part and send — as `ecu` of the description (Intrepid) or with an
        XLfrClusterConfig file `config` or the channel's own configuration (Vector), as the leading coldstart node
        when `coldstart`. Returns an error, or ''."""
        if not 0 <= index < len(self._interfaces):
            return self._set_error('Choose an interface (Find Interfaces first)')
        entry = self._interfaces[index]
        self._stop_source()
        try:
            if entry['kind'] == 'vector':
                self._start_vector_interface(entry, mode, coldstart, config)
            else:
                self._start_intrepid_interface(entry, mode, ecu, coldstart)
        except FlexRayError as e:
            self._stop_source()
            return self._set_error(str(e))
        self._started(
            entry['kind'],
            entry['name'],
            'node' if mode == 'node' else ('spy' if entry['kind'] == 'vector' else 'listen'),
        )
        return ''

    def _start_vector_interface(self, entry: dict, mode: str, coldstart: bool, config: QUrl) -> None:
        from can_explorer.core.flexray_vector import load_config_file

        if mode != 'node':
            self._interface = self._open_vector()
            self._interface.start(entry['mask'])
            return
        values = None
        path = _local_path(config) if isinstance(config, QUrl) and not config.isEmpty() else None
        if path is not None and str(path):
            try:
                values = load_config_file(path)
            except (OSError, ValueError) as e:
                raise FlexRayError(f'Cannot read {path.name}: {e}') from e
        self._interface = self._open_vector('node')
        key_slot, key_payload, key_channels = None, b'', 'AB'
        if coldstart:
            key_slot, key_payload, key_channels = self._key_frame(values)
        self._interface.start(entry['mask'], values, coldstart, key_slot, key_payload, key_channels)

    def _key_frame(self, values: Optional[Dict[str, int]]) -> Tuple[int, bytes, str]:
        """The coldstart node's key slot frame: the description's controller key slot, else the first frame sent every
        cycle; its payload as encoded with signal values 0."""
        slot = None
        if self._database is not None:
            for controller in self._database.controllers.values():
                if controller.get('KEY-SLOT-ID'):
                    slot = int(float(controller['KEY-SLOT-ID']))
                    break
            definitions = self._database.definitions_for_slot(slot) if slot else []
            if not definitions:
                definitions = [d for d in self._database.definitions if d.repetition == 1][:1]
            if definitions:
                definition = definitions[0]
                return definition.slot, encode_frame(definition, {}), '+'.join(sorted(definition.channels))
        raise FlexRayError(
            'A coldstart node needs a key slot: load a cluster description with a frame sent every cycle'
        )

    def _start_intrepid_interface(self, entry: dict, mode: str, ecu: str, coldstart: bool) -> None:
        configuration = None
        if mode == 'node':
            from can_explorer.core.flexray_intrepid import node_configuration

            if self._database is None or not ecu:
                raise FlexRayError('Taking part in the cluster needs the description and the ECU to be')
            cluster, controller, missing = node_configuration(self._database, ecu)
            if missing:
                raise FlexRayError(
                    f'The description does not give these FlexRay parameters of the cluster or of {ecu}: '
                    + ', '.join(missing)
                )
            configuration = (cluster, controller)
        self._interface = self._open_intrepid()
        self._interface.start(entry['device'], entry['controller'], configuration, coldstart)

    def _started(self, source: str, name: str, mode: str) -> None:
        self._source, self._source_name, self._mode, self._error = source, name, mode, ''
        self._analyzer.reset()
        self._timer.start()
        self.changed.emit()

    @pyqtSlot()
    def stop(self):
        self._stop_source()
        self.changed.emit()

    @pyqtSlot(bool)
    def set_paused(self, paused: bool):
        self._paused = paused
        self.changed.emit()

    @pyqtSlot(str, result=str)
    def set_filter(self, text: str) -> str:
        """Show only some slots: numbers and ranges, e.g. "12 40-50" ('' for all). Returns an error, or ''."""
        slots: set = set()
        for part in text.replace(',', ' ').split():
            low, _, high = part.partition('-')
            try:
                first, last = int(low), int(high or low)
            except ValueError:
                return f'Not a slot or slot range: {part}'
            slots.update(range(first, last + 1))
        self._filter = slots or None
        self.frames_changed.emit()
        return ''

    @pyqtSlot()
    def clear(self):
        self._frames.clear()
        self._events.clear()
        self._slots.clear()
        self._signals.clear()
        self._analyzer.reset()
        self._refresh_analysis(force=True)
        self._plot_source.signals_updated.emit()
        self.frames_changed.emit()

    # Sending

    def _transmitter(self):
        if self._source == 'simulation' and self._simulator is not None:
            return self._simulator
        if self._mode == 'node' and self._interface is not None:
            return self._interface
        raise FlexRayError('Sending needs the simulation or an interface taking part in the cluster (node)')

    @pyqtSlot(int, 'QVariantMap', str, result=str)
    def transmit_definition(self, index: int, values: Dict[str, Any], channels: str) -> str:
        """Send a frame of the description (an index into definitions) in its slot and cycles, with signal values
        (missing ones are 0; every PDU's update bit set), on `channels` ('' for its own). Returns an error, or ''."""
        if self._database is None or not 0 <= index < len(self._database.definitions):
            return 'Choose a frame of the cluster description'
        definition = self._database.definitions[index]
        try:
            numbers = {name: float(value) if isinstance(value, str) else value for name, value in values.items()}
            payload = encode_frame(definition, numbers)
        except (FlexRayError, ValueError) as e:
            return str(e)
        channels = channels or '+'.join(sorted(definition.channels))
        repetition = definition.repetition
        return self._transmit(
            definition.slot, definition.base_cycle % repetition, repetition, channels, payload, definition.name,
            definition.cycles,
        )  # fmt: skip

    @pyqtSlot(int, int, int, str, str, result=str)
    def transmit_raw(self, slot: int, base_cycle: int, repetition: int, channels: str, data: str) -> str:
        """Send bytes (hex) in `slot` from `base_cycle`, every `repetition` cycles. Returns an error, or ''."""
        try:
            payload = _parse_hex(data)
        except FlexRayError as e:
            return str(e)
        cycles = 'every cycle' if repetition == 1 else f'from {base_cycle}, every {repetition}'
        return self._transmit(slot, base_cycle, repetition, channels or 'A', payload, '', cycles)

    def _transmit(self, slot, base, repetition, channels, payload, name, cycles) -> str:
        if not 1 <= slot <= flexray.MAX_SLOT:
            return f'A FlexRay slot is 1 to {flexray.MAX_SLOT}'
        try:
            self._transmitter().transmit(slot, base, repetition, channels, payload)
        except FlexRayError as e:
            return str(e)
        self._transmissions[slot] = {'name': name, 'channels': channels, 'cycles': cycles, 'payload': payload}
        self.transmit_changed.emit()
        return ''

    @pyqtSlot(int, result=str)
    def stop_transmit(self, slot: int) -> str:
        if slot not in self._transmissions:
            return ''
        try:
            self._transmitter().stop_transmit(slot)
        except FlexRayError as e:
            return str(e)
        del self._transmissions[slot]
        self.transmit_changed.emit()
        return ''

    # Plotting

    @pyqtSlot(str, str, result=str)
    def plot_signal(self, frame: str, signal: str) -> str:
        unit = ''
        if self._database is not None:
            for d in self._database.definitions:
                for pdu in d.pdus:
                    if d.name == frame and pdu.message is not None:
                        unit = next((s.unit or '' for s in pdu.message.signals if s.name == signal), unit)
        key = self._signals.track(frame, signal, unit)
        self._signals.update(self._frames, self._database)  # The frames held already
        self._plot_source.tracked_signals_changed.emit()
        self._plot_source.signals_updated.emit()
        self.changed.emit()
        return key

    @pyqtSlot(str)
    def untrack_key(self, key: str):
        self._signals.untrack_key(key)
        self._plot_source.tracked_signals_changed.emit()
        self.changed.emit()

    # Recording

    @pyqtSlot(QUrl, result=str)
    def start_recording(self, url: QUrl) -> str:
        """Record the frames that arrive from now on as pcap (LINKTYPE_FLEXRAY). Returns an error, or ''."""
        path = _local_path(url)
        if path.suffix.lower() not in ('.pcap', '.pcapng'):
            path = path.with_suffix('.pcap')
        self.stop_recording()
        try:
            self._writer = flexray.CaptureWriter(path)
        except OSError as e:
            return self._set_error(f'Cannot write {path.name}: {e}')
        self.changed.emit()
        return ''

    @pyqtSlot()
    def stop_recording(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self.changed.emit()

    @pyqtSlot(QUrl, result=str)
    def save_frames(self, url: QUrl) -> str:
        """Write the frames held now as pcap. Returns an error, or ''."""
        path = _local_path(url)
        if path.suffix.lower() != '.pcap':
            path = path.with_suffix('.pcap')
        try:
            flexray.write_capture(path, list(self._frames))
        except (OSError, FlexRayError) as e:
            return self._set_error(f'Cannot write {path.name}: {e}')
        return ''

    # Frames arriving

    def _add(self, frames: List[FlexRayFrame]) -> None:
        for frame in frames:
            if self._writer is not None:
                self._writer.write(frame)
        if self._paused:
            return
        for frame in frames:
            self._frames.append(frame)
            entry = self._slots.setdefault(
                (frame.slot, frame.channel), {'count': 0, 'cycles': set(), 'errors': 0, 'null': 0, 'last': frame}
            )
            entry['count'] += 1
            entry['cycles'].add(frame.cycle)
            entry['errors'] += 1 if frame.errors else 0
            entry['null'] += frame.null_frame
            entry['last'] = frame
        self._analyzer.add(frames)
        self._analysis_due = True
        if self._signals.series:
            self._signals.update(frames, self._database)
            if self._signals.drain_changed():
                self._plot_source.signals_updated.emit()

    def _add_events(self, events: List[FlexRayEvent]) -> None:
        if not events:
            return
        if self._writer is not None:
            for event in events:
                if event.kind == 'symbol' and event.channel in ('A', 'B'):
                    self._writer.write_symbol(event)
        if not self._paused:
            self._events.extend(events)
            self._analyzer.add_events(events)
            self._analysis_due = True

    def _reanalyse(self) -> None:
        self._analyzer.set_database(self._database)
        self._analyzer.add(list(self._frames))
        if self._source == 'capture':
            self._analyzer.flush()
        self._analyzer.add_events(self._events)
        self._refresh_analysis(force=True)

    def _refresh_analysis(self, force: bool = False) -> None:
        if not (force or self._analysis_due):
            return
        self._analysis_due = False
        self._analysis = self._analyzer.report()
        self.analysis_changed.emit()

    def _poll(self) -> None:
        events: List[FlexRayEvent] = []
        try:
            if self._simulator is not None:
                frames = self._simulator.frames_until(time.time())
            elif self._interface is not None:
                frames = self._interface.receive()
                take = getattr(self._interface, 'take_events', None)
                events = take() if take is not None else []
            else:
                return
        except FlexRayError as e:
            self._stop_source()
            self._set_error(str(e))
            return
        if frames or events:
            self._add(frames)
            self._add_events(events)
            self.frames_changed.emit()

    def shutdown(self):
        self._stop_source()
        self.stop_recording()
        self._analysis_timer.stop()
