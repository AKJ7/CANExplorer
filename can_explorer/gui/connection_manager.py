import asyncio
import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import can
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QGuiApplication

from can_explorer.core.database_io import WRITE_SUFFIXES, convert_database
from can_explorer.core.decoding import DATABASE_SUFFIXES, FrameDecoder, open_signal_database
from can_explorer.core.diagnostic_description import DESCRIPTION_SUFFIXES
from can_explorer.core.filters import MessageFilter
from can_explorer.core.frames import parse_can_id, parse_hex_bytes
from can_explorer.core.ldf import LdfDecoder
from can_explorer.core.lin import DEFAULT_BAUDRATE, LIN_BAUDRATES, MAX_FRAME_ID
from can_explorer.core.lin_schedule import LinScheduler
from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder
from can_explorer.core.recording import Trigger, TriggeredRecorder
from can_explorer.core.session import BusSession, SessionError, SessionState
from can_explorer.core.log_formats import file_filters
from can_explorer.core.sources import BusConfig, FileSource, LiveSource, MessageSource
from can_explorer.gui.app_state import AppState
from can_explorer.gui.compare import CompareController
from can_explorer.gui.database_editor import DatabaseEditorController
from can_explorer.gui.canopen import CanOpenController
from can_explorer.gui.diagnostics import DiagnosticsController
from can_explorer.gui.flashing import FlashController
from can_explorer.gui.j1939 import J1939Controller
from can_explorer.gui.playback import BusReplay, ReplayPlayback
from can_explorer.gui.simulation import SimulationController
from can_explorer.gui.plot_view import PlotView
from can_explorer.gui.testing import TestController
from can_explorer.gui.trace_tools import TraceTools
from can_explorer.gui.workspace import SUFFIX as WORKSPACE_SUFFIX
from can_explorer.gui.workspace import WorkspaceError, open_workspace, save_workspace
from can_explorer.gui.models import OverviewModel, TraceModel
from can_explorer.sys.log_handler import get_logger
from can_explorer.util import canutils

logger = get_logger(__name__)

STATISTICS_INTERVAL_MS = 1_000
HISTORY_SECONDS = 60
TOP_IDENTIFIERS = 8
DATABASE_FORMAT_NAMES = {
    '.dbc': 'Vector DBC',
    '.dbf': 'BUSMASTER DBF',
    '.arxml': 'AUTOSAR ARXML',
    '.kcd': 'Kayak KCD',
    '.sym': 'PCAN Symbol',
    '.ldf': 'LIN description LDF',
    '.cdd': 'CANdelaStudio CDD',
}
FD_DATA_BITRATES = (1_000_000, 2_000_000, 4_000_000, 5_000_000, 8_000_000)


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


