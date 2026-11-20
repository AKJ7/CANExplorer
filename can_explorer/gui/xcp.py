import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core import calibration
from can_explorer.core.a2l import DATA_TYPES, A2lDocument, A2lVariable, load_a2l
from can_explorer.core.can_command import CanCommandClient, CanCommandError
from can_explorer.core.ccp import CcpClient
from can_explorer.core.frames import format_hex, parse_can_id, parse_hex_bytes
from can_explorer.core.protocols.xcp import CcpDecoder, XcpDecoder
from can_explorer.core.uds import load_key_source
from can_explorer.core.xcp import DaqSignal, XcpClient, resource_names
from can_explorer.core.xcp_ethernet import DEFAULT_PORT, XcpEthernetTransport
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_LOG = 200
DEFAULT_POLL_MS = 200
REFRESH_MS = 100  # The table follows DAQ samples at most this often
TRANSPORTS = ['CAN', 'CAN FD', 'UDP', 'TCP']
MAX_CHECKSUM_VERIFY = 0x10000  # Larger ranges are only checksummed by the ECU, not compared with an upload


def _hex(text: str, maximum: int, what: str) -> int:
    try:
        value = int(str(text).strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


def _plottable(value: Any, variable: A2lVariable, raw: bytes, byte_order: str) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and raw:  # Verbal: plot the raw number
        return variable.raw_value(raw, byte_order)
    return None


class XcpController(QObject):
    """Measurement and calibration over XCP (CAN, CAN FD, UDP, TCP) or CCP on the connection: read measurements
    live (polling or DAQ) and read or write calibration values, by address or by name from an A2L file."""

    changed = pyqtSignal()
    variables_changed = pyqtSignal()
    log_changed = pyqtSignal()
    editor_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._client = None  # XcpClient or CcpClient
        self._transport = None  # CanCommandClient or XcpEthernetTransport
        self._protocol = 'XCP'
        self._transport_name = 'CAN'
        self._busy = False
        self._document: Optional[A2lDocument] = None
        self._values: Dict[str, Dict[str, Any]] = {}  # name -> {'value': str, 'raw': str, 'error': str}
        self._log: List[Dict[str, Any]] = []
        self._info: Dict[str, str] = {}
        self._events: List[Dict[str, Any]] = []
        self._acquisition: Dict[str, int] = {}  # name -> DAQ event channel (absent: polled)
        self._plotted: Set[str] = set()
        self._measurement = None
        self._key_source = ''
        self._dataset: Optional[calibration.Dataset] = None
        self._dataset_file = ''
        self._differences: List[Dict[str, Any]] = []
        self._editor: Dict[str, Any] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(DEFAULT_POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(REFRESH_MS)
        self._refresh.timeout.connect(self.variables_changed.emit)
        self._polling = False
        self._lock = asyncio.Lock()

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
    def transports(self) -> List[str]:
        return TRANSPORTS

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
        return self._document.path if self._document is not None else ''

    @pyqtProperty(bool, notify=changed)
    def polling(self) -> bool:
        return self._polling

    @pyqtProperty(bool, notify=changed)
    def daq_running(self) -> bool:
        return self._measurement is not None

    @pyqtProperty(str, notify=changed)
    def key_source(self) -> str:
        return self._key_source

    @pyqtProperty('QVariantMap', notify=changed)
    def info(self) -> Dict[str, str]:
        return dict(self._info)

    @pyqtProperty('QVariantList', notify=changed)
    def details(self) -> List[Dict[str, str]]:
        """What the slave reported, for display: {'label', 'value', 'level': '' | 'ok' | 'warning' | 'error'}."""
        rows = []
        for label, value in self._info.items():
            level = ''
            if label == 'EPK':
                level = 'ok' if value.startswith('matches') else 'error'
            elif label == 'locked':
                level = 'ok' if value == 'none' else 'warning'
            rows.append({'label': label, 'value': value, 'level': level})
        return rows

    @pyqtProperty('QVariantList', notify=changed)
    def events(self) -> List[Dict[str, Any]]:
        return list(self._events)

    @pyqtProperty(str, notify=changed)
    def dataset_file(self) -> str:
        return self._dataset_file

    @pyqtProperty('QVariantList', notify=changed)
    def differences(self) -> List[Dict[str, Any]]:
        return [
            {
                'name': d['name'],
                'file': calibration.format_value(d['reference']) if d['reference'] is not None else '–',
                'ecu': calibration.format_value(d['other']) if d['other'] is not None else '–',
            }
            for d in self._differences
        ]

    @pyqtProperty('QVariantMap', notify=editor_changed)
    def editor(self) -> Dict[str, Any]:
        return dict(self._editor)

    @pyqtProperty('QVariantList', notify=log_changed)
    def log(self) -> List[Dict[str, Any]]:
        return list(self._log)

    @pyqtProperty('QVariantList', notify=variables_changed)
    def variables(self) -> List[Dict[str, Any]]:
        rows = []
        variables = self._document.variables if self._document is not None else []
        for variable in variables:
            value = self._values.get(variable.name, {})
            event = self._acquisition.get(variable.name)
            rows.append(
                {
                    'name': variable.name,
                    'description': variable.description,
                    'address': f'0x{variable.address:X}' + (f':{variable.extension}' if variable.extension else ''),
                    'type': variable.data_type if variable.kind in ('MEASUREMENT', 'VALUE') else variable.kind,
                    'kind': variable.kind,
                    'unit': variable.unit,
                    'writable': variable.writable and not variable.unsupported,
                    'structured': variable.kind != 'ASCII'
                    and (variable.kind not in ('MEASUREMENT', 'VALUE') or bool(variable.shape)),
                    'daq': variable.kind == 'MEASUREMENT',
                    'acquisition': 0 if event is None else 1 + self._event_index(event),
                    'plotted': variable.name in self._plotted,
                    'value': value.get('value', ''),
                    'raw': value.get('raw', ''),
                    'error': variable.unsupported or value.get('error', ''),
                }
            )
        return rows

    def _event_index(self, channel: int) -> int:
        return next((i for i, e in enumerate(self._events) if e['channel'] == channel), 0)

    # Connection

    def _transport_error(self, transport: str) -> str:
        if transport in ('UDP', 'TCP'):
            return ''
        if not self._connection.can_transmit:
            return 'This connection cannot transmit'
        if not self.session.is_open:
            return 'Connect to the bus first'
        if transport == 'CAN FD' and not self._connection.fd:
            return 'This connection is not a CAN FD connection'
        return ''

    @pyqtSlot(result='QVariantMap')
    def a2l_settings(self) -> Dict[str, Any]:
        """Connection settings from the loaded A2L's IF_DATA, for the form: protocol, transport, ids, host…"""
        document = self._document
        if document is None:
            return {}
        if document.xcp is not None and document.xcp.transports:
            layer = document.xcp.transports[0]
            settings: Dict[str, Any] = {'protocol': 'XCP'}
            if layer.kind == 'CAN':
                settings['transport'] = 'CAN FD' if layer.fd else 'CAN'
                settings['extended'] = layer.extended
                if layer.master_id is not None:
                    settings['request_id'] = f'{layer.master_id:X}'
                if layer.slave_id is not None:
                    settings['response_id'] = f'{layer.slave_id:X}'
            else:
                settings.update(transport=layer.kind, host=layer.host or '127.0.0.1', port=layer.port or DEFAULT_PORT)
            return settings
        if document.ccp is not None and document.ccp.cro_id is not None:
            ccp = document.ccp
            return {
                'protocol': 'CCP',
                'transport': 'CAN',
                'request_id': f'{ccp.cro_id:X}',
                'response_id': f'{ccp.dto_id or 0:X}',
                'extended': ccp.extended,
                'station_address': f'{ccp.station_address or 0:X}',
            }
        return {}

    @pyqtSlot('QVariantMap', result=str)
    def connect_slave(self, options: Dict[str, Any]) -> str:
        """Options: protocol ('XCP'/'CCP'), transport ('CAN', 'CAN FD', 'UDP', 'TCP'), request_id, response_id
        (hex), extended, station_address (hex, CCP), byte_order ('little'/'big', CCP), host, port (Ethernet).
        Returns an error or ''."""
        if self._busy:
            return 'An operation is in progress'
        if self.connected:
            return 'Disconnect first'
        protocol = str(options.get('protocol') or 'XCP').upper()
        protocol = protocol if protocol in ('XCP', 'CCP') else 'XCP'
        transport = str(options.get('transport') or 'CAN')
        transport = transport if transport in TRANSPORTS else 'CAN'
        if protocol == 'CCP' and transport not in ('CAN',):
            return 'CCP runs on CAN only'
        error = self._transport_error(transport)
        if error:
            return error
        try:
            compute_key = load_key_source(self._key_source) if self._key_source else None
        except Exception as e:
            return f'Could not load the seed & key algorithm: {e}'
        self._protocol = protocol
        self._transport_name = transport
        if transport in ('UDP', 'TCP'):
            host = str(options.get('host') or '').strip()
            if not host:
                return 'Enter the host of the XCP slave'
            try:
                port = int(options.get('port') or DEFAULT_PORT)
            except (TypeError, ValueError):
                return 'Invalid port'
            self._transport = XcpEthernetTransport(host, port, transport.lower())
            self._client = XcpClient(self._transport, compute_key=compute_key)
            self._start(self._connect())
            return ''
        try:
            extended = bool(options.get('extended', False))
            request_id = parse_can_id(str(options.get('request_id') or '7E0'), extended)
            response_id = parse_can_id(str(options.get('response_id') or '7E1'), extended)
            station = _hex(options.get('station_address') or '0', 0xFFFF, 'station address')
            byte_order = 'big' if str(options.get('byte_order') or 'little') == 'big' else 'little'
        except ValueError as e:
            return str(e)
        layer = self._document.xcp.transport('CAN') if self._document is not None and self._document.xcp else None
        daq_ids = [identifier for _, identifier in layer.daq_ids] if layer is not None else []
        self._transport = CanCommandClient(
            self.session, request_id, response_id, extended, fd=transport == 'CAN FD', data_ids=daq_ids
        )
        if protocol == 'CCP':
            self._client = CcpClient(self._transport, station_address=station, byte_order=byte_order,
                                     compute_key=compute_key)  # fmt: skip
        else:
            self._client = XcpClient(self._transport, compute_key=compute_key)
        self._configure_decoder(request_id, response_id, extended)
        self._start(self._connect())
        return ''

    def _configure_decoder(self, request_id: int, response_id: int, extended: bool) -> None:
        decoder = getattr(self.session.decoder, 'protocol', None)
        wanted = CcpDecoder if self._protocol == 'CCP' else XcpDecoder
        if isinstance(decoder, wanted):
            decoder.configure(request_id, response_id, extended)

    async def _connect(self):
        client = self._client
        try:
            if isinstance(self._transport, XcpEthernetTransport):
                await self._transport.open()
            await client.connect()
        except Exception as e:
            self._note(f'Connect failed: {e}', error=True)
            self._close_transport()
            return
        info: Dict[str, str] = {'transport': self._transport_name, 'byte order': f'{client.byte_order} endian'}
        if isinstance(client, XcpClient):
            info.update(
                {
                    'granularity': f'{client.address_granularity} byte(s)',
                    'MAX_CTO / DTO': f'{client.max_cto} / {client.max_dto}',
                    'locked': resource_names(client.protection),
                }
            )
            client.on_event = lambda event: self._note(f'ECU event: {event.name}', error=event.code == 0x06)
            for name, operation in (('ECU id', client.get_id()), ('page', client.get_cal_page())):
                try:
                    info[name] = str(await operation)
                except Exception:
                    pass
        else:
            try:
                identification = await client.exchange_id()
                info['ECU id'] = identification['id']
                info['locked'] = self._ccp_locked(identification['protection'])
            except Exception:
                pass
        if self._document is not None:
            try:
                epk = await calibration.check_epk(client, self._document)
                if epk['match'] is not None:
                    info['EPK'] = 'matches the A2L' if epk['match'] else f'MISMATCH: ECU {epk["ecu"]!r}'
                    if not epk['match']:
                        self._note(f'The A2L does not match the ECU: EPK {epk["expected"]!r}, ECU {epk["ecu"]!r}', True)
            except Exception as e:
                info['EPK'] = f'not readable: {e}'
        self._info = info
        await self._load_events()
        self._note(f'Connected ({self._protocol} on {self._transport_name}, {client.byte_order} endian)')
        self._polling = True
        self._timer.start()
        await self._read_characteristics()

    async def _read_characteristics(self) -> None:
        """Read the characteristics that polling does not refresh (curves, maps, blocks, axes, strings) once."""
        polled = {v.name for v in self._polled()}
        variables = self._document.variables if self._document is not None else []
        for variable in variables:
            if variable.name in polled or variable.unsupported or variable.kind == 'MEASUREMENT':
                continue
            if not self.connected:
                return
            try:
                await self._read_variable(variable)
            except Exception as e:
                self._values[variable.name] = {**self._values.get(variable.name, {}), 'error': str(e)}
                if isinstance(e, CanCommandError):
                    break
        self.variables_changed.emit()

    @staticmethod
    def _ccp_locked(protection: int) -> str:
        return (
            ', '.join(name for bit, name in ((0x01, 'CAL'), (0x02, 'DAQ'), (0x40, 'PGM')) if protection & bit) or 'none'
        )

    async def _load_events(self) -> None:
        events: List[Dict[str, Any]] = []
        document = self._document
        if self._protocol == 'XCP' and document is not None and document.events:
            events = [{'channel': e.channel, 'name': e.name} for e in document.events]
        elif self._protocol == 'XCP' and isinstance(self._client, XcpClient):
            try:
                events = [{'channel': e['channel'], 'name': e['name'] or f'Event {e["channel"]}'}
                          for e in await self._client.daq_events()]  # fmt: skip
            except Exception:
                events = []
        elif self._protocol == 'CCP' and document is not None and document.ccp is not None and document.ccp.rasters:
            events = [{'channel': channel, 'name': name} for name, channel in document.ccp.rasters]
        if not events:
            events = [{'channel': channel, 'name': f'Event {channel}'} for channel in range(4)]
        self._events = events
        self.changed.emit()

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
            await self._stop_measurement()
            await client.disconnect()
            self._note('Disconnected')
        except Exception as e:
            self._note(f'Disconnect: {e}', error=True)
        finally:
            self._close_transport()

    def _close_transport(self):
        self._measurement = None
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._client = None
        self._info = {}
        self.changed.emit()

    # Seed & key

    @pyqtSlot(str)
    def set_key_source(self, source: str):
        """Use a key source: 'keystore:<entry>', 'profile:<name>' (a security profile) or a file path."""
        self._key_source = source
        if self._client is not None:
            try:
                self._client.compute_key = load_key_source(source) if source else None
            except Exception as e:
                self._note(f'Seed & key algorithm: {e}', error=True)
        self.changed.emit()

    @pyqtSlot(QUrl)
    def set_key_file(self, url: QUrl):
        self.set_key_source(url.toLocalFile() if url.isLocalFile() else url.toString())

    @pyqtSlot(result=str)
    def unlock(self) -> str:
        if not self.connected:
            return 'Connect first'
        if not self._key_source:
            return 'Choose a seed & key algorithm first'

        async def run():
            try:
                left = await self._client.unlock_all()
                if isinstance(self._client, XcpClient):
                    self._info['locked'] = resource_names(left)
                    self._note(f'Unlocked; still locked: {resource_names(left)}')
                else:
                    self._info['locked'] = 'none'
                    self._note('Unlocked CAL and DAQ')
                self.changed.emit()
            except Exception as e:
                self._note(f'Unlock: {e}', error=True)

        self._start(run())
        return ''

    # A2L

    @pyqtSlot(QUrl, result=str)
    def load_a2l(self, url: QUrl) -> str:
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            document = load_a2l(path)
        except Exception as e:
            return f'Could not read {Path(path).name}: {e}'
        self._unplot_all()
        self._document = document
        self._values = {}
        self._acquisition = {}
        variables = document.variables
        measurements = sum(1 for v in variables if v.kind == 'MEASUREMENT')
        unsupported = sum(1 for v in variables if v.unsupported)
        self._note(
            f'Loaded {Path(path).name}: {measurements} measurements, {len(variables) - measurements} characteristics'
            + (f' ({unsupported} not readable)' if unsupported else '')
        )
        self.changed.emit()
        self.variables_changed.emit()
        if self.connected and not self._busy:
            self._start(self._read_characteristics())
        return ''

    @pyqtSlot()
    def unload_a2l(self):
        self._unplot_all()
        self._document = None
        self._values = {}
        self._acquisition = {}
        self.changed.emit()
        self.variables_changed.emit()

    # Running

    def _start(self, coroutine) -> None:
        self._busy = True
        self.changed.emit()
        asyncio.ensure_future(self._guarded(coroutine))

    async def _guarded(self, coroutine) -> None:
        try:
            async with self._lock:  # One exchange with the ECU at a time (polling waits too)
                await coroutine
        finally:
            self._busy = False
            self.changed.emit()

    def _variable(self, name: str) -> Optional[A2lVariable]:
        return self._document.variable(name) if self._document is not None else None

    @staticmethod
    def _summary(variable: A2lVariable, value: Any) -> str:
        """The table's text of a value: curves and maps by their size and range."""
        if variable.kind in ('CURVE', 'MAP') and isinstance(value, list):
            numbers = [v for v in calibration._flatten(value) if isinstance(v, (int, float))]
            size = f'{len(value[0])} × {len(value)}' if variable.kind == 'MAP' and value else f'{len(value)} points'
            if numbers:
                low, high = calibration.format_value(min(numbers)), calibration.format_value(max(numbers))
                span = low if low == high else f'{low} … {high}'
                return f'{size} · {span}' + (f' {variable.unit}' if variable.unit else '')
            return size
        unit = variable.unit if not isinstance(value, (list, str)) else ''
        text = calibration.format_value(value, unit)
        if isinstance(value, list) and variable.unit:
            text += f' {variable.unit}'
        return text

    def _set_value(self, variable: A2lVariable, value: Any, raw: bytes, timestamp: Optional[float] = None) -> None:
        text = self._summary(variable, value)
        self._values[variable.name] = {'value': text, 'raw': format_hex(raw[:16]), 'error': ''}
        if variable.name in self._plotted:
            number = _plottable(value, variable, raw, self._byte_order(variable))
            if number is not None:
                self.session.signals.append_external(self._plot_key(variable.name), timestamp or time.time(), number)

    def _byte_order(self, variable: Optional[A2lVariable] = None) -> str:
        if variable is not None and variable.byte_order:
            return variable.byte_order
        if self._document is not None and self._document.byte_order:
            return self._document.byte_order
        return self._client.byte_order if self._client is not None else 'little'

    async def _read_variable(self, variable: A2lVariable):
        result = await calibration.read_value(self._client, variable, self._document)
        self._set_value(variable, result.value, result.raw)
        return result

    def _poll(self):
        if self._busy or not self.connected:
            return
        asyncio.ensure_future(self._poll_once())

    def _polled(self) -> List[A2lVariable]:
        variables = self._document.variables if self._document is not None else []
        daq = self._acquisition if self._measurement is not None else {}
        return [v for v in variables if v.kind in ('MEASUREMENT', 'VALUE') and not v.unsupported and v.name not in daq]

    async def _poll_once(self):
        async with self._lock:
            await self._poll_locked()

    async def _poll_locked(self):
        if not self.connected:
            return
        variables = [v for v in self._polled() if v.kind == 'MEASUREMENT' or not v.shape]
        if not variables:
            return
        try:
            raw = await calibration.read_many(self._client, [v for v in variables if v.layout is None])
        except Exception as e:
            self._poll_failed(e)
            return
        for variable in variables:
            data = raw.get(variable.name)
            try:
                if data is None:  # A characteristic: through its record layout
                    await self._read_variable(variable)
                elif variable.shape:
                    self._set_value(variable, variable.decode_values(data, self._byte_order(variable)), data)
                else:
                    self._set_value(variable, variable.decode(data, self._byte_order(variable)), data)
            except Exception as e:
                self._values[variable.name] = {**self._values.get(variable.name, {}), 'error': str(e)}
                if isinstance(e, CanCommandError):
                    self._poll_failed(e)
                    break
        self.variables_changed.emit()

    def _poll_failed(self, error: Exception) -> None:
        if isinstance(error, CanCommandError):  # The link is gone: stop hammering it
            self._note(f'Polling stopped: {error}', error=True)
            self._timer.stop()
            self._polling = False
            self.changed.emit()
        else:
            self._note(f'Polling: {error}', error=True)

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
        """Write a value typed by the user: a number or verbal value, a list ("1, 2, 3"), map rows ("1 2; 3 4"),
        or JSON (a list, or {"value": …, "axes": […]} from the editor)."""
        variable = self._variable(name)
        if variable is None:
            return 'No such variable'
        if not variable.writable:
            return f'{name} is a measurement and cannot be written'
        if variable.unsupported:
            return f'{name}: {variable.unsupported}'
        if not self.connected:
            return 'Connect first'
        axes = None
        try:
            stripped = str(text).strip()
            if stripped.startswith('{'):
                content = json.loads(stripped)
                value, axes = content['value'], content.get('axes')
            else:
                value = calibration.parse_value(variable, stripped)
        except (ValueError, KeyError, calibration.CalibrationError) as e:
            return f'{name}: {e}'
        if value == '' and variable.kind != 'ASCII':
            return f'Enter a value to write to {name}'
        self._start(self._write(variable, value, axes))
        return ''

    async def _write(self, variable: A2lVariable, value: Any, axes=None):
        try:
            await calibration.write_value(self._client, variable, value, self._document, axes)
            result = await self._read_variable(variable)  # Read back
            self._note(f'{variable.name} = {calibration.format_value(result.value, variable.unit)}')
            if self._editor.get('name') == variable.name:
                self._show_editor(variable, result)
        except Exception as e:
            self._values[variable.name] = {**self._values.get(variable.name, {}), 'error': str(e)}
            self._note(f'Write {variable.name}: {e}', error=True)
        self.variables_changed.emit()

    # Editor of curves, maps, blocks and arrays

    @pyqtSlot(str, result=str)
    def open_editor(self, name: str) -> str:
        variable = self._variable(name)
        if variable is None:
            return 'No such variable'
        if not self.connected:
            return 'Connect first'

        async def run():
            try:
                self._show_editor(variable, await self._read_variable(variable))
            except Exception as e:
                self._note(f'Read {name}: {e}', error=True)
            self.variables_changed.emit()

        self._start(run())
        return ''

    def _show_editor(self, variable: A2lVariable, result: calibration.CalibrationValue) -> None:
        value = result.value
        rows = value if variable.kind == 'MAP' else [value] if isinstance(value, list) else [[value]]
        if rows and rows[0] and isinstance(rows[0][0], list):  # A multi-dimensional block: one row per first index
            rows = [calibration._flatten(row) for row in rows]
        self._editor = {
            'name': variable.name,
            'kind': variable.kind,
            'unit': variable.unit,
            'writable': variable.writable and not variable.unsupported,
            'rows': [[calibration.format_value(v) for v in row] for row in rows],
            'x': [calibration.format_value(v) for v in result.axes[0]] if result.axes else [],
            'y': [calibration.format_value(v) for v in result.axes[1]] if len(result.axes) > 1 else [],
            'x_editable': bool(variable.axes) and variable.axes[0].attribute == 'STD_AXIS',
            'y_editable': len(variable.axes) > 1 and variable.axes[1].attribute == 'STD_AXIS',
            'text': value if isinstance(value, str) else '',
            'description': variable.description,
            'limits': (
                f'{calibration.format_value(variable.minimum)} … {calibration.format_value(variable.maximum)}'
                if variable.minimum is not None and variable.maximum is not None
                else ''
            ),
            'x_name': self._axis_label(variable, 0),
            'y_name': self._axis_label(variable, 1),
        }
        self.editor_changed.emit()

    @staticmethod
    def _axis_label(variable: A2lVariable, index: int) -> str:
        if index >= len(variable.axes):
            return ''
        axis = variable.axes[index]
        name = axis.input_quantity if axis.input_quantity not in ('', 'NO_INPUT_QUANTITY') else 'XY'[index]
        return f'{name} [{axis.unit}]' if axis.unit else name

    @pyqtSlot()
    def close_editor(self):
        self._editor = {}
        self.editor_changed.emit()

    @pyqtSlot('QVariantList', 'QVariantList', 'QVariantList', result=str)
    def write_editor(self, rows: List[List[str]], x: List[str], y: List[str]) -> str:
        """Write the editor's cells (and the points of its standard axes)."""
        name = self._editor.get('name')
        variable = self._variable(name) if name else None
        if variable is None:
            return 'Nothing to write'

        def cell(text: str):
            text = str(text).strip()
            try:
                return float(text)
            except ValueError:
                if variable.compu.is_verbal:
                    return text
                raise calibration.CalibrationError(f'{text!r} is not a number') from None

        try:
            values = [[cell(c) for c in row] for row in rows]
            axes = None
            if self._editor.get('x_editable') or self._editor.get('y_editable'):
                axes = [[float(v) for v in x] if self._editor.get('x_editable') else None,
                        [float(v) for v in y] if self._editor.get('y_editable') else None]  # fmt: skip
        except (ValueError, calibration.CalibrationError) as e:
            return str(e)
        if variable.kind == 'MAP':
            value: Any = values
        elif variable.kind in ('VALUE',) and not variable.shape:
            value = values[0][0]
        elif len(variable.shape) > 1:
            value = values
        else:
            value = values[0] if values else []
        self._start(self._write(variable, value, axes))
        return ''

    # DAQ and plotting

    @pyqtSlot(str, int)
    def set_acquisition(self, name: str, index: int):
        """0: poll the measurement; n: measure it by DAQ on the n-th event."""
        if index <= 0 or index > len(self._events):
            self._acquisition.pop(name, None)
        else:
            self._acquisition[name] = self._events[index - 1]['channel']
        self.variables_changed.emit()

    @pyqtSlot(result=str)
    def start_daq(self) -> str:
        if not self.connected:
            return 'Connect first'
        if self._measurement is not None:
            return 'DAQ is running'
        signals = []
        for name, event in self._acquisition.items():
            variable = self._variable(name)
            if variable is not None and variable.kind == 'MEASUREMENT':
                signals.append(DaqSignal(name, variable.address, variable.size, variable.extension, event))
        if not signals:
            return 'Choose an event instead of "Poll" for the measurements to measure by DAQ'

        async def run():
            try:
                self._measurement = await self._client.start_measurement(signals, self._on_sample)
                self._note(f'DAQ started: {len(signals)} measurements')
            except Exception as e:
                self._measurement = None
                self._note(f'DAQ: {e}', error=True)
            self.changed.emit()

        self._start(run())
        return ''

    def _on_sample(self, sample) -> None:
        for name, raw in sample.values.items():
            variable = self._variable(name)
            if variable is None:
                continue
            try:
                order = self._byte_order(variable)
                value = variable.decode_values(raw, order) if variable.shape else variable.decode(raw, order)
                self._set_value(variable, value, raw, sample.timestamp)
            except Exception as e:
                self._values[name] = {**self._values.get(name, {}), 'error': str(e)}
        if not self._refresh.isActive():
            self._refresh.start()

    @pyqtSlot()
    def stop_daq(self):
        if self._measurement is not None:
            self._start(self._stop_measurement())

    async def _stop_measurement(self):
        measurement, self._measurement = self._measurement, None
        if measurement is None:
            return
        try:
            await measurement.stop()
            self._note(
                f'DAQ stopped: {measurement.samples} samples'
                + (f', {measurement.lost} lost' if measurement.lost else '')
            )
        except Exception as e:
            self._note(f'Stopping DAQ: {e}', error=True)
        self.changed.emit()

    def _plot_key(self, name: str) -> str:
        return f'{self._protocol}.{name}'

    @pyqtSlot(str, bool)
    def set_plotted(self, name: str, plotted: bool):
        variable = self._variable(name)
        if variable is None:
            return
        signals = self.session.signals
        if plotted:
            self._plotted.add(name)
            signals.track_external(self._plot_key(name), variable.unit)
        else:
            self._plotted.discard(name)
            signals.untrack_key(self._plot_key(name))
        self._connection.tracked_signals_changed.emit()
        self.variables_changed.emit()

    def _unplot_all(self) -> None:
        if not self._plotted:
            return
        for name in self._plotted:
            for protocol in ('XCP', 'CCP'):
                self.session.signals.untrack_key(f'{protocol}.{name}')
        self._plotted.clear()
        self._connection.tracked_signals_changed.emit()

    # Pages and checksum

    @pyqtSlot(str, result=str)
    def set_page(self, page: str) -> str:
        """XCP: a page number (for the ECU and the XCP access); CCP: the start address of the page (hex)."""
        if not self.connected:
            return 'Connect first'
        try:
            value = int(page, 0) if self._protocol == 'XCP' else _hex(page, 0xFFFFFFFF, 'page address')
        except ValueError as e:
            return str(e)

        async def run():
            try:
                if isinstance(self._client, XcpClient):
                    await self._client.set_cal_page(value)
                    self._info['page'] = str(await self._client.get_cal_page())
                else:
                    await self._client.select_cal_page(value)
                    self._info['page'] = f'0x{(await self._client.get_active_cal_page())[1]:X}'
                self._note(f'Calibration page {self._info["page"]} active')
            except Exception as e:
                self._note(f'Page: {e}', error=True)
            self.changed.emit()

        self._start(run())
        return ''

    @pyqtSlot(int, int, result=str)
    def copy_page(self, source: int, destination: int) -> str:
        if not self.connected:
            return 'Connect first'
        if not isinstance(self._client, XcpClient):
            return 'Copying pages is an XCP command'

        async def run():
            try:
                await self._client.copy_cal_page(source, destination)
                self._note(f'Page {source} copied to page {destination}')
            except Exception as e:
                self._note(f'Copy page: {e}', error=True)

        self._start(run())
        return ''

    @pyqtSlot(str, str, result=str)
    def checksum(self, address: str, size: str) -> str:
        if not self.connected:
            return 'Connect first'
        try:
            start = _hex(address, 0xFFFFFFFF, 'address')
            length = int(str(size).strip(), 0)
        except ValueError as e:
            return str(e)
        if length <= 0:
            return 'Enter the number of bytes'

        async def run():
            try:
                if isinstance(self._client, XcpClient):
                    if length <= MAX_CHECKSUM_VERIFY:
                        data = await self._client.read(start, 0, length)
                        result = await self._client.verify_checksum(start, data)
                        verdict = {True: 'matches the uploaded data', False: 'DOES NOT match the uploaded data',
                                   None: 'not verifiable here'}[result['match']]  # fmt: skip
                        self._note(
                            f'Checksum {result["type"]} 0x{result["ecu"]:X}: {verdict}', result['match'] is False
                        )
                    else:
                        kind, value = await self._client.build_checksum(start, length)
                        self._note(f'Checksum type 0x{kind:02X}: 0x{value:X}')
                else:
                    value = await self._client.build_checksum(start, length)
                    self._note(f'Checksum (ECU specific algorithm): {format_hex(value)}')
            except Exception as e:
                self._note(f'Checksum: {e}', error=True)

        self._start(run())
        return ''

    # Datasets

    @pyqtSlot(QUrl, result=str)
    def save_dataset(self, url: QUrl) -> str:
        if not self.connected or self._document is None:
            return 'Connect and load the A2L first'
        path = url.toLocalFile() if url.isLocalFile() else url.toString()

        async def run():
            dataset, errors = await calibration.read_dataset(self._client, self._document)
            try:
                calibration.save_dataset(path, dataset, self._document)
                self._note(
                    f'Saved {len(dataset)} values to {Path(path).name}'
                    + (f', {len(errors)} unreadable' if errors else '')
                )
            except OSError as e:
                self._note(f'Save: {e}', error=True)

        self._start(run())
        return ''

    @pyqtSlot(QUrl, result=str)
    def load_dataset(self, url: QUrl) -> str:
        """Load a dataset (JSON or DCM) and compare it with the ECU."""
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            dataset = calibration.load_dataset(path)
        except Exception as e:
            return f'Could not read {Path(path).name}: {e}'
        self._dataset, self._dataset_file = dataset, path
        self._differences = []
        self.changed.emit()
        if self.connected and self._document is not None:
            self._start(self._compare())
        return ''

    async def _compare(self):
        names = [name for name in self._dataset if self._variable(name) is not None]
        ecu, errors = await calibration.read_dataset(self._client, self._document, names)
        self._differences = calibration.compare_datasets(self._dataset, ecu)
        unknown = len(self._dataset) - len(names)
        self._note(
            f'{Path(self._dataset_file).name}: {len(self._differences)} of {len(self._dataset)} values differ from the ECU'
            + (f' ({unknown} not in the A2L)' if unknown else '')
        )
        self.changed.emit()

    @pyqtSlot(result=str)
    def write_dataset(self) -> str:
        if self._dataset is None:
            return 'Load a dataset first'
        if not self.connected or self._document is None:
            return 'Connect and load the A2L first'

        async def run():
            errors = await calibration.write_dataset(self._client, self._document, self._dataset)
            for name, error in errors.items():
                self._note(f'{name}: {error}', error=True)
            self._note(f'Wrote {len(self._dataset) - len(errors)} values from {Path(self._dataset_file).name}')
            await self._compare()

        self._start(run())
        return ''

    # Reading and writing by address (no A2L)

    @pyqtSlot(str, int, result=str)
    def read_address(self, address: str, size: int) -> str:
        if not self.connected:
            return 'Connect first'
        try:
            extension, value = self._address(address)
        except ValueError as e:
            return str(e)

        async def run():
            try:
                raw = await self._client.read(value, extension, max(1, min(size, 64)))
                self._note(f'0x{value:X}: {format_hex(raw)}')
            except Exception as e:
                self._note(f'Read 0x{value:X}: {e}', error=True)

        self._start(run())
        return ''

    @staticmethod
    def _address(text: str):
        """'1000' or '1000:2' (address extension 2)."""
        address, _, extension = str(text).partition(':')
        return (int(extension, 0) if extension.strip() else 0), _hex(address, 0xFFFFFFFF, 'address')

    @pyqtSlot(str, str, result=str)
    def write_address(self, address: str, data: str) -> str:
        if not self.connected:
            return 'Connect first'
        try:
            extension, value = self._address(address)
            payload = parse_hex_bytes(data)
        except ValueError as e:
            return str(e)
        if not payload:
            return 'Enter the bytes to write, hex'

        async def run():
            try:
                await self._client.write(value, extension, payload)
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
        self._refresh.stop()
        self._polling = False
        self._close_transport()
