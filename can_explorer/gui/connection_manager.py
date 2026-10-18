from pathlib import Path
from typing import List, Optional

import can
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QGuiApplication

from can_explorer.core.decoding import DATABASE_SUFFIXES, DbcDecoder, FrameDecoder
from can_explorer.core.filters import MessageFilter
from can_explorer.core.frames import parse_can_id, parse_hex_bytes
from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder
from can_explorer.core.session import BusSession, SessionError, SessionState
from can_explorer.core.sources import LOG_FILE_SUFFIXES, BusConfig, FileSource, LiveSource, MessageSource
from can_explorer.gui.models import OverviewModel, TraceModel
from can_explorer.sys.log_handler import get_logger
from can_explorer.util import canutils

logger = get_logger(__name__)

# How often received frames are pushed to the views. Batching keeps the GUI responsive on busy buses.
REFRESH_INTERVAL_MS = 33
MAX_TRACE_ROWS = 100_000


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


class CanConnection(QObject):
    """Qt adapter around a BusSession: exposes its state and models to QML."""

    state_changed = pyqtSignal()
    statistics_changed = pyqtSignal()
    recording_changed = pyqtSignal()
    database_changed = pyqtSignal()
    filter_changed = pyqtSignal()
    paused_changed = pyqtSignal()
    periodic_changed = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, name: str, session: BusSession, protocol: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._name = name
        self._protocol = protocol
        self.session = session
        self._trace = TraceModel(lambda: self.session.start_time, MAX_TRACE_ROWS, parent=self)
        self._overview = OverviewModel(session.statistics, parent=self)
        self._filter = MessageFilter()
        self._filter_error = ''
        self._paused = False
        self._frame_count = 0
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)
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

    # Session control

    @pyqtSlot()
    def open(self):
        try:
            self.session.start()
        except SessionError as e:
            self.error_occurred.emit(str(e))

    @pyqtSlot()
    def close(self):
        self.session.stop()

    def _on_session_state(self, session: BusSession):
        if session.is_open:
            self._timer.start()
        else:
            self._timer.stop()
            self._refresh()
        if session.state is SessionState.ERROR and session.error:
            self.error_occurred.emit(session.error)
        self.state_changed.emit()
        self.recording_changed.emit()

    def _on_session_error(self, message: str):
        self.error_occurred.emit(message)
        self.recording_changed.emit()

    def _refresh(self):
        frames = self.session.trace.drain()
        added, changed = self.session.statistics.drain_changes()
        if not self._paused:
            self._trace.append_frames(frames)
            self._overview.refresh(added, changed)
        if self._frame_count != self.session.trace.total:
            self._frame_count = self.session.trace.total
            self.statistics_changed.emit()

    @pyqtSlot()
    def clear(self):
        self.session.clear()
        self._trace.rebuild([])
        self._overview.rebuild()
        self._frame_count = 0
        self.statistics_changed.emit()

    @pyqtSlot(bool)
    def set_paused(self, paused: bool):
        if paused == self._paused:
            return
        self._paused = paused
        if not paused:
            # Catch up with everything received while the view was frozen
            self._trace.rebuild(self.session.trace)
            self._overview.rebuild()
        self.paused_changed.emit()

    @pyqtSlot(str)
    def set_filter(self, text: str):
        try:
            message_filter = MessageFilter.parse(text)
        except ValueError as e:
            self._filter_error = str(e)
        else:
            self._filter, self._filter_error = message_filter, ''
            self._trace.set_filter(message_filter, self.session.trace)
            self._overview.set_filter(message_filter)
        self.filter_changed.emit()

    # Recording and databases

    @pyqtSlot(QUrl)
    def start_recording(self, url: QUrl):
        try:
            self.session.start_recording(_local_path(url))
        except SessionError as e:
            self.error_occurred.emit(str(e))
        self.recording_changed.emit()

    @pyqtSlot()
    def stop_recording(self):
        self.session.stop_recording()
        self.recording_changed.emit()

    @pyqtSlot(QUrl)
    def load_database(self, url: QUrl):
        path = _local_path(url)
        try:
            self.session.decoder.database = DbcDecoder(path)
        except Exception as e:
            self.error_occurred.emit(f'Could not load {path.name}: {e}')
            return
        logger.info(f'Loaded signal database {path}')
        self.database_changed.emit()

    @pyqtSlot()
    def unload_database(self):
        self.session.decoder.database = None
        self.database_changed.emit()

    # Transmission

    @pyqtSlot(str, str, bool, bool, int, result=str)
    def send(self, can_id: str, data: str, extended: bool, fd: bool, period_ms: int) -> str:
        """Send once (period_ms == 0) or periodically. Returns an error message, or '' on success."""
        try:
            payload = parse_hex_bytes(data)
            max_length = 64 if fd else 8
            if len(payload) > max_length:
                raise ValueError(f'At most {max_length} data bytes allowed')
            msg = can.Message(
                arbitration_id=parse_can_id(can_id, extended),
                is_extended_id=extended,
                is_fd=fd,
                data=payload,
            )
            if period_ms > 0:
                self.session.start_periodic(msg, period_ms / 1000)
            else:
                self.session.send(msg)
        except (ValueError, SessionError) as e:
            return str(e)
        return ''

    @pyqtSlot()
    def stop_periodic(self):
        self.session.stop_periodic()


