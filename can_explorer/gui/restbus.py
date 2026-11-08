"""The restbus-simulation view of a connection (QML): transmit the connection's database messages periodically at
their cycle times, so the ECU under test sees the rest of the bus. Per connection; it rebuilds when the database
is loaded, changed or unloaded."""

from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.decoding import DbcDecoder
from can_explorer.core.restbus import DEFAULT_CYCLE_MS, RestbusSimulator
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)


class RestbusController(QObject):
    """Drives a `RestbusSimulator` on one connection's session and database."""

    changed = pyqtSignal()  # Running / connected state, default cycle
    messages_changed = pyqtSignal()  # The message table (database loaded, toggles, values)

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._default_cycle_ms = DEFAULT_CYCLE_MS
        self._sim: Optional[RestbusSimulator] = None
        self._build()
        connection.state_changed.connect(self.changed)  # Connect/disconnect affects whether it can start

    # Database

    def _database(self) -> Optional[DbcDecoder]:
        decoder = self._connection.session.decoder
        database = getattr(decoder, 'database', None)  # None, a DbcDecoder, or an LdfDecoder (LIN)
        return database if isinstance(database, DbcDecoder) else None

    def _build(self) -> None:
        if self._sim is not None:
            self._sim.stop()
        database = self._database()
        messages = database.messages() if database is not None else []
        self._sim = RestbusSimulator(self._connection.session, messages, self._default_cycle_ms) if messages else None

    def database_changed(self) -> None:
        """Rebuild from the connection's database (called when it is loaded, its bus changes, or it is unloaded)."""
        self._build()
        self.changed.emit()
        self.messages_changed.emit()

    def _entry(self, name: str):
        if self._sim is None:
            return None
        return next((entry for entry in self._sim.messages() if entry.message.name == name), None)

    # State

    @pyqtProperty(bool, notify=messages_changed)
    def has_database(self) -> bool:
        return self._sim is not None

    @pyqtProperty(bool, notify=changed)
    def running(self) -> bool:
        return self._sim is not None and self._sim.running

    @pyqtProperty(bool, notify=changed)
    def can_start(self) -> bool:
        return self._sim is not None and self._connection.connected and self._connection.can_transmit

    @pyqtProperty(int, notify=messages_changed)
    def message_count(self) -> int:
        return len(self._sim.messages()) if self._sim is not None else 0

    @pyqtProperty(int, notify=messages_changed)
    def enabled_count(self) -> int:
        return self._sim.enabled_count if self._sim is not None else 0

    @pyqtProperty(int, notify=changed)
    def default_cycle_ms(self) -> int:
        return self._default_cycle_ms

    @pyqtSlot(int)
    def set_default_cycle(self, milliseconds: int) -> None:
        milliseconds = max(1, int(milliseconds))
        if milliseconds == self._default_cycle_ms:
            return
        self._default_cycle_ms = milliseconds
        if self._sim is not None:
            self._sim.set_default_period(milliseconds / 1000.0)
        self.changed.emit()
        self.messages_changed.emit()

    @pyqtProperty('QStringList', notify=messages_changed)
    def nodes(self) -> List[str]:
        if self._sim is None:
            return []
        return sorted({node for entry in self._sim.messages() for node in entry.senders})

    @pyqtProperty('QVariantList', notify=messages_changed)
    def messages(self) -> List[Dict[str, Any]]:
        if self._sim is None:
            return []
        rows = []
        for entry in self._sim.messages():
            message = entry.message
            rows.append(
                {
                    'name': message.name,
                    'id': f'{message.frame_id:X}',
                    'extended': message.is_extended_frame,
                    'senders': ', '.join(entry.senders),
                    'cycle_ms': message.cycle_time if entry.has_cycle_time else 0,
                    'length': message.length,
                    'signal_count': len(message.signals),
                    'enabled': entry.enabled,
                }
            )
        return rows

    @pyqtSlot(str, result='QVariantList')
    def message_signals(self, name: str) -> List[Dict[str, Any]]:
        """The editable signals of a message: name, current physical value, unit and range."""
        entry = self._entry(name)
        if entry is None:
            return []
        rows = []
        for signal in entry.message.signals:
            value = entry.values.get(signal.name)
            rows.append(
                {
                    'name': signal.name,
                    'value': '' if value is None else f'{value:g}' if isinstance(value, float) else str(value),
                    'unit': signal.unit or '',
                    'minimum': '' if signal.minimum is None else str(signal.minimum),
                    'maximum': '' if signal.maximum is None else str(signal.maximum),
                }
            )
        return rows

    # Actions

    @pyqtSlot(str, bool)
    def set_enabled(self, name: str, enabled: bool) -> None:
        entry = self._entry(name)
        if entry is not None:
            self._sim.set_enabled(entry.key, enabled)
            self.changed.emit()
            self.messages_changed.emit()

    @pyqtSlot(bool)
    def set_all(self, enabled: bool) -> None:
        if self._sim is not None:
            self._sim.set_all(enabled)
            self.changed.emit()
            self.messages_changed.emit()

    @pyqtSlot()
    def enable_cyclic_only(self) -> None:
        if self._sim is not None:
            self._sim.enable_cyclic_only()
            self.changed.emit()
            self.messages_changed.emit()

    @pyqtSlot(str, result=int)
    def exclude_node(self, node: str) -> int:
        if self._sim is None:
            return 0
        count = self._sim.exclude_sender(node)
        self.changed.emit()
        self.messages_changed.emit()
        return count

    @pyqtSlot(str, str, str)
    def set_value(self, name: str, signal: str, value: str) -> None:
        entry = self._entry(name)
        if entry is None:
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        parsed = int(number) if number.is_integer() else number
        self._sim.set_value(entry.key, signal, parsed)
        self.messages_changed.emit()

    @pyqtSlot(result=str)
    def start(self) -> str:
        """Start transmitting. Returns an error message, or ''."""
        if self._sim is None:
            return 'This connection has no database to simulate'
        if not self._connection.connected:
            return 'Connect to the bus first'
        if not self._connection.can_transmit:
            return 'This connection cannot transmit'
        if self._sim.enabled_count == 0:
            return 'No messages are enabled'
        self._sim.start()
        self.changed.emit()
        return ''

    @pyqtSlot()
    def stop(self) -> None:
        if self._sim is not None:
            self._sim.stop()
        self.changed.emit()

    def shutdown(self) -> None:
        if self._sim is not None:
            self._sim.stop()
