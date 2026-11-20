"""A measurement and calibration session with one ECU over XCP (CAN, CAN FD, UDP, TCP) or CCP, kept open between
calls and answering in plain JSON-serialisable data: what the MCP `xcp_*` tools use.
"""

import asyncio
import fnmatch
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from can_explorer.core import calibration
from can_explorer.core.a2l import A2lDocument, A2lVariable, load_a2l
from can_explorer.core.can_command import CanCommandClient
from can_explorer.core.ccp import CcpClient
from can_explorer.core.frames import parse_hex_bytes
from can_explorer.core.xcp import DaqSignal, XcpClient, resource_names
from can_explorer.core.xcp_ethernet import DEFAULT_PORT, XcpEthernetTransport

MAX_MEASURE_SECONDS = 30.0


class CalibrationSessionError(Exception):
    pass


def _hex_int(value: Any, what: str) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 16)
    except ValueError:
        raise CalibrationSessionError(f'Invalid hex {what}: {value!r}') from None


def describe_variable(variable: A2lVariable) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        'name': variable.name,
        'kind': variable.kind,
        'description': variable.description,
        'address': f'0x{variable.address:X}',
        'type': variable.data_type,
        'unit': variable.unit,
        'writable': variable.writable and not variable.unsupported,
    }
    if variable.extension:
        entry['extension'] = variable.extension
    if variable.shape:
        entry['shape'] = list(variable.shape)
    if variable.minimum is not None:
        entry['limits'] = [variable.minimum, variable.maximum]
    if variable.compu.is_verbal:
        entry['values'] = [text for _, _, text in variable.compu.verbal]
    if variable.unsupported:
        entry['unsupported'] = variable.unsupported
    return entry