class ConnectionManager(QObject):
    connections_changed = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connections: List[CanConnection] = []
        QGuiApplication.instance().aboutToQuit.connect(self.close_all)

    @pyqtProperty('QVariantList', notify=connections_changed)
    def connections(self) -> List[CanConnection]:
        return list(self._connections)

    @pyqtProperty('QStringList', constant=True)
    def interfaces(self) -> List[str]:
        return sorted(description for _, description in canutils.get_supported_interfaces())

    @pyqtProperty('QStringList', constant=True)
    def bitrates(self) -> List[str]:
        return [str(bitrate) for bitrate in canutils.SupportedProtocols.IsoCAN.supported_bitrates]

    @pyqtProperty('QStringList', constant=True)
    def protocols(self) -> List[str]:
        return list(PROTOCOL_DECODERS)

    @pyqtProperty('QStringList', constant=True)
    def log_file_filters(self) -> List[str]:
        patterns = ' '.join(f'*{suffix}' for suffix in LOG_FILE_SUFFIXES)
        return [f'CAN log files ({patterns})', *(f'{suffix[1:].upper()} (*{suffix})' for suffix in LOG_FILE_SUFFIXES)]

    @pyqtProperty('QStringList', constant=True)
    def database_file_filters(self) -> List[str]:
        return [f'Signal databases ({" ".join(f"*{suffix}" for suffix in DATABASE_SUFFIXES)})', 'All files (*)']

    @pyqtSlot(result='QStringList')
    def available_channels(self) -> List[str]:
        # TODO: Extend of proper interfaces
        return [config['channel'] for config in canutils.get_available_channels(['socketcan'])]

    def _add(self, name: str, source: MessageSource, protocol: str) -> CanConnection:
        decoder = FrameDecoder(protocol=create_protocol_decoder(protocol))
        connection = CanConnection(name, BusSession(source, decoder), protocol, parent=self)
        connection.error_occurred.connect(self.error_occurred)
        self._connections.append(connection)
        self.connections_changed.emit()
        return connection

    @pyqtSlot(str, str, str, str, str, bool, bool)
    def add_connection(
        self,
        name: str,
        interface_description: str,
        channel: str,
        bitrate: str,
        protocol: str,
        fd: bool,
        receive_own_messages: bool,
    ):
        config = BusConfig(
            interface=canutils.get_interface_name(interface_description) or interface_description,
            channel=channel,
            bitrate=int(bitrate, base=10),
            fd=fd,
            receive_own_messages=receive_own_messages,
        )
        logger.info(f'Adding new connection {name!r}: {config=}')
        self._add(name, LiveSource(config), protocol)

    @pyqtSlot(QUrl, str, bool)
    def open_log_file(self, url: QUrl, protocol: str, realtime: bool):
        path = _local_path(url)
        connection = self._add(path.name, FileSource(path, realtime=realtime), protocol)
        connection.open()

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
        for connection in self._connections:
            connection.close()
