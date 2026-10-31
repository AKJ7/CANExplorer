"""GUI controller for SAE J1850 VPW / GM Class 2 diagnostics of a connection (the "elm327_vpw" interface).

Diagnostics run over the connection's own session (an `Elm327VpwBus`), so the single ELM327 adapter is shared
between the live trace and these requests. The service layer is SAE J1979 (OBD-II) with GM Class 2 addressing:
current data (PIDs), stored trouble codes, the VIN and raw requests.
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.elm327_vpw import SessionJ1850Transport
from can_explorer.core.frames import format_hex, parse_hex_bytes
from can_explorer.core.j1850 import J1850Client, J1850Error, describe_payload
from can_explorer.core.reverse.obd import PIDS, PIDS_BY_NUMBER
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_HISTORY = 200


def _parse_int(text: str, maximum: int, what: str) -> int:
    value = int(str(text).strip() or '0', 16)
    if not 0 <= value <= maximum:
        raise ValueError(f'The {what} must be between 0 and 0x{maximum:X}')
    return value


class J1850Controller(QObject):
    """J1850 VPW / GM Class 2 (SAE J1979) diagnostics over the connection's session."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._busy = False
        self._history: List[Dict[str, Any]] = []
        self._dtcs: List[Dict[str, str]] = []
        self._vin = ''

    @property
    def session(self):
        return self._connection.session

    # Properties

    @pyqtProperty(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty('QVariantList', notify=changed)
    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    @pyqtProperty('QVariantList', notify=changed)
    def dtcs(self) -> List[Dict[str, str]]:
        return list(self._dtcs)

    @pyqtProperty(str, notify=changed)
    def vin(self) -> str:
        return self._vin

    @pyqtProperty('QVariantList', constant=True)
    def pids(self) -> List[Dict[str, Any]]:
        return [{'number': f'{pid.number:02X}', 'name': pid.name, 'unit': pid.unit} for pid in PIDS]

    def transport_error(self) -> str:
        if not self._connection.can_transmit:
            return 'This connection cannot send'
        if not self.session.is_open:
            return 'Connect first'
        return ''

    # Running requests

    def _record(self, request: bytes, response: Optional[bytes], description: str, positive: bool, error: bool):
        self._history.append(
            {
                'request': format_hex(request),
                'request_description': describe_payload(request),
                'response': format_hex(response) if response else '',
                'description': description,
                'positive': positive,
                'error': error,
            }
        )
        del self._history[:-MAX_HISTORY]

    def _start(self, request: bytes, operation: Callable[[J1850Client], Awaitable[tuple]]) -> str:
        if self._busy:
            return 'A request is already in progress'
        error = self.transport_error()
        if error:
            return error
        self._busy = True
        self.changed.emit()
        asyncio.ensure_future(self._run(request, operation))
        return ''

    async def _run(self, request: bytes, operation: Callable[[J1850Client], Awaitable[tuple]]):
        transport = SessionJ1850Transport(self.session)
        try:
            response, description, positive = await operation(J1850Client(transport))
            self._record(request, response, description, positive, error=False)
        except J1850Error as e:
            self._record(request, None, str(e), positive=False, error=True)
        except Exception as e:  # Transport errors
            logger.warning(f'J1850 request failed: {e}')
            self._record(request, None, str(e), positive=False, error=True)
        finally:
            transport.close()
            self._busy = False
            self.changed.emit()

    # Services

    @pyqtSlot(str, result=str)
    def read_pid(self, pid: str) -> str:
        try:
            number = _parse_int(pid, 0xFF, 'PID')
        except ValueError as e:
            return str(e)

        async def operation(client: J1850Client):
            data = await client.request_pid(number)
            definition = PIDS_BY_NUMBER.get(number)
            if definition is not None and len(data) >= definition.length:
                value = definition.decode(data[: definition.length])
                description = f'{definition.name}: {value:g} {definition.unit}'.strip()
            else:
                description = f'PID 0x{number:02X} = {format_hex(data)}'
            return bytes([0x41, number]) + data, description, True

        return self._start(bytes([0x01, number]), operation)

    @pyqtSlot(result=str)
    def read_vin(self) -> str:
        async def operation(client: J1850Client):
            vin = await client.vehicle_info(0x02)
            self._vin = vin.decode('ascii', 'replace').strip()
            return bytes([0x49, 0x02]) + vin, f'VIN: {self._vin}', True

        return self._start(bytes([0x09, 0x02]), operation)

    @pyqtSlot(result=str)
    def read_dtcs(self) -> str:
        async def operation(client: J1850Client):
            codes = await client.read_dtcs()
            self._dtcs = [{'code': code} for code in codes]
            return b'', f'{len(codes)} trouble code(s)', True

        return self._start(bytes([0x03]), operation)

    @pyqtSlot(result=str)
    def clear_dtcs(self) -> str:
        async def operation(client: J1850Client):
            await client.clear_dtcs()
            self._dtcs = []
            return b'', 'Diagnostic information cleared', True

        return self._start(bytes([0x04]), operation)

    @pyqtSlot(str, result=str)
    def raw_request(self, payload: str) -> str:
        try:
            request = parse_hex_bytes(payload)
        except ValueError as e:
            return str(e)
        if not request:
            return 'Enter the request bytes, e.g. 01 0C'

        async def operation(client: J1850Client):
            result = await client.request(request)
            if result.error:
                raise J1850Error(result.error)
            return result.response, result.description, result.positive or result.response is None

        return self._start(request, operation)

    @pyqtSlot()
    def clear_history(self):
        self._history = []
        self.changed.emit()

    def shutdown(self):
        pass