class CanConnection(QObject):
    """Qt adapter around a BusSession: exposes its state and models to QML."""

    state_changed = pyqtSignal()
    statistics_changed = pyqtSignal()
    bus_statistics_changed = pyqtSignal()
    recording_changed = pyqtSignal()
    database_changed = pyqtSignal()
    filter_changed = pyqtSignal()
    paused_changed = pyqtSignal()
    periodic_changed = pyqtSignal()
    tracked_signals_changed = pyqtSignal()
    signals_updated = pyqtSignal()
    schedule_changed = pyqtSignal()
    error_occurred = pyqtSignal(str)
    recording_event = pyqtSignal(str)  # Trigger events of a triggered recording

    def __init__(
        self,
        name: str,
        session: BusSession,
        protocol: str,
        max_rows: int,
        refresh_interval_ms: int,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._name = name
        self._protocol = protocol
        self.session = session
        describe = session.decoder.full_description
        self._trace = TraceModel(session.trace, lambda: self.session.start_time, describe, parent=self)
        self._overview = OverviewModel(session.statistics, describe, parent=self)
        self._plot_view = PlotView(self, parent=self)
        self._trace_tools = TraceTools(self._trace, lambda: session.decoder.database, describe, parent=self)
        self._filter = MessageFilter()
        self._filter_error = ''
        self._paused = False
        self._frame_count = 0
        self._recording_status = ''
        self.origin: Dict[str, Any] = {}  # How the connection was created, for workspaces
        self._scheduler: Optional[LinScheduler] = None
        self._history: deque = deque(maxlen=HISTORY_SECONDS)
        self._diagnostics = DiagnosticsController(self, parent=self)
        self._j1939 = J1939Controller(self, parent=self)
        self._canopen = CanOpenController(self, parent=self)
        self._flashing = FlashController(self, parent=self)
        self._simulation = SimulationController(self, parent=self)
        self._tests = TestController(self, parent=self)
        self._replay = ReplayPlayback(self, parent=self)
        self._bus_replay = BusReplay(self, parent=self)
        self._timer = QTimer(self)
        self._timer.setInterval(refresh_interval_ms)
        self._timer.timeout.connect(self._refresh)
        self._statistics_timer = QTimer(self)
        self._statistics_timer.setInterval(STATISTICS_INTERVAL_MS)
        self._statistics_timer.timeout.connect(self._sample_bus_statistics)
        session.add_state_listener(self._on_session_state)
        session.add_periodic_listener(lambda _: self.periodic_changed.emit())
        session.add_error_listener(self._on_session_error)

    # Properties

    @pyqtProperty(str, constant=True)
    def name(self) -> str:
        return self._name

    @pyqtProperty(str, constant=True)
    def description(self) -> str:
        return self.session.source.description

    @pyqtProperty(str, constant=True)
    def protocol(self) -> str:
        return self._protocol

    @pyqtProperty(bool, constant=True)
    def can_transmit(self) -> bool:
        return self.session.source.can_send

    @pyqtProperty(bool, constant=True)
    def is_replay(self) -> bool:
        return isinstance(self.session.source, FileSource)

    @pyqtProperty(bool, constant=True)
    def fd(self) -> bool:
        source = self.session.source
        return isinstance(source, LiveSource) and source.config.fd

    @pyqtProperty(bool, constant=True)
    def is_lin(self) -> bool:
        source = self.session.source
        return isinstance(source, LiveSource) and source.config.is_lin

    @pyqtProperty(str, notify=state_changed)
    def state(self) -> str:
        return self.session.state.value

    @pyqtProperty(bool, notify=state_changed)
    def connected(self) -> bool:
        return self.session.is_open

    @pyqtProperty(QObject, constant=True)
    def trace(self) -> TraceModel:
        return self._trace

    @pyqtProperty(QObject, constant=True)
    def overview(self) -> OverviewModel:
        return self._overview

    @pyqtProperty(int, notify=statistics_changed)
    def frame_count(self) -> int:
        return self._frame_count

    @pyqtProperty(bool, notify=recording_changed)
    def recording(self) -> bool:
        return self.session.recording

    @pyqtProperty(str, notify=recording_changed)
    def recording_file(self) -> str:
        return str(self.session.recording_path or '')

    @pyqtProperty(str, notify=recording_changed)
    def recording_status(self) -> str:
        """E.g. 'recording to trace_002.blf (2 files)' or 'armed: EngineData.EngineSpeed > 6000'."""
        recorder = self.session.recorder
        return recorder.describe() if recorder is not None else ''

    @pyqtProperty(bool, notify=recording_changed)
    def recording_triggered(self) -> bool:
        return isinstance(self.session.recorder, TriggeredRecorder)

    @pyqtProperty(str, notify=database_changed)
    def database_file(self) -> str:
        database = self.session.decoder.database
        return str(database.path) if database is not None else ''

    @pyqtProperty(str, notify=filter_changed)
    def filter_text(self) -> str:
        return self._filter.text

    @pyqtProperty(str, notify=filter_changed)
    def filter_error(self) -> str:
        return self._filter_error

    @pyqtProperty(bool, notify=paused_changed)
    def paused(self) -> bool:
        return self._paused

    @pyqtProperty(int, notify=periodic_changed)
    def periodic_count(self) -> int:
        return len(self.session.periodic)

    # Bus statistics

    @pyqtProperty(float, notify=bus_statistics_changed)
    def rx_rate(self) -> float:
        return self.session.bus_statistics.last.rx_rate

    @pyqtProperty(float, notify=bus_statistics_changed)
    def tx_rate(self) -> float:
        return self.session.bus_statistics.last.tx_rate

    @pyqtProperty(float, notify=bus_statistics_changed)
    def bus_load(self) -> float:
        """Estimated bus load in percent, or -1 when the bitrate is unknown."""
        load = self.session.bus_statistics.last.load
        return -1.0 if load is None else load * 100

    @pyqtProperty(int, notify=bus_statistics_changed)
    def error_frames(self) -> int:
        return self.session.bus_statistics.error_frames

    @pyqtProperty(str, notify=bus_statistics_changed)
    def bus_state(self) -> str:
        return self.session.bus_state or ''

    def _sample_bus_statistics(self):
        snapshot = self.session.sample_bus_statistics()
        self._history.append(
            (snapshot.rx_rate, snapshot.tx_rate, -1.0 if snapshot.load is None else snapshot.load * 100)
        )
        self.bus_statistics_changed.emit()

    # Dashboard

    @pyqtProperty('QVariantList', notify=bus_statistics_changed)
    def rx_history(self) -> List[float]:
        """Received frames per second over the last minute, one value per second."""
        return [rx for rx, _, _ in self._history]

    @pyqtProperty('QVariantList', notify=bus_statistics_changed)
    def tx_history(self) -> List[float]:
        return [tx for _, tx, _ in self._history]

    @pyqtProperty('QVariantList', notify=bus_statistics_changed)
    def load_history(self) -> List[float]:
        """Bus load in percent over the last minute (empty when the bitrate is unknown)."""
        return [load for _, _, load in self._history if load >= 0]

    @pyqtProperty(int, notify=bus_statistics_changed)
    def identifier_count(self) -> int:
        return len(self.session.statistics.entries)

    @pyqtProperty('QVariantList', notify=bus_statistics_changed)
    def traffic_breakdown(self) -> List[Dict[str, Any]]:
        """Frames by kind, for the dashboard's donut chart."""
        counts: Dict[str, int] = {}
        for entry in self.session.statistics.entries.values():
            msg = entry.last.message
            if self.is_lin:
                kind = 'Headers' if msg.is_remote_frame else 'Responses'
            elif msg.is_remote_frame:
                kind = 'Remote'
            elif msg.is_fd:
                kind = 'CAN FD'
            else:
                kind = 'Extended' if msg.is_extended_id else 'Standard'
            counts[kind] = counts.get(kind, 0) + entry.count
        if self.session.bus_statistics.error_frames:
            counts['Error'] = self.session.bus_statistics.error_frames
        return [{'label': label, 'value': value} for label, value in sorted(counts.items(), key=lambda item: -item[1])]

    @pyqtProperty('QVariantList', notify=bus_statistics_changed)
    def top_identifiers(self) -> List[Dict[str, Any]]:
        """The most frequent identifiers with their share, cycle time and database name."""
        entries = sorted(self.session.statistics.entries.values(), key=lambda entry: -entry.count)[:TOP_IDENTIFIERS]
        if not entries:
            return []
        maximum = entries[0].count
        database = self.session.decoder.database
        result = []
        for entry in entries:
            msg = entry.last.message
            definition = database.message_definition(msg) if database is not None else None
            result.append(
                {
                    'id': f'{msg.arbitration_id:08X}' if msg.is_extended_id else f'{msg.arbitration_id:03X}',
                    'name': definition.name if definition is not None else '',
                    'direction': 'Rx' if msg.is_rx else 'Tx',
                    'count': entry.count,
                    'share': entry.count / maximum,
                    'cycle': '' if entry.cycle_time is None else f'{entry.cycle_time * 1000:.1f} ms',
                }
            )
        return result

    # Session control

    @pyqtSlot()
    def open(self):
        try:
            self.session.start()
        except SessionError as e:
            self.error_occurred.emit(str(e))

    @pyqtSlot()
    def close(self):
        self.stop_schedule()
        self._bus_replay.stop()
        self._tests.shutdown()
        self._flashing.shutdown()
        self._diagnostics.shutdown()
        self._simulation.shutdown()
        self.session.stop()

    def set_refresh_interval(self, interval_ms: int):
        self._timer.setInterval(interval_ms)

    def _on_session_state(self, session: BusSession):
        if not session.is_open:
            self._bus_replay.stop()
            self._tests.shutdown()
        self._replay.refresh()
        if session.is_open:
            self._timer.start()
            self._statistics_timer.start()
            self.session.sample_bus_statistics()
        else:
            self._timer.stop()
            self._statistics_timer.stop()
            self._refresh()
            self.bus_statistics_changed.emit()
            self.schedule_changed.emit()
        if session.state is SessionState.ERROR and session.error:
            self.error_occurred.emit(session.error)
        self.state_changed.emit()
        self.recording_changed.emit()

    def _on_session_error(self, message: str):
        self.error_occurred.emit(message)
        self.recording_changed.emit()

    def _refresh(self):
        self.session.trace.drain()  # The trace view follows the store itself
        added, changed = self.session.statistics.drain_changes()
        if not self._paused:
            self._trace.refresh()
            self._overview.refresh(added, changed)
            if self.session.signals.drain_changed():
                self.signals_updated.emit()
        if self._frame_count != self.session.trace.total:
            self._frame_count = self.session.trace.total
            self.statistics_changed.emit()
        self._replay.refresh()
        self._bus_replay.refresh()
        status = self.recording_status
        if status != self._recording_status:  # E.g. a split recording started its next file
            self._recording_status = status
            self.recording_changed.emit()

    @pyqtSlot()
    def clear(self):
        self._history.clear()
        self.session.clear()
        self._trace.rebuild()
        self._overview.rebuild()
        self._frame_count = 0
        self.statistics_changed.emit()
        self.bus_statistics_changed.emit()
        self.signals_updated.emit()

    @pyqtSlot(bool)
    def set_paused(self, paused: bool):
        if paused == self._paused:
            return
        self._paused = paused
        if not paused:
            # Catch up with everything received while the view was frozen
            self._trace.refresh()
            self._overview.rebuild()
            self.signals_updated.emit()
        self.paused_changed.emit()

    @pyqtSlot(str)
    def set_filter(self, text: str):
        try:
            message_filter = MessageFilter.parse(text)
        except ValueError as e:
            self._filter_error = str(e)
        else:
            self._filter, self._filter_error = message_filter, ''
            self._trace.set_filter(message_filter)
            self._overview.set_filter(message_filter)
        self.filter_changed.emit()

    # Recording, export and databases

    @pyqtSlot(QUrl)
    def start_recording(self, url: QUrl):
        try:
            self.session.start_recording(_local_path(url))
        except SessionError as e:
            self.error_occurred.emit(str(e))
        self.recording_changed.emit()

    @pyqtSlot(QUrl, 'QVariantMap', result=str)
    def record(self, url: QUrl, options: Dict[str, Any]) -> str:
        """Start recording with options: split_mb, split_minutes, and for a triggered recording trigger (bool),
        start, stop, pre, post (seconds) and repeat. Returns an error, or ''."""
        try:
            split_bytes = int(float(options.get('split_mb') or 0) * 1_000_000)
            split_seconds = float(options.get('split_minutes') or 0) * 60
            trigger = None
            if options.get('trigger'):
                trigger = Trigger(
                    start=str(options.get('start', '')),
                    stop=str(options.get('stop', '')),
                    pre=max(0.0, float(options.get('pre') or 0)),
                    post=max(0.0, float(options.get('post') or 0)),
                    repeat=bool(options.get('repeat')),
                )
        except (TypeError, ValueError) as e:
            return f'Invalid recording options: {e}'
        try:
            self.session.start_recording(
                _local_path(url), split_bytes, split_seconds, trigger, on_event=self._on_recording_event
            )
        except SessionError as e:
            return str(e)
        finally:
            self.recording_changed.emit()
        return ''

    def _on_recording_event(self, text: str) -> None:
        self.recording_event.emit(text)
        self.recording_changed.emit()

    @pyqtSlot()
    def stop_recording(self):
        self.session.stop_recording()
        self.recording_changed.emit()

    @pyqtSlot(QUrl, QObject, result=str)
    def export_trace(self, url: QUrl, selection_model: Optional[QObject]) -> str:
        """Write the selected trace rows (all displayed rows without a selection) to a log file."""
        frames = self._trace.selected_frames(selection_model)
        path = _local_path(url)
        try:
            count = BusSession.export(frames, path)
        except SessionError as e:
            self.error_occurred.emit(str(e))
            return ''
        return f'Exported {count} frames to {path.name}'

    @pyqtSlot(QUrl)
    def load_database(self, url: QUrl):
        path = _local_path(url)
        if path.suffix.lower() in DESCRIPTION_SUFFIXES:
            # Diagnostic descriptions describe the ECU's data identifiers, not bus signals
            error = self._diagnostics.load_description(url)
            if error:
                self.error_occurred.emit(error)
            return
        try:
            database = open_signal_database(path)
        except Exception as e:
            self.error_occurred.emit(f'Could not load {path.name}: {e}')
            return
        self.stop_schedule()
        self._scheduler = None
        self._select_bus(database)
        self.session.decoder.database = database
        logger.info(f'Loaded signal database {path}')
        self._database_changed()

    def _select_bus(self, database) -> None:
        """For databases describing several buses, pick the one whose bitrate matches the connection."""
        buses = getattr(database, 'database', None) and database.database.buses
        source = self.session.source
        if not buses or len(buses) < 2 or not isinstance(source, LiveSource):
            return
        matching = [bus.name for bus in buses if bus.baudrate == source.config.bitrate]
        if len(matching) == 1:
            database.set_bus(matching[0])
            logger.info(f'Decoding with bus {matching[0]!r} of {database.path.name} (matching bitrate)')

    @pyqtProperty('QStringList', notify=database_changed)
    def database_buses(self) -> List[str]:
        database = self.session.decoder.database
        return list(getattr(database, 'buses', []) or [])

    @pyqtProperty(str, notify=database_changed)
    def database_bus(self) -> str:
        return getattr(self.session.decoder.database, 'bus', None) or ''

    @pyqtSlot(str)
    def set_database_bus(self, bus: str):
        """Decode with the messages of one bus of the database ('' for all buses)."""
        database = self.session.decoder.database
        if database is None or not hasattr(database, 'set_bus'):
            return
        try:
            database.set_bus(bus or None)
        except ValueError as e:
            self.error_occurred.emit(str(e))
            return
        self.session.decoder.database = database  # Invalidates cached descriptions
        self._database_changed()

    @pyqtSlot()
    def unload_database(self):
        self.stop_schedule()
        self._scheduler = None
        self.session.decoder.database = None
        self.session.signals.untrack_all()
        self._database_changed()

    def _database_changed(self):
        self._trace.refresh_descriptions()
        self._overview.refresh_descriptions()
        self.database_changed.emit()
        self.tracked_signals_changed.emit()
        self.schedule_changed.emit()

    # Signals

    @pyqtProperty('QVariantList', notify=database_changed)
    def database_signals(self) -> List[Dict[str, str]]:
        database = self.session.decoder.database
        if database is None:
            return []
        return [
            {'key': info.key, 'message': info.message, 'signal': info.signal, 'unit': info.unit}
            for info in database.signals()
        ]

    @pyqtProperty('QStringList', notify=tracked_signals_changed)
    def tracked_signals(self) -> List[str]:
        return self.session.signals.tracked

    @pyqtSlot(str, str, str)
    def track_signal(self, message: str, signal: str, unit: str):
        self.session.signals.track(message, signal, unit)
        self.tracked_signals_changed.emit()

    @pyqtSlot(str, str)
    def untrack_signal(self, message: str, signal: str):
        self.session.signals.untrack(message, signal)
        self.tracked_signals_changed.emit()

    # Transmission

    @pyqtSlot(str, str, bool, bool, int, bool, bool, result=str)
    def send(
        self, can_id: str, data: str, extended: bool, fd: bool, period_ms: int, hardware: bool, remote: bool
    ) -> str:
        """Send once (period_ms == 0) or periodically. Returns an error message, or '' on success.

        remote: send an RTR frame; on LIN connections this is a header only, answered by a slave.
        """
        try:
            payload = b'' if remote else parse_hex_bytes(data)
            max_length = 64 if fd else 8
            if len(payload) > max_length:
                raise ValueError(f'At most {max_length} data bytes allowed')
            frame_id = parse_can_id(can_id, extended)
            if self.is_lin:
                if extended or fd:
                    raise ValueError('LIN frames have a 6-bit identifier and at most 8 data bytes')
                if frame_id > MAX_FRAME_ID:
                    raise ValueError(f'LIN frame identifiers are 00-3F, got {frame_id:02X}')
                if not remote and not payload:
                    raise ValueError('A LIN response needs 1-8 data bytes')
            msg = can.Message(
                arbitration_id=frame_id,
                is_extended_id=extended,
                is_fd=fd,
                bitrate_switch=fd,
                is_remote_frame=remote,
                dlc=8 if remote else None,
                data=payload,
            )
            if period_ms > 0:
                self.session.start_periodic(msg, period_ms / 1000, hardware=hardware)
            else:
                self.session.send(msg)
        except (ValueError, SessionError) as e:
            return str(e)
        return ''

    @pyqtSlot()
    def stop_periodic(self):
        self.session.stop_periodic()

    # Diagnostics and simulation

    @pyqtProperty(QObject, constant=True)
    def diagnostics(self) -> DiagnosticsController:
        return self._diagnostics

    @pyqtProperty(QObject, constant=True)
    def j1939(self) -> J1939Controller:
        """Nodes, trouble codes and sending of parameter groups on J1939 connections."""
        return self._j1939

    @pyqtProperty(QObject, constant=True)
    def flashing(self) -> FlashController:
        """Programming ECUs over UDS."""
        return self._flashing

    @pyqtProperty(QObject, constant=True)
    def canopen(self) -> CanOpenController:
        """NMT, SDO access and PDOs on CANopen connections."""
        return self._canopen

    @pyqtProperty(QObject, constant=True)
    def simulation(self) -> SimulationController:
        return self._simulation

    @pyqtProperty(QObject, constant=True)
    def plot_view(self) -> PlotView:
        """Time range, cursors and charts of the signal plots."""
        return self._plot_view

    @pyqtProperty(QObject, constant=True)
    def trace_tools(self) -> TraceTools:
        """Search and bookmarks of the trace."""
        return self._trace_tools

    @pyqtSlot(int)
    def replay_from_row(self, row: int):
        """Continue a replay at the frame of a trace row."""
        frame = self._trace.frame(row)
        if frame is not None and self.is_replay:
            self._replay.seek(self._trace.time(frame))

    @pyqtProperty(QObject, constant=True)
    def tests(self) -> TestController:
        """Test scripts, running them and their results."""
        return self._tests

    @pyqtProperty(QObject, constant=True)
    def replay(self) -> ReplayPlayback:
        """Playback of a log file replay connection (pause, speed, progress)."""
        return self._replay

    @pyqtProperty(QObject, constant=True)
    def bus_replay(self) -> BusReplay:
        """Transmission of a log file on the bus of a live connection."""
        return self._bus_replay

    # LIN master schedule

    def _ldf(self) -> Optional[LdfDecoder]:
        database = self.session.decoder.database
        return database if isinstance(database, LdfDecoder) else None

    @pyqtProperty('QStringList', notify=database_changed)
    def schedule_tables(self) -> List[str]:
        ldf = self._ldf()
        return LinScheduler.tables(ldf) if ldf is not None else []

    @pyqtProperty(bool, notify=schedule_changed)
    def schedule_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    @pyqtProperty(str, notify=schedule_changed)
    def schedule_table(self) -> str:
        return self._scheduler.table.name if self._scheduler is not None else ''

    @pyqtProperty('QVariantList', notify=schedule_changed)
    def master_signals(self) -> List[Dict[str, Any]]:
        ldf = self._ldf()
        if ldf is None:
            return []
        values = self._scheduler.values if self._scheduler is not None else {}
        return [{**entry, 'value': str(values.get(entry['signal'], entry['value']))} for entry in ldf.master_signals()]

    @pyqtSlot(str, result=str)
    def start_schedule(self, table: str) -> str:
        """Run an LDF schedule table as LIN master. Returns an error or ''."""
        ldf = self._ldf()
        if ldf is None:
            return 'Load an LDF file first'
        if not self.session.is_open:
            return 'Connect first'
        previous = self._scheduler.values if self._scheduler is not None else {}
        self.stop_schedule()
        try:
            self._scheduler = LinScheduler(self.session, ldf, table)
        except ValueError as e:
            return str(e)
        self._scheduler.values.update({k: v for k, v in previous.items() if k in self._scheduler.values})
        self._scheduler.start()
        self.schedule_changed.emit()
        return ''

    @pyqtSlot()
    def stop_schedule(self):
        if self._scheduler is not None:
            self._scheduler.stop()
        self.schedule_changed.emit()

    @pyqtSlot(str, str, result=str)
    def set_master_signal(self, signal: str, text: str) -> str:
        """Set a master signal from text: a number (decimal or 0x hex) or a logical value name."""
        ldf = self._ldf()
        if ldf is None:
            return 'Load an LDF file first'
        if self._scheduler is None:
            tables = LinScheduler.tables(ldf)
            if not tables:
                return 'The LDF has no schedule table'
            self._scheduler = LinScheduler(self.session, ldf, tables[0])
        text = text.strip()
        try:
            value = int(text, 0)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                value = text
        try:
            self._scheduler.set_signal(signal, value)
        except Exception as e:
            return f'Invalid value for {signal}: {e}'
        self.schedule_changed.emit()
        return ''


class ConnectionManager(QObject):
    connections_changed = pyqtSignal()
    detected_channels_changed = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, app_state: Optional[AppState] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connections: List[CanConnection] = []
        self._app_state = app_state if app_state is not None else AppState(parent=self)
        self._app_state.preferences_changed.connect(self._apply_preferences)
        self._detected_channels: Dict[str, List[str]] = {}
        self._detecting = False
        self._detection: Optional[asyncio.Future] = None
        self._workspace_file = ''
        self._database_editor = DatabaseEditorController(parent=self)
        self._compare = CompareController(parent=self)
        self._database_editor.saved.connect(self._reload_database)
        QGuiApplication.instance().aboutToQuit.connect(self.close_all)

    # Workspaces

    workspace_changed = pyqtSignal()

    @pyqtProperty(str, notify=workspace_changed)
    def workspace_file(self) -> str:
        return self._workspace_file

    @pyqtProperty(str, notify=workspace_changed)
    def workspace_name(self) -> str:
        return Path(self._workspace_file).stem if self._workspace_file else ''

    @pyqtProperty('QStringList', constant=True)
    def workspace_filters(self) -> List[str]:
        return [f'CANExplorer workspaces (*{WORKSPACE_SUFFIX})']

    @pyqtSlot(QUrl, int, result=str)
    def save_workspace(self, url: QUrl, current: int = -1) -> str:
        """Save the connections and their setup; `current` is the selected connection. Returns an error, or ''."""
        path = _local_path(url)
        if not path.suffix:
            path = path.with_suffix(WORKSPACE_SUFFIX)
        try:
            save_workspace(self, path, current)
        except OSError as e:
            return f'Could not save the workspace: {e}'
        self._workspace_file = str(path)
        self._app_state.add_recent_workspace(str(path))
        self.workspace_changed.emit()
        return ''

    @pyqtSlot(int, result=str)
    def save_current_workspace(self, current: int) -> str:
        """Save to the workspace file opened or saved last."""
        if not self._workspace_file:
            return 'No workspace file yet; use "Save Workspace As"'
        return self.save_workspace(QUrl.fromLocalFile(self._workspace_file), current)

    @pyqtSlot(QUrl, result=str)
    def open_workspace(self, url: QUrl) -> str:
        """Replace the connections with those of a workspace. Returns problems, one per line, or ''."""
        path = _local_path(url)
        try:
            problems = open_workspace(self, path)
        except WorkspaceError as e:
            return str(e)
        self._workspace_file = str(path)
        self._app_state.add_recent_workspace(str(path))
        self.workspace_changed.emit()
        return '\n'.join(problems)

    @pyqtSlot(int, result=str)
    def open_recent_workspace(self, index: int) -> str:
        recent = self._app_state.recent_workspaces
        if not 0 <= index < len(recent):
            return ''
        return self.open_workspace(QUrl.fromLocalFile(recent[index]['path']))

    @pyqtSlot(result=int)
    def workspace_current(self) -> int:
        """The connection selected when the workspace was saved (read after opening it)."""
        try:
            return int(json.loads(Path(self._workspace_file).read_text(encoding='utf-8')).get('current', -1))
        except (OSError, ValueError):
            return -1

    @pyqtProperty(QObject, constant=True)
    def database_editor(self) -> DatabaseEditorController:
        return self._database_editor

    @pyqtProperty(QObject, constant=True)
    def compare(self) -> CompareController:
        """Comparing two recordings."""
        return self._compare

    def _reload_database(self, path: str) -> None:
        """Connections decoding with a database that was edited and saved use the new version."""
        saved = Path(path).resolve()
        for connection in self._connections:
            if connection.database_file and Path(connection.database_file).resolve() == saved:
                connection.load_database(QUrl.fromLocalFile(str(saved)))

    @pyqtProperty(QObject, constant=True)
    def app_state(self) -> AppState:
        return self._app_state

    @pyqtProperty('QVariantList', notify=connections_changed)
    def connections(self) -> List[CanConnection]:
        return list(self._connections)

    @pyqtProperty('QStringList', constant=True)
    def interfaces(self) -> List[str]:
        return sorted(description for _, description in canutils.get_supported_interfaces())

    @pyqtProperty('QStringList', constant=True)
    def bitrates(self) -> List[str]:
        return [str(bitrate) for bitrate in canutils.CLASSIC_CAN_BITRATES]

    @pyqtProperty('QStringList', constant=True)
    def data_bitrates(self) -> List[str]:
        return [str(bitrate) for bitrate in FD_DATA_BITRATES]

    @pyqtProperty('QStringList', constant=True)
    def lin_baudrates(self) -> List[str]:
        return [str(baudrate) for baudrate in LIN_BAUDRATES]

    @pyqtProperty('QStringList', constant=True)
    def protocols(self) -> List[str]:
        return list(PROTOCOL_DECODERS)

    @pyqtProperty('QStringList', constant=True)
    def log_file_filters(self) -> List[str]:
        return file_filters()

    @pyqtProperty('QStringList', constant=True)
    def database_file_filters(self) -> List[str]:
        suffixes = DATABASE_SUFFIXES + DESCRIPTION_SUFFIXES
        return [
            f'Signal databases and diagnostic descriptions ({" ".join(f"*{suffix}" for suffix in suffixes)})',
            *(f'{name} (*{suffix})' for suffix, name in DATABASE_FORMAT_NAMES.items()),
            'All files (*)',
        ]

    @pyqtProperty('QStringList', constant=True)
    def database_output_filters(self) -> List[str]:
        return [f'{DATABASE_FORMAT_NAMES[suffix]} (*{suffix})' for suffix in WRITE_SUFFIXES]

    @pyqtSlot(QUrl, QUrl, result=str)
    def convert_database(self, source: QUrl, destination: QUrl) -> str:
        """Convert a signal database, e.g. DBC <-> DBF. Returns a summary, or '' after reporting an error."""
        try:
            report = convert_database(_local_path(source), _local_path(destination))
        except Exception as e:
            self.error_occurred.emit(f'Could not convert {_local_path(source).name}: {e}')
            return ''
        logger.info(report.summary())
        return report.summary()

    # Channel detection

    @pyqtProperty('QVariantMap', notify=detected_channels_changed)
    def detected_channels(self) -> Dict[str, List[str]]:
        """Detected channels per interface description."""
        return dict(self._detected_channels)

    @pyqtProperty(bool, notify=detected_channels_changed)
    def detecting(self) -> bool:
        return self._detecting

    @pyqtSlot()
    def detect_channels(self):
        """Probe all interfaces for channels in a worker thread (some backends take seconds)."""
        if self._detecting:
            return
        self._detecting = True
        self.detected_channels_changed.emit()
        self._detection = asyncio.ensure_future(self._detect_channels())

    @staticmethod
    def _probe_interfaces() -> List[Dict[str, Any]]:
        # Probing every backend logs a warning for each missing driver; that is expected here
        can_logger = logging.getLogger('can')
        level = can_logger.level
        can_logger.setLevel(logging.ERROR)
        try:
            return can.detect_available_configs()
        finally:
            can_logger.setLevel(level)

    async def _detect_channels(self):
        try:
            configs = await asyncio.to_thread(self._probe_interfaces)
        except Exception as e:
            logger.warning(f'Channel detection failed: {e}')
            configs = []
        descriptions = dict(canutils.get_supported_interfaces())
        detected: Dict[str, List[str]] = {}
        for config in configs:
            interface = config.get('interface')
            channel = config.get('channel')
            if interface is None or channel is None:
                continue
            channels = detected.setdefault(descriptions.get(interface, interface), [])
            if str(channel) not in channels:
                channels.append(str(channel))
        logger.info(f'Detected channels: {detected}')
        self._detected_channels = detected
        self._detecting = False
        self.detected_channels_changed.emit()

    # Connections

    def _add(self, name: str, source: MessageSource, protocol: str) -> CanConnection:
        decoder = FrameDecoder(protocol=create_protocol_decoder(protocol))
        state = self._app_state
        session = BusSession(
            source,
            decoder,
            buffer_size=state.max_trace_rows,
            history_size=state.trace_history,
            history_directory=Path(state.trace_directory),
        )
        connection = CanConnection(
            name, session, protocol, state.max_trace_rows, state.refresh_interval_ms, parent=self
        )
        connection.error_occurred.connect(self.error_occurred)
        self._connections.append(connection)
        self.connections_changed.emit()
        return connection

    @pyqtSlot('QVariantMap')
    def add_connection(self, options: Dict[str, Any]):
        """Options: name, interface (description), channel, bus ('CAN' or 'LIN'), protocol, receive_own_messages;
        for CAN bitrate, fd, data_bitrate; for LIN lin_baudrate."""
        try:
            bus_type = str(options.get('bus', 'CAN')).upper()
            if bus_type not in ('CAN', 'LIN'):
                raise ValueError(f'Unknown bus type {bus_type!r}')
            lin = bus_type == 'LIN'
            fd = bool(options.get('fd', False)) and not lin
            data_bitrate = str(options.get('data_bitrate') or '')
            config = BusConfig(
                interface=canutils.get_interface_name(options['interface']) or options['interface'],
                channel=str(options['channel']),
                bitrate=int(options.get('bitrate', 500_000)),
                fd=fd,
                data_bitrate=int(data_bitrate) if fd and data_bitrate else None,
                receive_own_messages=bool(options.get('receive_own_messages', False)),
                bus_type=bus_type,
                lin_baudrate=int(options.get('lin_baudrate') or DEFAULT_BAUDRATE),
            )
            protocol = options.get('protocol') or ('LIN' if lin else 'Raw')
            create_protocol_decoder(protocol)
        except (KeyError, ValueError) as e:
            self.error_occurred.emit(f'Invalid connection settings: {e}')
            return
        name = options.get('name') or f'Connection {len(self._connections) + 1}'
        logger.info(f'Adding new connection {name!r}: {config=}')
        settings = {
            'name': name,
            'interface': options['interface'],
            'channel': config.channel,
            'bus': bus_type,
            'bitrate': config.bitrate,
            'data_bitrate': config.data_bitrate or '',
            'lin_baudrate': config.lin_baudrate,
            'protocol': protocol,
            'fd': config.fd,
            'receive_own_messages': config.receive_own_messages,
        }
        connection = self._add(name, LiveSource(config), protocol)
        connection.origin = {'kind': 'bus', 'settings': settings}
        self._app_state.add_recent_connection(settings)

    @pyqtSlot(int)
    def open_recent_connection(self, index: int):
        recent = self._app_state.recent_connections
        if 0 <= index < len(recent):
            self.add_connection(recent[index])

    @pyqtSlot(QUrl, str, float)
    def open_log_file(self, url: QUrl, protocol: str, speed: float):
        """Replay a log file into a new connection at `speed` times the recorded timing (0: as fast as possible)."""
        path = _local_path(url)
        connection = self._add(path.name, FileSource(path, speed=speed), protocol)
        connection.origin = {'kind': 'replay', 'path': str(path), 'protocol': protocol, 'speed': speed}
        self._app_state.add_recent_log_file(str(path), protocol, speed)
        connection.open()

    @pyqtSlot(int)
    def open_recent_log_file(self, index: int):
        recent = self._app_state.recent_log_files
        if 0 <= index < len(recent):
            entry = recent[index]
            # Entries written before the speed was introduced only tell whether the timing was reproduced
            speed = entry.get('speed', 1.0 if entry.get('realtime', True) else 0.0)
            self.open_log_file(QUrl.fromLocalFile(entry['path']), entry.get('protocol', 'Raw'), float(speed))

    @pyqtSlot(int)
    def remove_connection(self, index: int):
        if not 0 <= index < len(self._connections):
            return
        connection = self._connections.pop(index)
        connection.close()
        self.connections_changed.emit()
        connection.deleteLater()

    @pyqtSlot()
    def close_all(self):
        if self._detection is not None and not self._detection.done():
            self._detection.cancel()
        for connection in self._connections:
            connection.close()

    def _apply_preferences(self):
        for connection in self._connections:
            connection.set_refresh_interval(self._app_state.refresh_interval_ms)
