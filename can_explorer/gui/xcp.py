import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.a2l import DATA_TYPES, A2lVariable, read_a2l
from can_explorer.core.can_command import CanCommandClient, CanCommandError
from can_explorer.core.ccp import CcpClient
from can_explorer.core.frames import format_hex, parse_can_id, parse_hex_bytes
from can_explorer.core.xcp import XcpClient
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_LOG = 200
DEFAULT_POLL_MS = 200


def _hex(text: str, maximum: int, what: str) -> int:
    try:
        value = int(str(text).strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


class XcpController(QObject):
    """Measurement and calibration over XCP or CCP on the connection's CAN bus: read measurements live and read or
    write calibration values, by address or by name from an A2L file."""

    changed = pyqtSignal()
    variables_changed = pyqtSignal()
    log_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._client = None  # XcpClient or CcpClient
        self._transport: Optional[CanCommandClient] = None
        self._protocol = 'XCP'
        self._busy = False
        self._a2l_file = ''
        self._variables: List[A2lVariable] = []
        self._values: Dict[str, Dict[str, Any]] = {}  # name -> {'value': str, 'raw': str, 'error': str}
        self._log: List[Dict[str, Any]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(DEFAULT_POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._polling = False

    @property
    def session(self):
        return self._connection.session

    def _note(self, text: str, error: bool = False):
        (logger.warning if error else logger.info)(f'{self._protocol}: {text}')
        self._log.append({'text': text, 'error': error})
        del self._log[:-MAX_LOG]
        self.log_changed.emit()

    # Properties

    @pyqtProperty('QVariantList', constant=True)
    def protocols(self) -> List[str]:
        return ['XCP', 'CCP']

    @pyqtProperty('QVariantList', constant=True)
    def data_types(self) -> List[str]:
        return list(DATA_TYPES)

    @pyqtProperty(str, notify=changed)
    def protocol(self) -> str:
        return self._protocol

    @pyqtProperty(bool, notify=changed)
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    @pyqtProperty(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(str, notify=changed)
    def byte_order(self) -> str:
        return self._client.byte_order if self._client is not None else 'little'

    @pyqtProperty(str, notify=changed)
    def a2l_file(self) -> str:
        return self._a2l_file

    @pyqtProperty(bool, notify=changed)
    def polling(self) -> bool:
        return self._polling

    @pyqtProperty('QVariantList', notify=log_changed)
    def log(self) -> List[Dict[str, Any]]:
        return list(self._log)

    @pyqtProperty('QVariantList', notify=variables_changed)
    def variables(self) -> List[Dict[str, Any]]:
        rows = []
        for variable in self._variables:
            value = self._values.get(variable.name, {})
            rows.append(
                {
                    'name': variable.name,
                    'description': variable.description,
                    'address': f'0x{variable.address:X}',
                    'type': variable.data_type,
                    'unit': variable.unit,
                    'writable': variable.writable,
                    'value': value.get('value', ''),
                    'raw': value.get('raw', ''),
                    'error': value.get('error', ''),
                }
            )
        return rows

    # Connection

    def _transport_error(self) -> str:
        if not self._connection.can_transmit:
            return 'This connection cannot transmit'
        if not self.session.is_open:
            return 'Connect to the bus first'
        return ''

    @pyqtSlot('QVariantMap', result=str)
    def connect_slave(self, options: Dict[str, Any]) -> str:
        """Options: protocol ('XCP'/'CCP'), request_id, response_id (hex), extended, station_address (hex, CCP),
        byte_order ('little'/'big', CCP). Returns an error or ''."""
        if self._busy:
            return 'An operation is in progress'
        if self.connected:
            return 'Disconnect first'
        error = self._transport_error()
        if error:
            return error
        protocol = str(options.get('protocol') or 'XCP').upper()
        try:
            extended = bool(options.get('extended', False))
            request_id = parse_can_id(str(options.get('request_id') or '7E0'), extended)
            response_id = parse_can_id(str(options.get('response_id') or '7E1'), extended)
            station = _hex(options.get('station_address') or '0', 0xFFFF, 'station address')
            byte_order = 'big' if str(options.get('byte_order') or 'little') == 'big' else 'little'
        except ValueError as e:
            return str(e)
        self._protocol = protocol if protocol in ('XCP', 'CCP') else 'XCP'
        self._transport = CanCommandClient(self.session, request_id, response_id, extended)
        if self._protocol == 'CCP':
            self._client = CcpClient(self._transport, station_address=station, byte_order=byte_order)
        else:
            self._client = XcpClient(self._transport)
        self._start(self._connect())
        return ''

    async def _connect(self):
        try:
            await self._client.connect()
            self._note(f'Connected ({self._protocol}, {self._client.byte_order}-endian)')
            self._polling = True
            self._timer.start()
        except Exception as e:
            self._note(f'Connect failed: {e}', error=True)
            self._close_transport()

    @pyqtSlot()
    def disconnect_slave(self):
        self._timer.stop()
        self._polling = False
        client = self._client
        if client is not None and client.connected:
            self._start(self._disconnect(client))
        else:
            self._close_transport()
            self.changed.emit()

    async def _disconnect(self, client):
        try:
            await client.disconnect()
            self._note('Disconnected')
        except Exception as e:
            self._note(f'Disconnect: {e}', error=True)
        finally:
            self._close_transport()

    def _close_transport(self):
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._client = None
        self.changed.emit()

    # A2L

    @pyqtSlot(QUrl, result=str)
    def load_a2l(self, url: QUrl) -> str:
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            variables = read_a2l(path)
        except Exception as e:
            return f'Could not read {Path(path).name}: {e}'
        self._variables = variables
        self._values = {}
        self._a2l_file = path
        measurements = sum(1 for v in variables if not v.writable)
        self._note(
            f'Loaded {Path(path).name}: {measurements} measurements, {len(variables) - measurements} characteristics'
        )
        self.changed.emit()
        self.variables_changed.emit()
        return ''

    @pyqtSlot()
    def unload_a2l(self):
        self._variables = []
        self._values = {}
        self._a2l_file = ''
        self.changed.emit()
        self.variables_changed.emit()

    # Running

    def _start(self, coroutine) -> None:
        self._busy = True
        self.changed.emit()
        asyncio.ensure_future(self._guarded(coroutine))

    async def _guarded(self, coroutine) -> None:
        try:
            await coroutine
        finally:
            self._busy = False
            self.changed.emit()

    def _variable(self, name: str) -> Optional[A2lVariable]:
        return next((v for v in self._variables if v.name == name), None)

    async def _read_variable(self, variable: A2lVariable):
        raw = await self._client.read(variable.address, 0, variable.size)
        physical = variable.decode(raw, self._client.byte_order)
        text = f'{physical:.6g}' + (f' {variable.unit}' if variable.unit else '')
        self._values[variable.name] = {'value': text, 'raw': format_hex(raw), 'error': ''}

    def _poll(self):
        if self._busy or not self.connected:
            return
        asyncio.ensure_future(self._poll_once())

    async def _poll_once(self):
        if not self.connected:
            return
        changed = False
        for variable in self._variables:
            try:
                await self._read_variable(variable)
                changed = True
            except Exception as e:
                self._values[variable.name] = {
                    **self._values.get(variable.name, {}),
                    'error': str(e),
                }
                if isinstance(e, CanCommandError):  # The link is gone: stop hammering it
                    self._note(f'Polling stopped: {e}', error=True)
                    self._timer.stop()
                    self._polling = False
                    self.changed.emit()
                    break
        if changed or not self.connected:
            self.variables_changed.emit()

    @pyqtSlot(str, result=str)
    def read_variable(self, name: str) -> str:
        variable = self._variable(name)
        if variable is None:
            return 'No such variable'
        if not self.connected:
            return 'Connect first'
        self._start(self._read_and_emit(variable))
        return ''

    async def _read_and_emit(self, variable: A2lVariable):
        try:
            await self._read_variable(variable)
        except Exception as e:
            self._values[variable.name] = {**self._values.get(variable.name, {}), 'error': str(e)}
            self._note(f'Read {variable.name}: {e}', error=True)
        self.variables_changed.emit()

    @pyqtSlot(str, str, result=str)
    def write_variable(self, name: str, text: str) -> str:
        variable = self._variable(name)
        if variable is None:
            return 'No such variable'
        if not variable.writable:
            return f'{name} is a measurement and cannot be written'
        if not self.connected:
            return 'Connect first'
        try:
            value = float(str(text).strip())
        except ValueError:
            return f'Enter a number to write to {name}'
        self._start(self._write(variable, value))
        return ''

    async def _write(self, variable: A2lVariable, value: float):
        try:
            await self._client.write(variable.address, 0, variable.encode(value, self._client.byte_order))
            await self._read_variable(variable)  # Read back
            self._note(f'{variable.name} = {value:g}')
        except Exception as e:
            self._values[variable.name] = {**self._values.get(variable.name, {}), 'error': str(e)}
            self._note(f'Write {variable.name}: {e}', error=True)
        self.variables_changed.emit()

    # Reading and writing by address (no A2L)

    @pyqtSlot(str, int, result=str)
    def read_address(self, address: str, size: int) -> str:
        if not self.connected:
            return 'Connect first'
        try:
            value = _hex(address, 0xFFFFFFFF, 'address')
        except ValueError as e:
            return str(e)

        async def run():
            try:
                raw = await self._client.read(value, 0, max(1, min(size, 64)))
                self._note(f'0x{value:X}: {format_hex(raw)}')
            except Exception as e:
                self._note(f'Read 0x{value:X}: {e}', error=True)

        self._start(run())
        return ''

    @pyqtSlot(str, str, result=str)
    def write_address(self, address: str, data: str) -> str:
        if not self.connected:
            return 'Connect first'
        try:
            value = _hex(address, 0xFFFFFFFF, 'address')
            payload = parse_hex_bytes(data)
        except ValueError as e:
            return str(e)
        if not payload:
            return 'Enter the bytes to write, hex'

        async def run():
            try:
                await self._client.write(value, 0, payload)
                self._note(f'Wrote {format_hex(payload)} to 0x{value:X}')
            except Exception as e:
                self._note(f'Write 0x{value:X}: {e}', error=True)

        self._start(run())
        return ''

    @pyqtSlot(int)
    def set_poll_interval(self, interval_ms: int):
        self._timer.setInterval(max(50, min(interval_ms, 5000)))

    @pyqtSlot()
    def clear_log(self):
        self._log.clear()
        self.log_changed.emit()

    def shutdown(self):
        # Called when the connection or the app closes, possibly with no running event loop, so tear down directly
        # rather than scheduling an async DISCONNECT (that is only sent on an interactive disconnect_slave()).
        self._timer.stop()
        self._polling = False
        self._close_transport()
