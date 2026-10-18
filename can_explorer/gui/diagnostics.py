import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.frames import format_hex, parse_can_id, parse_hex_bytes
from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.lin_tp import LinTpClient
from can_explorer.core.uds import (
    DATA_IDENTIFIERS,
    RESET_TYPES,
    ROUTINE_CONTROL_TYPES,
    SESSIONS,
    SessionKeepAlive,
    UdsClient,
    UdsError,
    describe_request,
    did_name,
    format_did_value,
    load_key_function,
)
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_HISTORY = 200


def _parse_int(text: str, maximum: int, what: str) -> int:
    try:
        value = int(text.strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


class DiagnosticsController(QObject):
    """UDS diagnostics of a connection: over ISO-TP on CAN, or over the LIN transport layer on LIN."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._request_id, self._response_id, self._extended, self._nad = 0x7E0, 0x7E8, False, 0x01
        self._busy = False
        self._history: List[Dict[str, Any]] = []
        self._dtcs: List[Dict[str, str]] = []
        self._key_file = ''
        self._keep_alive: Optional[SessionKeepAlive] = None
        self._keep_alive_transport = None

    @property
    def session(self):
        return self._connection.session

    @property
    def is_lin(self) -> bool:
        return self._connection.is_lin

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

    @pyqtProperty(bool, notify=changed)
    def tester_present(self) -> bool:
        return self._keep_alive is not None and self._keep_alive.running

    @pyqtProperty(str, notify=changed)
    def key_file(self) -> str:
        return self._key_file

    @pyqtProperty(str, constant=True)
    def transport_name(self) -> str:
        return 'LIN transport layer' if self.is_lin else 'ISO-TP'

    @pyqtProperty('QVariantList', constant=True)
    def known_dids(self) -> List[Dict[str, str]]:
        return [{'did': f'{did:04X}', 'name': name} for did, name in DATA_IDENTIFIERS.items()]

    @pyqtProperty('QVariantList', constant=True)
    def sessions(self) -> List[Dict[str, Any]]:
        return [{'value': value, 'name': name} for value, name in SESSIONS.items()]

    @pyqtProperty('QVariantList', constant=True)
    def reset_types(self) -> List[Dict[str, Any]]:
        return [{'value': value, 'name': name} for value, name in RESET_TYPES.items()]

    @pyqtProperty('QVariantList', constant=True)
    def routine_types(self) -> List[Dict[str, Any]]:
        return [{'value': value, 'name': name} for value, name in ROUTINE_CONTROL_TYPES.items()]

    # Addressing and transport

    @pyqtSlot(str, str, bool, str, result=str)
    def set_addressing(self, request_id: str, response_id: str, extended: bool, nad: str) -> str:
        """CAN: request and response identifiers (hex). LIN: node address (hex). Returns an error or ''."""
        try:
            if self.is_lin:
                value = _parse_int(nad, 0x7F, 'NAD')
                if value == 0:
                    raise ValueError('NAD 0x00 is reserved for sleep commands')
                self._nad = value
            else:
                self._request_id = parse_can_id(request_id, extended)
                self._response_id = parse_can_id(response_id, extended)
                self._extended = extended
        except ValueError as e:
            return str(e)
        self.set_tester_present(False)
        return ''

    def _transport(self):
        if self.is_lin:
            return LinTpClient(self.session, self._nad)
        return IsoTpClient(self.session, self._request_id, self._response_id, self._extended, fd=self._connection.fd)

    # Running requests

    def _record(self, request: bytes, response: Optional[bytes], description: str, positive: bool, error: bool):
        self._history.append(
            {
                'request': format_hex(request),
                'request_description': describe_request(request),
                'response': format_hex(response) if response else '',
                'description': description,
                'positive': positive,
                'error': error,
            }
        )
        del self._history[:-MAX_HISTORY]

    def _start(self, request: bytes, operation: Callable[[UdsClient], Awaitable[tuple]]) -> str:
        if self._busy:
            return 'A request is already in progress'
        if not self.session.is_open:
            return 'Connect first'
        self._busy = True
        self.changed.emit()
        asyncio.ensure_future(self._run(request, operation))
        return ''

    async def _run(self, request: bytes, operation: Callable[[UdsClient], Awaitable[tuple]]):
        transport = self._transport()
        try:
            response, description, positive = await operation(UdsClient(transport))
            self._record(request, response, description, positive, error=False)
        except UdsError as e:
            self._record(request, None, str(e), positive=False, error=True)
        except Exception as e:  # Transport errors
            logger.warning(f'Diagnostic request failed: {e}')
            self._record(request, None, str(e), positive=False, error=True)
        finally:
            transport.close()
            self._busy = False
            self.changed.emit()

    def _service(self, request: bytes, describe: Callable[[bytes], str]) -> str:
        async def operation(client: UdsClient):
            response = await client.call(request)
            return response, describe(response), True

        return self._start(request, operation)

    # Services

    @pyqtSlot(str, result=str)
    def raw_request(self, payload: str) -> str:
        try:
            request = parse_hex_bytes(payload)
        except ValueError as e:
            return str(e)
        if not request:
            return 'Enter the request bytes, e.g. 22 F1 90'

        async def operation(client: UdsClient):
            result = await client.request(request)
            if result.error:
                raise UdsError(result.error)
            return result.response, result.description, result.positive or result.response is None

        return self._start(request, operation)

    @pyqtSlot(int, result=str)
    def session_control(self, session: int) -> str:
        return self._service(bytes([0x10, session]), lambda _: f'{SESSIONS.get(session, hex(session))} session active')

    @pyqtSlot(int, result=str)
    def ecu_reset(self, reset_type: int) -> str:
        return self._service(bytes([0x11, reset_type]), lambda _: f'ECU reset ({RESET_TYPES.get(reset_type)})')

    @pyqtSlot(str, result=str)
    def read_did(self, did: str) -> str:
        try:
            value = _parse_int(did, 0xFFFF, 'data identifier')
        except ValueError as e:
            return str(e)
        return self._service(
            bytes([0x22, value >> 8, value & 0xFF]),
            lambda response: f'{did_name(value)} = {format_did_value(value, bytes(response[3:]))}',
        )

    @pyqtSlot(str, str, result=str)
    def write_did(self, did: str, data: str) -> str:
        try:
            value = _parse_int(did, 0xFFFF, 'data identifier')
            payload = parse_hex_bytes(data)
        except ValueError as e:
            return str(e)
        if not payload:
            return 'Enter the data to write'
        return self._service(bytes([0x2E, value >> 8, value & 0xFF]) + payload, lambda _: f'{did_name(value)} written')

    @pyqtSlot(QUrl)
    def set_key_file(self, url: QUrl):
        self._key_file = url.toLocalFile() if url.isLocalFile() else url.toString()
        self.changed.emit()

    @pyqtSlot(int, result=str)
    def security_access(self, level: int) -> str:
        if not self._key_file:
            return 'Choose a key file defining compute_key(level, seed)'
        try:
            compute_key = load_key_function(self._key_file)
        except UdsError as e:
            return str(e)

        async def operation(client: UdsClient):
            unlocked = await client.security_access(level, compute_key)
            text = f'Security level {level} {"unlocked" if unlocked else "was already unlocked"}'
            return None, text, True

        return self._start(bytes([0x27, level]), operation)

    @pyqtSlot(str, result=str)
    def read_dtcs(self, status_mask: str) -> str:
        try:
            mask = _parse_int(status_mask or 'FF', 0xFF, 'status mask')
        except ValueError as e:
            return str(e)
        request = bytes([0x19, 0x02, mask])

        async def operation(client: UdsClient):
            _, dtcs = await client.read_dtcs(mask)
            self._dtcs = [
                {'code': dtc.text, 'status': f'0x{dtc.status:02X}', 'flags': ', '.join(dtc.status_flags)}
                for dtc in dtcs
            ]
            return None, f'{len(dtcs)} DTC{"s" if len(dtcs) != 1 else ""} with status mask 0x{mask:02X}', True

        return self._start(request, operation)

    @pyqtSlot(result=str)
    def clear_dtcs(self) -> str:
        def describe(_):
            self._dtcs = []
            return 'Diagnostic information cleared'

        return self._service(bytes([0x14, 0xFF, 0xFF, 0xFF]), describe)

    @pyqtSlot(int, str, str, result=str)
    def routine_control(self, control_type: int, routine: str, data: str) -> str:
        try:
            identifier = _parse_int(routine, 0xFFFF, 'routine identifier')
            payload = parse_hex_bytes(data)
        except ValueError as e:
            return str(e)
        request = bytes([0x31, control_type, identifier >> 8, identifier & 0xFF]) + payload
        return self._service(
            request,
            lambda response: f'Routine 0x{identifier:04X} {ROUTINE_CONTROL_TYPES.get(control_type)}'
            + (f': {format_hex(response[4:])}' if len(response) > 4 else ''),
        )

    @pyqtSlot(bool)
    def set_tester_present(self, enabled: bool):
        if enabled and not self.tester_present and self.session.is_open:
            self._keep_alive_transport = self._transport()
            self._keep_alive = SessionKeepAlive(self._keep_alive_transport)
            self._keep_alive.start()
        elif not enabled and self._keep_alive is not None:
            self._keep_alive.stop()
            self._keep_alive_transport.close()
            self._keep_alive = self._keep_alive_transport = None
        self.changed.emit()

    @pyqtSlot()
    def clear_history(self):
        self._history.clear()
        self.changed.emit()

    def shutdown(self):
        self.set_tester_present(False)
