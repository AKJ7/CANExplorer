import asyncio
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.doip import (
    ACTIVATION_TYPES,
    BROADCAST,
    DEFAULT_TESTER_ADDRESS,
    PORT,
    POWER_MODES,
    PROTOCOL_2012,
    PROTOCOL_2019,
    TLS_PORT,
    DoipClient,
    DoipEntity,
    DoipEntitySimulator,
    DoipError,
    discover,
    entity_status,
    power_mode,
)
from can_explorer.core.simulation import UdsServer
from can_explorer.gui.diagnostics import DiagnosticsController
from can_explorer.gui.flashing import FlashController
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_LOG = 200
SIMULATED = 'simulated'
DEFAULT_TARGET = 0x1001


def _parse_hex(text: str, maximum: int, what: str) -> int:
    try:
        value = int(str(text).strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


def _entity_row(entity: DoipEntity) -> Dict[str, Any]:
    return {
        'host': entity.host,
        'port': entity.port,
        'vin': entity.vin,
        'logical_address': f'{entity.logical_address:04X}',
        'eid': entity.eid_text,
        'gid': entity.gid_text,
        'further_action': entity.further_action_text if entity.further_action else '',
        'simulated': False,
    }


class DoipDiagnosticsController(DiagnosticsController):
    """UDS diagnostics over the DoIP tool's connection, to one target address."""

    def __init__(self, doip: 'DoipController', parent: Optional[QObject] = None):
        super().__init__(doip, parent)
        self._doip = doip
        self._target = DEFAULT_TARGET

    @property
    def is_lin(self) -> bool:
        return False

    def _target_text(self) -> str:
        return f'{self._target:04X}'

    @pyqtSlot(str, result=str)
    def set_target(self, target: str) -> str:
        """The logical address of the ECU (hex). Returns an error or ''."""
        try:
            self._target = _parse_hex(target, 0xFFFF, 'target address')
        except ValueError as e:
            return str(e)
        self.set_tester_present(False)
        return ''

    def _addressing(self) -> str:
        return 'doip'

    def _addressing_text(self) -> str:
        return f'target address 0x{self._target:04X}'

    def _transport(self):
        return self._doip.client.transport(self._target)

    def _transport_error(self) -> str:
        return '' if self._doip.client is not None and self._doip.client.is_open else 'Connect first'

    def _share_description(self, description):
        pass  # No trace to decode


class DoipController(QObject):
    """The DoIP tool: finds DoIP entities, connects to one with routing activation and runs UDS over it."""

    changed = pyqtSignal()
    entities_changed = pyqtSignal()
    log_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.client: Optional[DoipClient] = None
        self._simulator: Optional[DoipEntitySimulator] = None
        self._entities: List[DoipEntity] = []
        self._discovering = False
        self._connecting = False
        self._simulated = False
        self._log_entries: List[Dict[str, Any]] = []
        self._diagnostics = DoipDiagnosticsController(self, parent=self)
        self._flashing = FlashController(self._diagnostics, parent=self)
        self._diagnostics.changed.connect(self.changed)

    def _log(self, text: str, error: bool = False):
        (logger.warning if error else logger.info)(f'DoIP: {text}')
        self._log_entries.append({'text': text, 'error': error})
        del self._log_entries[:-MAX_LOG]
        self.log_changed.emit()

    @pyqtProperty(QObject, constant=True)
    def diagnostics(self) -> DoipDiagnosticsController:
        return self._diagnostics

    @pyqtProperty(QObject, constant=True)
    def flashing(self) -> FlashController:
        return self._flashing

    @pyqtProperty('QVariantList', notify=log_changed)
    def log(self) -> List[Dict[str, Any]]:
        return list(self._log_entries)

    @pyqtSlot()
    def clear_log(self):
        self._log_entries.clear()
        self.log_changed.emit()

    @pyqtProperty('QVariantList', constant=True)
    def activation_types(self) -> List[Dict[str, Any]]:
        return [{'value': value, 'name': f'0x{value:02X} {name}'} for value, name in ACTIVATION_TYPES.items()]

    @pyqtProperty('QVariantList', constant=True)
    def protocol_versions(self) -> List[Dict[str, Any]]:
        return [
            {'value': PROTOCOL_2012, 'name': 'ISO 13400-2:2012'},
            {'value': PROTOCOL_2019, 'name': 'ISO 13400-2:2019'},
        ]

    @pyqtProperty(str, constant=True)
    def default_tester_address(self) -> str:
        return f'{DEFAULT_TESTER_ADDRESS:04X}'

    @pyqtProperty(int, constant=True)
    def default_port(self) -> int:
        return PORT

    @pyqtProperty(int, constant=True)
    def tls_port(self) -> int:
        return TLS_PORT

    # Discovery

    @pyqtProperty('QVariantList', notify=entities_changed)
    def entities(self) -> List[Dict[str, Any]]:
        """The entities found, then the simulated entity."""
        rows = [_entity_row(entity) for entity in self._entities]
        rows.append(
            {
                'host': SIMULATED,
                'port': 0,
                'vin': 'Simulated DoIP entity (no hardware)',
                'logical_address': '1000',
                'eid': '',
                'gid': '',
                'further_action': '',
                'simulated': True,
            }
        )
        return rows

    @pyqtProperty(bool, notify=changed)
    def discovering(self) -> bool:
        return self._discovering

    @pyqtSlot(str, result=str)
    def discover(self, address: str) -> str:
        """Send a vehicle identification request to an address (empty: broadcast). Returns an error or ''."""
        if self._discovering:
            return 'Looking for DoIP entities already'
        address = address.strip() or BROADCAST
        self._discovering = True
        self.changed.emit()
        asyncio.ensure_future(self._discover(address))
        return ''

    async def _discover(self, address: str):
        try:
            self._entities = await discover(address)
            count = len(self._entities)
            self._log(f'{count} DoIP entit{"y" if count == 1 else "ies"} answered at {address}')
        except OSError as e:
            self._log(f'Could not send the vehicle identification request to {address}: {e.strerror or e}', True)
        finally:
            self._discovering = False
            self.entities_changed.emit()
            self.changed.emit()

    # Connection

    @pyqtProperty(bool, notify=changed)
    def is_open(self) -> bool:
        return self.client is not None and self.client.is_open

    @pyqtProperty(bool, notify=changed)
    def busy(self) -> bool:
        return self._connecting or self._discovering

    @pyqtProperty(bool, notify=changed)
    def simulated(self) -> bool:
        return self.is_open and self._simulated

    @pyqtProperty(str, notify=changed)
    def connection_text(self) -> str:
        client = self.client
        if client is None or not client.is_open:
            return ''
        where = 'simulated entity' if self._simulated else f'{client.host}:{client.port}'
        text = f'{where} · entity 0x{client.entity_address:04X} · tester 0x{client.source_address:04X}'
        return text + (' · TLS' if client.tls else '')

    @pyqtSlot('QVariantMap', result=str)
    def connect_entity(self, options: Dict[str, Any]) -> str:
        """Options: host ('simulated' for the simulated entity), port, tester_address (hex), activation_type,
        protocol_version, tls. Returns an error or ''."""
        if self._connecting:
            return 'Connecting already'
        if self.is_open:
            return 'Disconnect first'
        host = str(options.get('host') or '').strip()
        if not host:
            return 'Enter the address of the DoIP entity, or choose one found by Discover'
        try:
            tester = _parse_hex(options.get('tester_address') or self.default_tester_address, 0xFFFF, 'tester address')
            port = int(options.get('port') or 0) or None
            activation_type = int(options.get('activation_type', 0))
            version = int(options.get('protocol_version', PROTOCOL_2012))
        except (TypeError, ValueError) as e:
            return str(e)
        tls = bool(options.get('tls', False))
        self._connecting = True
        self.changed.emit()
        asyncio.ensure_future(self._connect(host, port, tester, activation_type, version, tls))
        return ''

    async def _connect(self, host: str, port: Optional[int], tester: int, activation_type: int, version, tls: bool):
        simulated = host == SIMULATED
        try:
            if simulated:
                if self._simulator is None:
                    self._simulator = DoipEntitySimulator(targets={DEFAULT_TARGET: UdsServer(slow_routines=(0x0F0F,))})
                    await self._simulator.start()
                host, port, tls = self._simulator.host, self._simulator.port, False
            client = DoipClient(host, port, tester, activation_type, version, tls)
            client.add_close_listener(lambda reason: self._on_closed(client, reason))
            activation = await client.connect()
            self.client, self._simulated = client, simulated
            self._log(
                f'Connected to {"the simulated entity" if simulated else f"{client.host}:{client.port}"}: '
                f'{activation.text} by entity 0x{activation.entity_address:04X}'
            )
        except DoipError as e:
            self._log(str(e), error=True)
        except OSError as e:
            self._log(f'Could not start the simulated entity: {e.strerror or e}', error=True)
        finally:
            self._connecting = False
            self.changed.emit()

    def _on_closed(self, client: DoipClient, reason: str):
        if client is not self.client:
            return
        self._diagnostics.set_tester_present(False)
        self._log(reason or 'Disconnected', error=bool(reason))
        self.changed.emit()

    @pyqtSlot()
    def disconnect_entity(self):
        self._flashing.cancel()
        self._diagnostics.set_tester_present(False)
        if self.client is not None:
            asyncio.ensure_future(self._close())

    async def _close(self):
        client = self.client
        if client is not None:
            await client.close()
        self.changed.emit()

    # Status

    @pyqtSlot(result=str)
    def query_status(self) -> str:
        """Ask the connected entity for its status and diagnostic power mode (over UDP). Returns an error or ''."""
        if not self.is_open:
            return 'Connect first'
        # Over UDP on the discovery port, also when the diagnostic messages use TLS
        port = self._simulator.port if self._simulated else PORT
        asyncio.ensure_future(self._query_status(self.client.host, port))
        return ''

    async def _query_status(self, host: str, port: int):
        try:
            status = await entity_status(host, port)
            self._log(f'Entity status: {status.text}')
        except (DoipError, OSError) as e:
            self._log(f'Entity status: {e}', error=True)
        try:
            mode = await power_mode(host, port)
            self._log(f'Diagnostic power mode: {POWER_MODES.get(mode, f"0x{mode:02X}")}')
        except (DoipError, OSError) as e:
            self._log(f'Diagnostic power mode: {e}', error=True)

    def shutdown(self):
        self._flashing.shutdown()
        self._diagnostics.shutdown()
        if self.client is not None:
            asyncio.ensure_future(self.client.close())
        if self._simulator is not None:
            asyncio.ensure_future(self._simulator.stop())