class CalibrationSession:
    def __init__(self):
        self.client = None
        self.transport = None
        self.document: Optional[A2lDocument] = None
        self.protocol = ''

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    def load(self, path: str) -> Dict[str, Any]:
        self.document = load_a2l(path)
        variables = self.document.variables
        return {
            'a2l': str(path),
            'measurements': sum(1 for v in variables if v.kind == 'MEASUREMENT'),
            'characteristics': sum(1 for v in variables if v.kind != 'MEASUREMENT'),
            'epk': self.document.epk,
            'events': [{'channel': e.channel, 'name': e.name, 'cycle_s': e.cycle} for e in self.document.events],
        }

    def variables(self, pattern: str = '', kind: str = '', limit: int = 200) -> Dict[str, Any]:
        if self.document is None:
            raise CalibrationSessionError('Load an A2L file first (a2l=…)')
        selected = [
            v
            for v in self.document.variables
            if (not pattern or fnmatch.fnmatch(v.name.lower(), pattern.lower()) or pattern.lower() in v.name.lower())
            and (not kind or v.kind == kind.upper())
        ]
        return {'total': len(selected), 'variables': [describe_variable(v) for v in selected[: max(1, limit)]]}

    async def connect(
        self,
        session=None,
        protocol: str = 'XCP',
        transport: str = 'CAN',
        request: Any = '7E0',
        response: Any = '7E1',
        extended: bool = False,
        station: Any = '0',
        byte_order: str = 'big',
        host: str = '',
        port: int = DEFAULT_PORT,
        compute_key: Optional[Callable[[int, bytes], bytes]] = None,
        fd: bool = False,
    ) -> Dict[str, Any]:
        await self.disconnect()
        protocol = protocol.upper()
        transport = transport.upper()
        if protocol not in ('XCP', 'CCP'):
            raise CalibrationSessionError('The protocol is XCP or CCP')
        if transport in ('UDP', 'TCP'):
            if protocol != 'XCP':
                raise CalibrationSessionError('CCP runs on CAN only')
            if not host:
                raise CalibrationSessionError('Give the host of the XCP slave')
            self.transport = XcpEthernetTransport(host, int(port or DEFAULT_PORT), transport.lower())
            await self.transport.open()
        else:
            if session is None:
                raise CalibrationSessionError('Not connected to a bus')
            self.transport = CanCommandClient(
                session,
                _hex_int(request, 'request identifier'),
                _hex_int(response, 'response identifier'),
                extended,
                fd=fd or transport == 'CAN FD',
            )
        if protocol == 'CCP':
            self.client = CcpClient(self.transport, _hex_int(station, 'station address'), byte_order, compute_key)
        else:
            self.client = XcpClient(self.transport, compute_key=compute_key)
        self.protocol = protocol
        try:
            await self.client.connect()
        except Exception:
            self.close()
            raise
        return await self.status()

    async def status(self) -> Dict[str, Any]:
        if not self.connected:
            return {'connected': False, 'a2l': self.document.path if self.document else None}
        client = self.client
        result: Dict[str, Any] = {'connected': True, 'protocol': self.protocol, 'byte_order': client.byte_order}
        if isinstance(client, XcpClient):
            result.update(
                address_granularity=client.address_granularity,
                max_cto=client.max_cto,
                max_dto=client.max_dto,
                locked=resource_names(client.protection),
            )
        if self.document is not None:
            result['a2l'] = self.document.path
            try:
                result['epk'] = await calibration.check_epk(client, self.document)
            except Exception as e:
                result['epk'] = {'error': str(e)}
        return result

    def _require(self) -> None:
        if not self.connected:
            raise CalibrationSessionError('Not connected to an XCP/CCP slave; use xcp_connect first')

    def _variable(self, name: str) -> A2lVariable:
        if self.document is None:
            raise CalibrationSessionError('Load an A2L file first')
        variable = self.document.variable(name)
        if variable is None:
            raise CalibrationSessionError(f'{name} is not in the A2L')
        return variable

    async def read(self, names: List[str]) -> Dict[str, Any]:
        self._require()
        values: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        for name in names:
            try:
                values[name] = (
                    await calibration.read_value(self.client, self._variable(name), self.document)
                ).as_dict()
            except Exception as e:
                errors[name] = str(e)
        return {'values': values, 'errors': errors} if errors else {'values': values}

    async def read_memory(self, address: Any, size: int, extension: int = 0) -> Dict[str, Any]:
        self._require()
        if not 0 < size <= 4096:
            raise CalibrationSessionError('Read 1 to 4096 bytes')
        data = await self.client.read(_hex_int(address, 'address'), extension, size)
        return {'address': f'0x{_hex_int(address, "address"):X}', 'data': data.hex(' ').upper()}

    async def write(self, name: str, value: Any, axes: Optional[List] = None) -> Dict[str, Any]:
        self._require()
        variable = self._variable(name)
        if isinstance(value, str) and not variable.compu.is_verbal and variable.kind != 'ASCII':
            value = calibration.parse_value(variable, value)
        await calibration.write_value(self.client, variable, value, self.document, axes)
        return {'written': name, 'now': (await calibration.read_value(self.client, variable, self.document)).as_dict()}

    async def write_memory(self, address: Any, data: str, extension: int = 0) -> Dict[str, Any]:
        self._require()
        payload = parse_hex_bytes(data)
        if not payload:
            raise CalibrationSessionError('Give the bytes to write, hex')
        await self.client.write(_hex_int(address, 'address'), extension, payload)
        return {'written': len(payload), 'address': f'0x{_hex_int(address, "address"):X}'}

    async def measure(self, names: List[str], duration: float = 1.0, event: Optional[int] = None) -> Dict[str, Any]:
        """Measure measurements for `duration` seconds — by DAQ on `event` (the first A2L event by default), or by
        polling when DAQ is not available — and summarise each: samples, min, max, mean, last."""
        self._require()
        duration = max(0.05, min(float(duration), MAX_MEASURE_SECONDS))
        variables = [self._variable(name) for name in names]
        series: Dict[str, List[float]] = {v.name: [] for v in variables}

        def record(variable: A2lVariable, raw: bytes) -> None:
            value = variable.decode(raw, self._order(variable)) if not variable.shape else None
            if isinstance(value, (int, float)):
                series[variable.name].append(float(value))
            elif isinstance(value, str):
                series[variable.name].append(variable.raw_value(raw, self._order(variable)))

        method = 'polling'
        if event is None and self.document is not None and self.document.events:
            event = self.document.events[0].channel
        daq_variables = [v for v in variables if v.kind == 'MEASUREMENT' and not v.shape]
        measurement = None
        if daq_variables and len(daq_variables) == len(variables):
            by_name = {v.name: v for v in variables}

            def on_sample(sample) -> None:
                for key, raw in sample.values.items():
                    record(by_name[key], raw)

            try:
                signals = [DaqSignal(v.name, v.address, v.size, v.extension, event or 0) for v in daq_variables]
                measurement = await self.client.start_measurement(signals, on_sample)
                method = f'DAQ (event {event or 0})'
            except Exception:
                measurement = None
        deadline = time.monotonic() + duration
        if measurement is not None:
            await asyncio.sleep(duration)
            await measurement.stop()
        else:
            scalars = [v for v in variables if not v.shape and v.layout is None]
            others = [v for v in variables if v not in scalars]
            while time.monotonic() < deadline:
                raw = await calibration.read_many(self.client, scalars)
                for variable in scalars:
                    record(variable, raw[variable.name])
                for variable in others:
                    result = await calibration.read_value(self.client, variable, self.document)
                    if isinstance(result.value, (int, float)):
                        series[variable.name].append(float(result.value))
                await asyncio.sleep(0.01)
        summary = {}
        for variable in variables:
            values = series[variable.name]
            if not values:
                summary[variable.name] = {'samples': 0}
                continue
            summary[variable.name] = {
                'samples': len(values),
                'min': min(values),
                'max': max(values),
                'mean': sum(values) / len(values),
                'last': values[-1],
                'unit': variable.unit,
            }
        return {'method': method, 'duration_s': duration, 'values': summary}

    def _order(self, variable: A2lVariable) -> str:
        if variable.byte_order:
            return variable.byte_order
        if self.document is not None and self.document.byte_order:
            return self.document.byte_order
        return self.client.byte_order

    async def disconnect(self) -> None:
        if self.client is not None and self.client.connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.close()

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
        self.transport = None
        self.client = None


def a2l_path(path: str) -> str:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise CalibrationSessionError(f'No such A2L file: {path}')
    return str(resolved)
