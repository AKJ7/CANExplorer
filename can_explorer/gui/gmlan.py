"""GUI controller for GMLAN (GMW3110) diagnostics of a connection, over ISO-TP (USDT) on CAN."""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.frames import format_hex, parse_can_id, parse_hex_bytes
from can_explorer.core.gmlan import (
    DEFAULT_REQUEST_ID,
    DEFAULT_RESPONSE_ID,
    DIAGNOSTIC_OPERATIONS,
    GmlanClient,
    GmlanDtc,
    GmlanError,
    GmlanKeepAlive,
    describe_request,
)
from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.uds import KEYSTORE_PREFIX, load_key_source
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_HISTORY = 200


def _parse_int(text: str, maximum: int, what: str) -> int:
    value = int(str(text).strip() or '0', 16)
    if not 0 <= value <= maximum:
        raise ValueError(f'The {what} must be between 0 and 0x{maximum:X}')
    return value


class GmlanController(QObject):
    """GMLAN diagnostics over ISO-TP on CAN: data identifiers, modes, security access and DTCs."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._request_id, self._response_id, self._extended = DEFAULT_REQUEST_ID, DEFAULT_RESPONSE_ID, False
        self._busy = False
        self._history: List[Dict[str, Any]] = []
        self._dtcs: List[Dict[str, str]] = []
        self._key_file = ''
        self._keep_alive: Optional[GmlanKeepAlive] = None
        self._keep_alive_transport = None
        # What this page has done to the ECU (cleared by ReturnToNormalMode or new addressing): the ECU itself also
        # leaves diagnostics when TesterPresent stops, which is not seen here
        self._operation = ''
        self._communication_disabled = False
        self._unlocked_level = 0

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

    @pyqtProperty(bool, notify=changed)
    def tester_present(self) -> bool:
        return self._keep_alive is not None and self._keep_alive.running

    @pyqtProperty(str, notify=changed)
    def key_file(self) -> str:
        return self._key_file

    @pyqtProperty('QVariantList', constant=True)
    def operations(self) -> List[Dict[str, Any]]:
        return [{'level': level, 'name': name} for level, name in DIAGNOSTIC_OPERATIONS.items()]

    @pyqtProperty(str, notify=changed)
    def request_id(self) -> str:
        return f'{self._request_id:0{8 if self._extended else 3}X}'

    @pyqtProperty(str, notify=changed)
    def response_id(self) -> str:
        return f'{self._response_id:0{8 if self._extended else 3}X}'

    @pyqtProperty(bool, notify=changed)
    def extended(self) -> bool:
        return self._extended

    @pyqtProperty(bool, notify=changed)
    def single_wire(self) -> bool:
        source = self.session.source if self.session is not None else None
        config = getattr(source, 'config', None)
        return bool(getattr(config, 'single_wire', False))

    @pyqtProperty(str, notify=changed)
    def operation(self) -> str:
        """The diagnostic operation last initiated (InitiateDiagnosticOperation), '' in normal mode."""
        return self._operation

    @pyqtProperty(bool, notify=changed)
    def communication_disabled(self) -> bool:
        """Whether normal communication was disabled (DisableNormalCommunication) since the last return to normal."""
        return self._communication_disabled

    @pyqtProperty(int, notify=changed)
    def unlocked_level(self) -> int:
        """The security level last unlocked, 0 if none since the last return to normal."""
        return self._unlocked_level

    def _reset_state(self):
        self._operation, self._communication_disabled, self._unlocked_level = '', False, 0

    @pyqtProperty(str, notify=changed)
    def addressing_text(self) -> str:
        width = 8 if self._extended else 3
        return f'request 0x{self._request_id:0{width}X}, response 0x{self._response_id:0{width}X}'

    def transport_error(self) -> str:
        if not self._connection.can_transmit:
            return 'This connection cannot send'
        if not self.session.is_open:
            return 'Connect first'
        return ''

    def _transport(self):
        return IsoTpClient(self.session, self._request_id, self._response_id, self._extended, fd=self._connection.fd)

    @pyqtSlot(str, str, bool, result=str)
    def set_addressing(self, request_id: str, response_id: str, extended: bool) -> str:
        try:
            request = parse_can_id(request_id, extended)
            response = parse_can_id(response_id, extended)
        except ValueError as e:
            return str(e)
        if (request, response, extended) != (self._request_id, self._response_id, self._extended):
            self._reset_state()  # Another ECU
        self._request_id, self._response_id, self._extended = request, response, extended
        self.changed.emit()
        return ''

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

    def _start(self, request: bytes, operation: Callable[[GmlanClient], Awaitable[tuple]]) -> str:
        if self._busy:
            return 'A request is already in progress'
        error = self.transport_error()
        if error:
            return error
        self._busy = True
        self.changed.emit()
        asyncio.ensure_future(self._run(request, operation))
        return ''

    async def _run(self, request: bytes, operation: Callable[[GmlanClient], Awaitable[tuple]]):
        transport = self._transport()
        try:
            response, description, positive = await operation(GmlanClient(transport))
            self._record(request, response, description, positive, error=False)
        except GmlanError as e:
            self._record(request, None, str(e), positive=False, error=True)
        except Exception as e:  # Transport errors
            logger.warning(f'GMLAN request failed: {e}')
            self._record(request, None, str(e), positive=False, error=True)
        finally:
            transport.close()
            self._busy = False
            self.changed.emit()

    def _service(
        self, request: bytes, describe: Callable[[bytes], str], on_success: Optional[Callable[[], None]] = None
    ) -> str:
        async def operation(client: GmlanClient):
            response = await client.call(request)
            if on_success is not None:
                on_success()
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
            return 'Enter the request bytes, e.g. 1A 90'

        async def operation(client: GmlanClient):
            result = await client.request(request)
            if result.error:
                raise GmlanError(result.error)
            return result.response, result.description, result.positive or result.response is None

        return self._start(request, operation)

    @pyqtSlot(int, result=str)
    def initiate_operation(self, level: int) -> str:
        name = DIAGNOSTIC_OPERATIONS.get(level, hex(level))

        def initiated():
            self._operation = name

        return self._service(bytes([0x10, level]), lambda _: f'InitiateDiagnosticOperation: {name}', initiated)

    @pyqtSlot(result=str)
    def return_to_normal(self) -> str:
        return self._service(bytes([0x20]), lambda _: 'Returned to normal mode', self._reset_state)

    @pyqtSlot(result=str)
    def disable_normal_communication(self) -> str:
        def disabled():
            self._communication_disabled = True

        return self._service(bytes([0x28]), lambda _: 'Normal communication disabled', disabled)

    @pyqtSlot(result=str)
    def report_programmed_state(self) -> str:
        return self._service(
            bytes([0xA2]), lambda response: f'Programmed state 0x{response[1]:02X}' if len(response) > 1 else 'No state'
        )

    @pyqtSlot(str, result=str)
    def read_did(self, did: str) -> str:
        try:
            value = _parse_int(did, 0xFF, 'data identifier')
        except ValueError as e:
            return str(e)
        return self._service(
            bytes([0x1A, value]), lambda response: f'DID 0x{value:02X} = {format_hex(bytes(response[2:]))}'
        )

    @pyqtSlot(str, str, result=str)
    def write_did(self, did: str, data: str) -> str:
        try:
            value = _parse_int(did, 0xFF, 'data identifier')
            payload = parse_hex_bytes(data)
        except ValueError as e:
            return str(e)
        if not payload:
            return 'Enter the data to write'
        return self._service(bytes([0x3B, value]) + payload, lambda _: f'DID 0x{value:02X} written')

    @pyqtSlot(QUrl)
    def set_key_file(self, url: QUrl):
        self._key_file = url.toLocalFile() if url.isLocalFile() else url.toString()
        self.changed.emit()

    @pyqtSlot(str)
    def set_key_entry(self, name: str):
        """Use a seed/key algorithm kept in the Security Manager's key store."""
        self._key_file = KEYSTORE_PREFIX + name
        self.changed.emit()

    @pyqtSlot(str)
    def set_key_source(self, source: str):
        """Use a key source: 'keystore:<entry>', 'profile:<name>' (a security profile) or a file path."""
        self._key_file = source
        self.changed.emit()

    @pyqtSlot(str, result=str)
    def security_access(self, level: str) -> str:
        try:
            value = _parse_int(level, 0xFF, 'security level')
        except ValueError as e:
            return str(e)
        if value % 2 == 0:
            return 'Security levels for requesting a seed are odd (1, 3, 5, …)'
        if not self._key_file:
            return 'Choose a key file first'
        try:
            compute_key = load_key_source(self._key_file)
        except Exception as e:
            return f'Could not load the key file: {e}'

        async def operation(client: GmlanClient):
            unlocked = await client.security_access(value, compute_key)
            self._unlocked_level = value
            return b'', f'Security level {value} unlocked' if unlocked else f'Level {value} was already unlocked', True

        return self._start(bytes([0x27, value]), operation)

    @pyqtSlot(result=str)
    def read_dtcs(self) -> str:
        async def operation(client: GmlanClient):
            dtcs: List[GmlanDtc] = await client.read_dtcs()
            self._dtcs = [{'code': dtc.text, 'status': f'0x{dtc.status:02X}'} for dtc in dtcs]
            return b'', f'{len(dtcs)} trouble code(s)', True

        return self._start(bytes([0xA9, 0x80, 0xFF]), operation)

    @pyqtSlot(result=str)
    def clear_dtcs(self) -> str:
        async def operation(client: GmlanClient):
            await client.clear_dtcs()
            self._dtcs = []
            return b'', 'Diagnostic information cleared', True

        return self._start(bytes([0x04]), operation)

    @pyqtSlot(result=str)
    def toggle_tester_present(self) -> str:
        if self.tester_present:
            self._stop_keep_alive()
            return ''
        error = self.transport_error()
        if error:
            return error
        self._keep_alive_transport = self._transport()
        self._keep_alive = GmlanKeepAlive(self._keep_alive_transport)
        self._keep_alive.start()
        self.changed.emit()
        return ''

    def _stop_keep_alive(self):
        if self._keep_alive is not None:
            self._keep_alive.stop()
            self._keep_alive = None
        if self._keep_alive_transport is not None:
            self._keep_alive_transport.close()
            self._keep_alive_transport = None
        self.changed.emit()

    @pyqtSlot()
    def clear_history(self):
        self._history = []
        self.changed.emit()

    def shutdown(self):
        self._stop_keep_alive()
