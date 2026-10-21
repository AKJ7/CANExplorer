from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.filters import MessageFilter
from can_explorer.core.gateway import Gateway, GatewayRule
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

REFRESH_MS = 300  # Live counters


def _parse_optional_id(text: Any, what: str) -> Optional[int]:
    text = str(text or '').strip()
    if not text:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= 0x1FFFFFFF:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


def _parse_offset(text: Any) -> int:
    text = str(text or '').strip()
    if not text:
        return 0
    negative = text.startswith('-')
    try:
        value = int(text.lstrip('+-').strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid hex ID offset: {text!r}') from None
    return -value if negative else value


class GatewayController(QObject):
    """The bus gateway tool: forwarding rules between the open connections, run by a core `Gateway`."""

    changed = pyqtSignal()
    rules_changed = pyqtSignal()

    def __init__(self, manager, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._manager = manager
        self._rules: List[GatewayRule] = []
        self._gateway = Gateway(self._resolve)
        self._enabled = False
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.rules_changed)
        manager.connections_changed.connect(self._on_connections_changed)

    # Connections

    def _resolve(self, name: str):
        connection = self._connection(name)
        return connection.session if connection is not None else None

    def _connection(self, name: str):
        for connection in self._manager.connections:
            if connection.name == name:
                return connection
        return None

    def _on_connections_changed(self):
        self._gateway.refresh()  # Sessions may have appeared, gone or been reopened
        self.changed.emit()
        self.rules_changed.emit()

    @pyqtProperty('QVariantList', notify=changed)
    def connections(self) -> List[Dict[str, Any]]:
        return [
            {'name': connection.name, 'can_transmit': connection.can_transmit, 'connected': connection.connected}
            for connection in self._manager.connections
        ]

    # State

    @pyqtProperty(bool, notify=changed)
    def enabled(self) -> bool:
        return self._enabled

    @pyqtProperty(bool, notify=changed)
    def running(self) -> bool:
        return self._gateway.running

    @pyqtSlot(bool)
    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        if enabled:
            self._gateway.start()
            self._timer.start()
        else:
            self._gateway.stop()
            self._timer.stop()
        self.changed.emit()
        self.rules_changed.emit()

    # Rules

    @pyqtProperty('QVariantList', notify=rules_changed)
    def rules(self) -> List[Dict[str, Any]]:
        return [self._rule_dict(index, rule) for index, rule in enumerate(self._rules)]

    def _rule_dict(self, index: int, rule: GatewayRule) -> Dict[str, Any]:
        source, destination = self._connection(rule.source), self._connection(rule.destination)
        active = (
            self._enabled
            and rule.enabled
            and source is not None
            and source.connected
            and destination is not None
            and destination.connected
            and destination.can_transmit
        )
        return {
            'index': index,
            'name': rule.name,
            'source': rule.source,
            'destination': rule.destination,
            'filter': rule.message_filter.text,
            'new_id': '' if rule.new_id is None else f'{rule.new_id:X}',
            'id_offset': '' if not rule.id_offset else f'{rule.id_offset:+X}',
            'min_interval_ms': round(rule.min_interval * 1000),
            'enabled': rule.enabled,
            'forwarded': rule.forwarded,
            'dropped': rule.dropped,
            'active': active,
            'source_missing': source is None,
            'destination_missing': destination is None,
        }

    def _build_rule(self, options: Dict[str, Any]) -> GatewayRule:
        source = str(options.get('source') or '').strip()
        destination = str(options.get('destination') or '').strip()
        if not source or not destination:
            raise ValueError('Choose a source and a destination connection')
        if source == destination:
            raise ValueError('The source and destination must be different connections')
        connection = self._connection(destination)
        if connection is not None and not connection.can_transmit:
            raise ValueError(f'{destination} cannot transmit, so it cannot be a gateway destination')
        try:
            message_filter = MessageFilter.parse(str(options.get('filter') or ''))
        except ValueError as e:
            raise ValueError(f'Filter: {e}') from None
        new_id = _parse_optional_id(options.get('new_id'), 'new ID')
        id_offset = _parse_offset(options.get('id_offset'))
        min_interval = max(0, int(options.get('min_interval_ms', 0) or 0)) / 1000
        name = str(options.get('name') or '').strip() or f'{source} → {destination}'
        return GatewayRule(
            name,
            source,
            destination,
            message_filter,
            new_id,
            id_offset,
            min_interval,
            enabled=bool(options.get('enabled', True)),
        )

    @pyqtSlot('QVariantMap', result=str)
    def add_rule(self, options: Dict[str, Any]) -> str:
        """Options: name, source, destination, filter, new_id (hex), id_offset (hex), min_interval_ms, enabled.
        Returns an error or ''."""
        try:
            rule = self._build_rule(options)
        except ValueError as e:
            return str(e)
        self._rules.append(rule)
        self._push()
        return ''

    @pyqtSlot(int, 'QVariantMap', result=str)
    def update_rule(self, index: int, options: Dict[str, Any]) -> str:
        if not 0 <= index < len(self._rules):
            return 'No such rule'
        try:
            rule = self._build_rule(options)
        except ValueError as e:
            return str(e)
        self._rules[index] = rule
        self._push()
        return ''

    @pyqtSlot(int, result=str)
    def remove_rule(self, index: int) -> str:
        if not 0 <= index < len(self._rules):
            return 'No such rule'
        del self._rules[index]
        self._push()
        return ''

    @pyqtSlot(int, bool)
    def set_rule_enabled(self, index: int, enabled: bool):
        if 0 <= index < len(self._rules):
            self._rules[index].enabled = enabled
            self._push()

    @pyqtSlot()
    def clear_rules(self):
        self._rules = []
        self._push()

    @pyqtSlot()
    def reset_counters(self):
        for rule in self._rules:
            rule.reset_counters()
        self.rules_changed.emit()

    def _push(self):
        self._gateway.set_rules(self._rules)
        self.changed.emit()
        self.rules_changed.emit()

    def shutdown(self):
        self._timer.stop()
        self._gateway.stop()
