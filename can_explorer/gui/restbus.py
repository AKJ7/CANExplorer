"""The restbus-simulation view of a connection (QML): transmit the connection's database messages the way their
nodes would, so the ECU under test sees the rest of the bus. Per connection; it rebuilds when the database is
loaded, changed or unloaded, keeping its configuration for the messages the new database still has."""

from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.decoding import DbcDecoder
from can_explorer.core.restbus import (
    CHECKSUM,
    CHECKSUMS,
    DEFAULT_CYCLE_MS,
    IF_ACTIVE,
    MODES,
    VALUE,
    RestbusSimulator,
    SignalRole,
    whole_byte,
)
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MODE_NAMES = {
    'cyclic': 'Cyclic',
    'on_change': 'On change',
    'cyclic_on_change': 'Cyclic + on change',
    'if_active': 'If active',
}


def _format(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


class RestbusController(QObject):
    """Drives a `RestbusSimulator` on one connection's session and database."""

    changed = pyqtSignal()  # Running / connected state, default cycle, timing, expected load
    messages_changed = pyqtSignal()  # The message table (database loaded, toggles, values, states)
    # The list of messages and their signals: only when the database changes. The view's lists use these as their
    # models and read each row through `revision`, so an edit refreshes the rows in place instead of rebuilding the
    # list (which would collapse the expanded rows and destroy the delegate whose handler made the edit).
    names_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._default_cycle_ms = DEFAULT_CYCLE_MS
        self._raw_values = False
        self._revision = 0
        self._sim: Optional[RestbusSimulator] = None
        self._build()
        connection.state_changed.connect(self.changed)  # Connect/disconnect affects whether it can start
        periodic_changed = getattr(connection, 'periodic_changed', None)
        if periodic_changed is not None:  # A transmission ended (e.g. a send error): show it as stopped
            periodic_changed.connect(self._on_periodic_changed)

    def _emit_messages(self) -> None:
        self._revision += 1  # Before the signal: the rows read through `revision` re-evaluate on it
        self.messages_changed.emit()

    def _on_periodic_changed(self) -> None:
        if self._sim is not None and self._sim.running:
            self._emit_messages()

    # Database

    def _database(self) -> Optional[DbcDecoder]:
        decoder = self._connection.session.decoder
        database = getattr(decoder, 'database', None)  # None, a DbcDecoder, or an LdfDecoder (LIN)
        return database if isinstance(database, DbcDecoder) else None

    def _bitrates(self):
        bitrates = getattr(self._connection.session.source, 'bitrates', None)
        return bitrates if isinstance(bitrates, tuple) else None

    def _build(self) -> None:
        config: Optional[Dict[str, Any]] = None
        was_running = False
        if self._sim is not None:
            config = self._sim.to_dict()
            was_running = self._sim.running
            self._sim.stop()
        database = self._database()
        messages = database.messages() if database is not None else []
        if not messages:
            self._sim = None
            return
        dbc = getattr(database.database, 'dbc', None)
        definitions = getattr(dbc, 'attribute_definitions', None) or {}
        defaults = {name: definition.default_value for name, definition in definitions.items()}
        bitrates = self._bitrates()
        self._sim = RestbusSimulator(
            self._connection.session,
            messages,
            self._default_cycle_ms,
            self._connection.session.decoder.secure,
            attribute_defaults=defaults,
            bitrate_switch=bool(bitrates and bitrates[1]),  # FD messages switch bitrate when there is a data rate
        )
        if config is not None:
            problems = self._sim.apply(config)  # The same choices, for the messages the new database still has
            for problem in problems:
                logger.debug('Restbus: %s', problem)
        if was_running and self._connection.connected and self._connection.can_transmit:
            self._sim.start()

    def database_changed(self) -> None:
        """Rebuild from the connection's database (called when it is loaded, its bus changes, or it is unloaded)."""
        self._build()
        self.names_changed.emit()
        self.changed.emit()
        self._emit_messages()

    def _entry(self, name: str):
        return self._sim.by_name(name) if self._sim is not None else None

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

    @pyqtProperty(int, notify=messages_changed)
    def problem_count(self) -> int:
        return len(self._sim.problems) if self._sim is not None and self._sim.running else 0

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
        self._emit_messages()

    @pyqtProperty(bool, notify=changed)
    def hardware_timing(self) -> bool:
        return self._sim is not None and self._sim.hardware

    @pyqtSlot(bool)
    def set_hardware_timing(self, hardware: bool) -> None:
        if self._sim is not None:
            self._sim.set_hardware(hardware)
            self.changed.emit()
            self._emit_messages()

    @pyqtProperty(bool, notify=changed)
    def raw_values(self) -> bool:
        """Signal values are shown and entered as raw (unscaled) numbers."""
        return self._raw_values

    @pyqtSlot(bool)
    def set_raw_values(self, raw: bool) -> None:
        if raw != self._raw_values:
            self._raw_values = raw
            self.changed.emit()
            self._emit_messages()

    @pyqtProperty(float, notify=messages_changed)
    def expected_load(self) -> float:
        """The bus load of the enabled periodic messages in percent (without bit stuffing); -1 if unknown."""
        bitrates = self._bitrates()
        if self._sim is None or not bitrates:
            return -1.0
        load = self._sim.expected_load(bitrates[0], bitrates[1])
        return -1.0 if load is None else load * 100.0

    @pyqtProperty('QVariantList', constant=True)
    def modes(self) -> List[Dict[str, str]]:
        return [{'mode': mode, 'name': MODE_NAMES[mode]} for mode in MODES]

    @pyqtProperty('QVariantList', constant=True)
    def checksum_algorithms(self) -> List[Dict[str, str]]:
        return [{'algorithm': key, 'name': algorithm.name} for key, algorithm in CHECKSUMS.items()]

    @pyqtProperty('QStringList', notify=messages_changed)
    def nodes(self) -> List[str]:
        if self._sim is None:
            return []
        return sorted({node for entry in self._sim.messages() for node in entry.senders})

    @pyqtProperty(int, notify=messages_changed)
    def revision(self) -> int:
        """Changes with every change of the table: the view's rows read their data through it."""
        return self._revision

    @pyqtProperty('QStringList', notify=names_changed)
    def message_names(self) -> List[str]:
        """The messages, in table order: the model of the view's list (changes only with the database)."""
        return [entry.name for entry in self._sim.messages()] if self._sim is not None else []

    @pyqtProperty('QVariantList', notify=messages_changed)
    def messages(self) -> List[Dict[str, Any]]:
        if self._sim is None:
            return []
        return [self._message_row(entry) for entry in self._sim.messages()]

    @pyqtSlot(str, result='QVariantMap')
    def message_row(self, name: str) -> Dict[str, Any]:
        entry = self._entry(name)
        return self._message_row(entry) if entry is not None else {}

    def _message_row(self, entry) -> Dict[str, Any]:
        message = entry.message
        return {
            'name': message.name,
            'id': f'{message.frame_id:X}',
            'extended': message.is_extended_frame,
            'senders': ', '.join(entry.senders),
            'cycle_ms': message.cycle_time if entry.has_cycle_time else 0,
            'period_ms': round((entry.active_period if entry.mode == IF_ACTIVE else entry.period) * 1000),
            'length': message.length,
            'signal_count': len(message.signals),
            'enabled': entry.enabled,
            'mode': entry.mode,
            'mode_name': MODE_NAMES[entry.mode],
            'database_mode': entry.database_mode,
            'sent_by_nodes': entry.sent_by_nodes,
            'state': self._sim.state_of(entry),
            'hardware_timed': entry.hardware_timed,
            'secured': entry.secured,
            'e2e': entry.e2e.label if entry.e2e is not None else '',
            'fd': entry.is_fd,
            'brs': entry.bitrate_switch,
            'pages': list(entry.pages),
            'selected_pages': list(entry.selected_pages),
            'computed': sorted(name for name, role in entry.roles.items() if role.role != VALUE),
            'problem': self._sim.problem_of(entry),
            'note': entry.note,
        }

    @pyqtSlot(str, result='QStringList')
    def signal_names(self, name: str) -> List[str]:
        """The editable signals of a message (not the SecOC freshness and authenticator, computed for each
        frame): the model of the view's signal editors."""
        entry = self._entry(name)
        if entry is None:
            return []
        return [signal.name for signal in entry.message.signals if signal.name not in entry.computed_signals]

    @pyqtSlot(str, result='QVariantList')
    def message_signals(self, name: str) -> List[Dict[str, Any]]:
        """The editable signals of a message, as `signal_row` describes them."""
        return [self.signal_row(name, signal) for signal in self.signal_names(name)]

    @pyqtSlot(str, str, result='QVariantMap')
    def signal_row(self, name: str, signal_name: str) -> Dict[str, Any]:
        """A signal: its current value (physical, or raw), unit, range, value table, role (value, counter,
        checksum), and whether it is computed for every frame (the rotated multiplexer) instead of edited."""
        entry = self._entry(name)
        if entry is None:
            return {}
        try:
            signal = entry.message.get_signal_by_name(signal_name)
        except KeyError:
            return {}
        value = entry.values.get(signal.name)
        if self._raw_values and not isinstance(value, str):
            try:
                value = signal.conversion.scaled_to_raw(value)
            except (TypeError, ValueError, ArithmeticError):
                pass
        role = entry.roles.get(signal.name, SignalRole())
        rotated = signal is entry.multiplexer and len(entry.selected_pages) > 1
        return {
            'name': signal.name,
            'value': _format(value),
            'unit': '' if self._raw_values else (signal.unit or ''),
            'minimum': _format(signal.minimum),
            'maximum': _format(signal.maximum),
            'choices': [str(choice) for choice in (signal.choices or {}).values()],
            'role': role.role,
            'algorithm': role.algorithm,
            'constant': role.constant,
            'can_checksum': whole_byte(signal) is not None,
            'can_count': not signal.is_multiplexer,
            'page': ', '.join(str(page) for page in (signal.multiplexer_ids or [])),
            'computed': 'rotated through the selected pages' if rotated else '',
        }

    # Actions

    def _changed_messages(self) -> None:
        self.changed.emit()
        self._emit_messages()

    @pyqtSlot(str, bool)
    def set_enabled(self, name: str, enabled: bool) -> None:
        entry = self._entry(name)
        if entry is not None:
            self._sim.set_enabled(entry.key, enabled)
            self._changed_messages()

    @pyqtSlot(bool)
    def set_all(self, enabled: bool) -> None:
        if self._sim is not None:
            self._sim.set_all(enabled)
            self._changed_messages()

    @pyqtSlot()
    def enable_cyclic_only(self) -> None:
        if self._sim is not None:
            self._sim.enable_cyclic_only()
            self._changed_messages()

    @pyqtSlot(str, result=int)
    def exclude_node(self, node: str) -> int:
        if self._sim is None:
            return 0
        count = self._sim.exclude_sender(node)
        self._changed_messages()
        return count

    @pyqtSlot(str, str)
    def set_mode(self, name: str, mode: str) -> None:
        entry = self._entry(name)
        if entry is not None:
            self._sim.set_mode(entry.key, mode)
            self._changed_messages()

    @pyqtSlot(str, str, str)
    def set_value(self, name: str, signal: str, value: str) -> None:
        """Set a signal from text: a choice name of its value table, or a number (raw when raw_values is on)."""
        entry = self._entry(name)
        if entry is None:
            return
        try:
            definition = entry.message.get_signal_by_name(signal)
        except KeyError:
            return
        text = value.strip()
        choices = [str(choice) for choice in (definition.choices or {}).values()]
        if text in choices:
            parsed: Any = text
        else:
            try:
                number = float(int(text, 0)) if text.lower().startswith(('0x', '0b')) else float(text)
            except (TypeError, ValueError):
                return
            if self._raw_values:
                number = definition.conversion.raw_to_scaled(number, decode_choices=False)
            parsed = int(number) if float(number).is_integer() else number
        self._sim.set_value(entry.key, signal, parsed)
        self._emit_messages()

    @pyqtSlot(str, str, str, str, str, result=str)
    def set_role(self, name: str, signal: str, role: str, algorithm: str = '', constant: str = '0') -> str:
        """Make a signal a value, a counter or a checksum (algorithm and constant, e.g. "0x5A"). Returns an error,
        or ''."""
        entry = self._entry(name)
        if entry is None:
            return 'unknown message'
        try:
            number = int(constant or '0', 0) & 0xFF
        except ValueError:
            return f'The constant {constant!r} is not a number'
        current = entry.roles.get(signal, SignalRole())
        spec = SignalRole(role, algorithm or current.algorithm, number if role == CHECKSUM else 0)
        error = self._sim.set_role(entry.key, signal, spec)
        self._emit_messages()
        return error

    @pyqtSlot(str, 'QVariantList')
    def set_pages(self, name: str, pages: List[Any]) -> None:
        entry = self._entry(name)
        if entry is not None:
            self._sim.set_pages(entry.key, [int(page) for page in pages])
            self._emit_messages()

    @pyqtSlot(str, result=str)
    def send_now(self, name: str) -> str:
        """Send a message once at once (with its repetitions). Returns an error, or ''."""
        entry = self._entry(name)
        if entry is None:
            return 'unknown message'
        if not self._connection.connected or not self._connection.can_transmit:
            return 'Connect to a bus that can transmit first'
        return self._sim.send_now(entry.key)

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
        problems = self._sim.start()
        self._changed_messages()
        return '\n'.join(problems)

    @pyqtSlot()
    def stop(self) -> None:
        if self._sim is not None:
            self._sim.stop()
        self._changed_messages()

    def shutdown(self) -> None:
        if self._sim is not None:
            self._sim.stop()

    # Workspace

    def to_dict(self) -> Dict[str, Any]:
        """The configuration to save in a workspace: what differs from the database, and whether it runs."""
        if self._sim is None:
            return {}
        return {**self._sim.to_dict(), 'running': self._sim.running}

    def apply(self, config: Dict[str, Any]) -> List[str]:
        """Restore a configuration from `to_dict` (after the database is loaded; started separately). Returns
        problems."""
        if not config:
            return []
        if self._sim is None:
            return ['restbus: no database to apply the restbus configuration to']
        if config.get('default_cycle_ms'):
            self._default_cycle_ms = max(1, int(config['default_cycle_ms']))
        problems = self._sim.apply(config)
        self._changed_messages()
        return problems
