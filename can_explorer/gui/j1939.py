import asyncio
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.frames import parse_hex_bytes
from can_explorer.core.j1939_tools import (
    GLOBAL_ADDRESS,
    INDUSTRY_GROUPS,
    PGN_ADDRESS_CLAIMED,
    PGN_DM2,
    PGN_DM3,
    PGN_DM11,
    TOOL_ADDRESS,
    TOOL_PGN_NAMES,
    DiagnosticMessage,
    J1939Id,
    J1939Network,
    J1939Node,
    J1939Sender,
    parse_pgn,
    pgn_label,
)
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_LOG = 200
REFRESH_MS = 500


def _parse_address(text: str, what: str) -> int:
    text = text.strip()
    if not text:
        return GLOBAL_ADDRESS
    try:
        value = int(text, 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= 0xFF:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


def _parse_value(text: str) -> Any:
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text  # A value description, e.g. 'On'


def _diagnostic(message: Optional[DiagnosticMessage]) -> Dict[str, Any]:
    if message is None:
        return {'received': False, 'lamps': [], 'dtcs': []}
    return {
        'received': True,
        'lamps': [{'lamp': lamp, 'state': state} for lamp, state in message.lamps.items()],
        'dtcs': [
            {
                'spn': dtc.spn,
                'fmi': dtc.fmi,
                'name': dtc.spn_name,
                'fmi_text': dtc.fmi_text,
                'occurrences': dtc.occurrences,
            }
            for dtc in message.dtcs
        ],
    }


class J1939Controller(QObject):
    """The J1939 page of a connection: the nodes on the bus, their trouble codes, and sending parameter groups."""

    changed = pyqtSignal()
    log_changed = pyqtSignal()
    busy_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._network = J1939Network()
        self._sender = J1939Sender(connection.session, self._log)
        self._log_entries: List[Dict[str, Any]] = []
        self._busy = False
        self._source = TOOL_ADDRESS
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        if self.active:
            connection.session.add_message_listener(self._network.on_message)
            self._timer.start()

    @property
    def network(self) -> J1939Network:
        return self._network

    @property
    def session(self):
        return self._connection.session

    def _refresh(self):
        if self._network.changed:
            self._network.changed = False
            self.changed.emit()

    def _log(self, text: str, error: bool = False):
        self._log_entries.append({'text': text, 'error': error})
        del self._log_entries[:-MAX_LOG]
        self.log_changed.emit()

    # Properties

    @pyqtProperty(bool, constant=True)
    def active(self) -> bool:
        return self._connection.protocol == 'J1939'

    @pyqtProperty('QVariantList', notify=changed)
    def nodes(self) -> List[Dict[str, Any]]:
        return [self._node(node) for _, node in sorted(self._network.nodes.items())]

    @staticmethod
    def _node(node: J1939Node) -> Dict[str, Any]:
        name = node.name
        dm1 = node.dm1
        return {
            'address': node.address,
            'address_text': f'0x{node.address:02X}',
            'claimed': name is not None,
            'function': name.function_name if name is not None else '',
            'name': f'{name.value:016X}' if name is not None else '',
            'manufacturer': name.manufacturer_code if name is not None else 0,
            'identity': name.identity_number if name is not None else 0,
            'ecu_instance': name.ecu_instance if name is not None else 0,
            'function_instance': name.function_instance if name is not None else 0,
            'industry_group': INDUSTRY_GROUPS.get(name.industry_group, str(name.industry_group)) if name else '',
            'vehicle_system': name.vehicle_system if name is not None else 0,
            'arbitrary_address': bool(name and name.arbitrary_address_capable),
            'frames': node.frames,
            'pgn_count': len(node.pgns),
            'lamps': [lamp for lamp, state in dm1.lamps.items() if state == 'on'] if dm1 is not None else [],
            'active_dtcs': len(dm1.dtcs) if dm1 is not None else 0,
            'previous_dtcs': len(node.dm2.dtcs) if node.dm2 is not None else -1,  # -1: not requested yet
        }

    @pyqtProperty('QVariantList', notify=changed)
    def cannot_claim(self) -> List[str]:
        return [name.describe() for name in self._network.cannot_claim]

    @pyqtProperty('QVariantList', notify=log_changed)
    def log(self) -> List[Dict[str, Any]]:
        return list(self._log_entries)

    @pyqtProperty(bool, notify=busy_changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(str, constant=True)
    def tool_address(self) -> str:
        return f'{self._source:02X}'

    def _database_pgns(self) -> Dict[str, int]:
        database = self.session.decoder.database
        if database is None or not hasattr(database, 'messages'):
            return {}
        return {
            message.name: J1939Id.from_can_id(message.frame_id).pgn
            for message in database.messages()
            if getattr(message, 'is_extended_frame', False)
        }

    @pyqtProperty('QVariantList', notify=changed)
    def pgn_names(self) -> List[str]:
        """'0xFEF1 CCVS1' for the known parameter groups and those of the database."""
        names = {f'0x{pgn:05X} {name}' for pgn, name in TOOL_PGN_NAMES.items()}
        names |= {f'0x{pgn:05X} {name}' for name, pgn in self._database_pgns().items()}
        return sorted(names)

    @pyqtSlot(int, result='QVariantMap')
    def node_details(self, address: int) -> Dict[str, Any]:
        node = self._network.nodes.get(address)
        if node is None:
            return {}
        details = self._node(node)
        details['description'] = node.name.describe() if node.name is not None else 'No address claim seen'
        details['pgns'] = [
            {'pgn': f'0x{pgn:05X}', 'label': pgn_label(pgn), 'count': count} for pgn, count in sorted(node.pgns.items())
        ]
        details['dm1'] = _diagnostic(node.dm1)
        details['dm2'] = _diagnostic(node.dm2)
        return details

    # Actions

    def _check_send(self) -> Optional[str]:
        if not self._connection.can_transmit:
            return 'This connection cannot send'
        if not self.session.is_open:
            return 'Connect first'
        return None

    @pyqtSlot(str, result=str)
    def set_tool_address(self, text: str) -> str:
        try:
            self._source = _parse_address(text or 'F9', 'source address')
        except ValueError as e:
            return str(e)
        return ''

    def _request(self, pgn: int, destination: int) -> str:
        error = self._check_send()
        if error:
            return error
        try:
            self._sender.request(pgn, destination, self._source)
        except Exception as e:
            return str(e)
        return ''

    @pyqtSlot(result=str)
    def request_address_claims(self) -> str:
        return self._request(PGN_ADDRESS_CLAIMED, GLOBAL_ADDRESS)

    @pyqtSlot(int, result=str)
    def request_dm2(self, address: int) -> str:
        return self._request(PGN_DM2, address)

    @pyqtSlot(int, bool, result=str)
    def clear_dtcs(self, address: int, previously_active: bool) -> str:
        """Clear active (DM11) or previously active (DM3) trouble codes of a node."""
        return self._request(PGN_DM3 if previously_active else PGN_DM11, address)

    @pyqtSlot(str, str, result=str)
    def request_pgn(self, pgn_text: str, destination: str) -> str:
        try:
            pgn = parse_pgn(pgn_text, self._database_pgns())
            address = _parse_address(destination, 'destination address')
        except ValueError as e:
            return str(e)
        return self._request(pgn, address)

    def _payload(self, pgn: int, text: str) -> bytes:
        """Hex bytes, or signal values 'Name=value, …' of the database message of the PGN."""
        if '=' not in text:
            return parse_hex_bytes(text)
        database = self.session.decoder.database
        definition = None
        if database is not None:
            definition = next(
                (
                    message
                    for message in database.messages()
                    if getattr(message, 'is_extended_frame', False) and J1939Id.from_can_id(message.frame_id).pgn == pgn
                ),
                None,
            )
        if definition is None:
            raise ValueError(f'The database has no message for {pgn_label(pgn)}; enter the data in hex')
        # Signals not given are "not available" (all bits set), as J1939 prescribes
        values = dict(definition.decode(b'\xff' * definition.length, decode_choices=False))
        names = {signal.name for signal in definition.signals}
        for item in text.split(','):
            if not item.strip():
                continue
            name, _, value = item.partition('=')
            name = name.strip()
            if name not in names:
                raise ValueError(f'{definition.name} has no signal {name!r}')
            values[name] = _parse_value(value.strip())
        try:
            return bytes(definition.encode(values, padding=True, strict=False))  # Unused bits 1
        except Exception as e:
            raise ValueError(f'Cannot encode {definition.name}: {e}') from None

    @pyqtSlot(str, int, str, str, result=str)
    def send_pgn(self, pgn_text: str, priority: int, destination: str, data: str) -> str:
        """Send a parameter group from the tool address; more than 8 bytes go with BAM or RTS/CTS."""
        error = self._check_send()
        if error:
            return error
        if self._busy:
            return 'A transfer is in progress'
        try:
            pgn = parse_pgn(pgn_text, self._database_pgns())
            address = _parse_address(destination, 'destination address')
            payload = self._payload(pgn, data)
            if not 0 <= priority <= 7:
                raise ValueError('Priorities are 0 to 7')
        except ValueError as e:
            return str(e)
        self._busy = True
        self.busy_changed.emit()
        asyncio.ensure_future(self._send(pgn, payload, address, priority))
        return ''

    async def _send(self, pgn: int, payload: bytes, destination: int, priority: int):
        try:
            await self._sender.send(pgn, payload, self._source, destination, priority)
        except Exception as e:
            logger.warning(f'Sending {pgn_label(pgn)} failed: {e}')
            self._log(f'Sending {pgn_label(pgn)} failed: {e}', error=True)
        finally:
            self._busy = False
            self.busy_changed.emit()

    @pyqtSlot()
    def clear(self):
        self._network.clear()
        self._refresh()

    @pyqtSlot()
    def clear_log(self):
        self._log_entries.clear()
        self.log_changed.emit()
