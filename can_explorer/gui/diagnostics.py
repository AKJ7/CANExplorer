import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.diagnostic_description import DiagnosticDescription
from can_explorer.core.frames import format_hex, parse_can_id, parse_hex_bytes
from can_explorer.core.isotp_client import IsoTpClient, format_isotp_address, parse_isotp_address
from can_explorer.core.lin_tp import LinTpClient
from can_explorer.core.uds import (
    DATA_IDENTIFIERS,
    RESET_TYPES,
    ROUTINE_CONTROL_TYPES,
    KEYSTORE_PREFIX,
    SESSIONS,
    SessionKeepAlive,
    UdsClient,
    UdsError,
    describe_request,
    describe_response,
    did_name,
    format_did_value,
    load_key_source,
)
from can_explorer.core import audit, keystore, uds_auth, uds_dtc, uds_functional, uds_periodic, uds_secured
from can_explorer.core.uds_shared import SharedIsoTp
from can_explorer.core.uds_auth import UdsAuthenticationError
from can_explorer.core.uds_security_test import SecurityTestError
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_HISTORY = 200
PERIODIC_REFRESH_MS = 250
PERIODIC_RATES = [
    {'value': uds_periodic.SEND_AT_SLOW_RATE, 'name': 'Slow'},
    {'value': uds_periodic.SEND_AT_MEDIUM_RATE, 'name': 'Medium'},
    {'value': uds_periodic.SEND_AT_FAST_RATE, 'name': 'Fast'},
]

# The ReadDTCInformation reports of the Trouble codes pane: value -> (sub-function, name)
DTC_REPORTS = {
    'status': (uds_dtc.REPORT_BY_STATUS_MASK, 'By status mask (0x02)'),
    'supported': (uds_dtc.REPORT_SUPPORTED, 'Supported DTCs (0x0A)'),
    'counters': (uds_dtc.REPORT_FAULT_DETECTION_COUNTER, 'Fault detection counters (0x14)'),
    'memory': (uds_dtc.REPORT_USER_MEMORY_BY_STATUS_MASK, 'User-defined memory (0x17)'),
    'permanent': (uds_dtc.REPORT_WWH_OBD_PERMANENT, 'Permanent, WWH-OBD (0x55)'),
}


SESSION_SOURCE = 'session'  # Secured requests with the session key of the last authentication


def _parse_int(text: str, maximum: int, what: str) -> int:
    try:
        value = int(text.strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


class DiagnosticsController(QObject):
    """UDS diagnostics of a connection: over ISO-TP on CAN, or over the LIN transport layer on LIN.

    Other transports (DoIP) subclass it and override _transport(), _transport_error(), _addressing() and
    _addressing_text().
    """

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._request_id, self._response_id, self._extended, self._nad = 0x7E0, 0x7E8, False, 0x01
        # Extended/mixed addressing: the address byte starting every request / response frame, or None
        self._tx_address: Optional[int] = None
        self._rx_address: Optional[int] = None
        self._busy = False
        self._history: List[Dict[str, Any]] = []
        self._dtcs: List[Dict[str, str]] = []
        self._dtc_memory: Optional[int] = None  # The user-defined memory the listed DTCs came from
        self._key_file = ''
        self._authenticated = False
        self._description: Optional[DiagnosticDescription] = None
        self._keep_alive: Optional[SessionKeepAlive] = None
        self._keep_alive_transport = None
        # Listening between requests: one ISO-TP receiver shared by all users of the addressing (see uds_shared)
        self._listening = False
        self._shared: Optional[SharedIsoTp] = None
        # Periodic data (0x2A): the receiver, the PDIDs started and the CAN id of their UUDT frames (None: on the
        # diagnostic channel)
        self._periodic: Optional[uds_periodic.PeriodicDataReceiver] = None
        self._periodic_ids: List[int] = []
        self._periodic_id: Optional[int] = None
        self._periodic_dirty = False
        self._periodic_timer = QTimer(self)
        self._periodic_timer.setInterval(PERIODIC_REFRESH_MS)
        self._periodic_timer.timeout.connect(self._refresh_periodic)
        # SecuredDataTransmission (0x84): when on, the view's requests travel in envelopes (see set_secured)
        self._secured = False
        self._secured_provider = None
        self._secured_options = uds_secured.SecuredOptions(sign=False)
        self._secured_source = ''
        self._secured_counter = 0
        self._secured_length = 0
        # The session key of the last authentication (communicationConfiguration asking for one): its context and the
        # credential it was made with (the shared key entry or the profile that turns it into a key)
        self._session: Optional[uds_secured.SessionContext] = None
        self._session_source = ''

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
        return {'lin': 'LIN transport layer', 'can': 'ISO-TP', 'doip': 'DoIP'}[self._addressing()]

    @pyqtProperty(str, constant=True)
    def addressing(self) -> str:
        """'can' (request and response identifiers), 'lin' (NAD) or 'doip' (target address)."""
        return self._addressing()

    def _addressing(self) -> str:
        return 'lin' if self.is_lin else 'can'

    @pyqtProperty(str, notify=changed)
    def target(self) -> str:
        """DoIP: the target address (hex); '' for the other transports."""
        return self._target_text()

    def _target_text(self) -> str:
        return ''

    @pyqtProperty('QVariantList', notify=changed)
    def known_dids(self) -> List[Dict[str, str]]:
        """The ECU's identifiers from the loaded description first, then the standardised ones."""
        dids = dict(self._description.identifiers()) if self._description is not None else {}
        for did, name in DATA_IDENTIFIERS.items():
            dids.setdefault(did, name)
        return [{'did': f'{did:04X}', 'name': name} for did, name in dids.items()]

    @pyqtProperty(str, notify=changed)
    def description_file(self) -> str:
        return str(self._description.path) if self._description is not None else ''

    def _did_name(self, did: int) -> str:
        name = self._description.name(did) if self._description is not None else None
        return name or did_name(did)

    def _describe_did(self, did: int, data: bytes) -> str:
        decoded = self._description.describe(did, data) if self._description is not None else None
        return decoded or f'{did_name(did)} = {format_did_value(did, data)}'

    @pyqtSlot(QUrl, result=str)
    def load_description(self, url: QUrl) -> str:
        """Load an ECU diagnostic description (CANdelaStudio .cdd, ODX .odx/.odx-d or packaged ODX .pdx). Returns an error or ''."""
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            description = DiagnosticDescription(path)
        except Exception as e:
            return f'Could not load {Path(path).name}: {e}'
        self._set_description(description)
        logger.info(f'Loaded diagnostic description {path} with {len(description.identifiers())} DIDs')
        return ''

    @pyqtSlot()
    def unload_description(self):
        self._set_description(None)

    def _set_description(self, description: Optional[DiagnosticDescription]):
        self._description = description
        self._share_description(description)
        self.changed.emit()

    def _share_description(self, description: Optional[DiagnosticDescription]):
        """Let the trace decoders (UDS, LIN) name identifiers and decode their data too."""
        protocol = self.session.decoder.protocol
        if protocol is not None and hasattr(protocol, 'description'):
            protocol.description = description

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
        """CAN: request and response identifiers (hex), each optionally with an address byte for extended or
        mixed addressing ('6F1:12'). LIN: node address (hex). Returns an error or ''."""
        try:
            if self.is_lin:
                value = _parse_int(nad, 0x7F, 'NAD')
                if value == 0:
                    raise ValueError('NAD 0x00 is reserved for sleep commands')
                self._nad = value
            else:
                request, tx_address = parse_isotp_address(request_id, extended)
                response, rx_address = parse_isotp_address(response_id, extended)
                self._request_id, self._response_id, self._extended = request, response, extended
                self._tx_address, self._rx_address = tx_address, rx_address
        except ValueError as e:
            return str(e)
        self.set_tester_present(False)
        if self._shared is not None and not self._shared.busy:
            self._shared.close()  # Created again with the new identifiers
            self._shared = None
        return ''

    @pyqtProperty(str, notify=changed)
    def addressing_text(self) -> str:
        return self._addressing_text()

    def _addressing_text(self) -> str:
        if self.is_lin:
            return f'NAD 0x{self._nad:02X}'
        request = format_isotp_address(self._request_id, self._tx_address, self._extended)
        response = format_isotp_address(self._response_id, self._rx_address, self._extended)
        return f'request 0x{request}, response 0x{response}'

    def create_transport(self):
        """A transport with the current addressing, e.g. for flashing; the caller closes it. While listening it
        shares the view's ISO-TP receiver."""
        return self._transport()

    def transport_error(self) -> str:
        """Why requests cannot be sent now, or ''."""
        return self._transport_error()

    def _transport_error(self) -> str:
        if not self._connection.can_transmit:
            return 'This connection cannot send'
        if not self.session.is_open:
            return 'Connect first'
        return ''

    def _transport(self, receiving: bool = True):
        """A transport for one user; `receiving` False for send-only users (tester present). While listening, all
        share one ISO-TP receiver, so a flow control frame is never answered twice."""
        if self.is_lin:
            return LinTpClient(self.session, self._nad)
        if self._listening:
            return self._shared_isotp().transport(receiving)
        return IsoTpClient(
            self.session,
            self._request_id,
            self._response_id,
            self._extended,
            fd=self._connection.fd,
            tx_address=self._tx_address,
            rx_address=self._rx_address,
        )

    def _shared_isotp(self) -> SharedIsoTp:
        key = (
            self.session,
            self._request_id,
            self._response_id,
            self._extended,
            self._connection.fd,
            self._tx_address,
            self._rx_address,
        )
        if self._shared is None or not self._shared.matches(*key):
            if self._shared is not None:
                self._shared.close()
            self._shared = SharedIsoTp(*key, on_idle=self._on_unsolicited)
        return self._shared

    # Listening between requests and unsolicited responses

    @pyqtProperty(bool, constant=True)
    def listening_available(self) -> bool:
        """Listening needs ISO-TP on CAN (LIN slaves only answer polls; DoIP is not covered yet)."""
        return not self.is_lin and self._addressing() == 'can'

    @pyqtProperty(bool, notify=changed)
    def listening(self) -> bool:
        return self._listening

    @pyqtSlot(bool, result=str)
    def set_listening(self, enabled: bool) -> str:
        """Receive what the ECU sends between requests (event-triggered responses, periodic data, late answers).
        Off by default: it answers the flow control of segmented messages on the response id, which another tool on
        the same identifiers (e.g. the OBD page on 0x7E8) would answer too. Returns an error or ''."""
        if enabled == self._listening:
            return ''
        if enabled and not self.listening_available:
            return 'Listening between requests needs ISO-TP on CAN'
        if self._busy:
            return 'A request is in progress'
        if self._shared is not None and self._shared.busy:
            return 'The connection is in use (flashing or a scan)'
        keep_alive = self.tester_present
        self.set_tester_present(False)
        self._listening = enabled
        if enabled:
            self._shared_isotp()
        elif self._shared is not None:
            self._shared.close()
            self._shared = None
        if keep_alive:
            self.set_tester_present(True)
        self.changed.emit()
        return ''

    def _on_unsolicited(self, payload: bytes) -> None:
        """A message that answers no request: periodic data goes to the periodic panel, the rest to the History."""
        if self._periodic is not None and self._periodic_id is None and self._periodic.feed_usdt(payload):
            return
        negative = payload[:1] == bytes([0x7F])
        self._history.append(
            {
                'request': '',
                'request_description': 'Unsolicited (sent by the ECU)',
                'response': format_hex(payload),
                'description': describe_response(payload, self._description),
                'positive': not negative,
                'error': False,
                'unsolicited': True,
            }
        )
        del self._history[:-MAX_HISTORY]
        self.changed.emit()

    # Running requests

    def _record(self, request: bytes, response: Optional[bytes], description: str, positive: bool, error: bool):
        self._history.append(
            {
                'request': format_hex(request),
                'request_description': describe_request(request, self._description),
                'response': format_hex(response) if response else '',
                'description': description,
                'positive': positive,
                'error': error,
                'unsolicited': False,
            }
        )
        del self._history[:-MAX_HISTORY]

    def _start(self, request: bytes, operation: Callable[[UdsClient], Awaitable[tuple]]) -> str:
        if self._busy:
            return 'A request is already in progress'
        error = self._transport_error()
        if error:
            return error
        self._busy = True
        self.changed.emit()
        asyncio.ensure_future(self._run(request, operation))
        return ''

    async def _run(self, request: bytes, operation: Callable[[UdsClient], Awaitable[tuple]]):
        transport = self._transport()
        secured = None
        try:
            if self._secured:
                secured = uds_secured.SecuredTransport(
                    transport, self._secured_provider, self._secured_options, self._secured_counter
                )
            client = UdsClient(secured or transport)
            if not self.is_lin:
                client.add_unsolicited_listener(self._on_unsolicited)  # Messages that answer no request
            try:
                response, description, positive = await operation(client)
            finally:
                if secured is not None:
                    self._secured_counter = secured.next_counter
            if secured is not None and secured.last is not None:
                description = f'{description} ({secured.last.describe()})'
            self._record(request, response, description, positive, error=False)
        except (UdsError, UdsAuthenticationError, SecurityTestError, uds_secured.SecuredDataError) as e:
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
            description = describe_response(result.response, self._description) if result.response else None
            return result.response, description or result.description, result.positive or result.response is None

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
            lambda response: self._describe_did(value, bytes(response[3:])),
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
        return self._service(
            bytes([0x2E, value >> 8, value & 0xFF]) + payload, lambda _: f'{self._did_name(value)} written'
        )

    # SecuredDataTransmission (0x84)

    @pyqtProperty(bool, notify=changed)
    def secured(self) -> bool:
        return self._secured

    @pyqtProperty(int, notify=changed)
    def secured_counter(self) -> int:
        """The anti-replay counter of the next secured request."""
        return self._secured_counter

    @pyqtProperty(str, notify=changed)
    def secured_source(self) -> str:
        return self._secured_source

    @pyqtSlot('QVariantMap', result=str)
    def set_secured(self, options: Dict[str, Any]) -> str:
        """Send this view's requests inside SecuredDataTransmission envelopes (or stop). Options: enabled, source
        ('' unsigned, 'keystore:<entry>' a secured data key with the reference scheme, or a Python provider file),
        sign, encrypt, signed_response, algorithm (hex byte), length (signature bytes of the reference scheme, 0: the
        full MAC), counter (the next anti-replay counter, kept when absent). Returns an error, or ''."""
        enabled = bool(options.get('enabled', False))
        source = str(options.get('source') or '')
        try:
            algorithm = _parse_int(str(options.get('algorithm') or '0'), 0xFF, 'Algorithm')
            length = int(options.get('length') or 0)
            if 'counter' in options:
                self._secured_counter = int(options['counter']) % uds_secured.COUNTER_MODULO
            if enabled and source == SESSION_SOURCE:
                if self._session is None:
                    raise uds_secured.SecuredDataError(
                        'No session key: authenticate with the communication configuration that asks the ECU for one'
                    )
                provider = uds_secured.session_provider(self._session, self._session_source, length=length or None)
            else:
                provider = uds_secured.load_provider(source, length=length or None) if enabled else None
            secured_options = uds_secured.SecuredOptions(
                sign=bool(options.get('sign', True)) and provider is not None,
                encrypt=bool(options.get('encrypt', False)),
                signature_on_response=bool(options.get('signed_response', False)),
                algorithm=algorithm,
                pre_established_key=source == SESSION_SOURCE,
            )
            if enabled and (secured_options.encrypt or secured_options.signature_on_response) and provider is None:
                raise uds_secured.SecuredDataError('Encryption and signed responses need a key or provider file')
        except (ValueError, uds_secured.SecuredDataError) as e:
            self._secured = False
            self.changed.emit()
            return str(e)
        self._secured, self._secured_provider, self._secured_options = enabled, provider, secured_options
        self._secured_source, self._secured_length = source, length
        if enabled and source:
            audit.record('uds secured data transmission', source=source, encrypt=secured_options.encrypt)
        self.changed.emit()
        return ''

    @pyqtSlot(QUrl, result=str)
    def secured_provider_path(self, url: QUrl) -> str:
        """The local path of a chosen provider file."""
        return url.toLocalFile() if url.isLocalFile() else url.toString()

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
        """Use a seed/key key source: 'keystore:<entry>', 'profile:<name>' (a security profile) or a file path."""
        self._key_file = source
        self.changed.emit()

    @pyqtSlot(int, result=str)
    def security_access(self, level: int) -> str:
        if not self._key_file:
            return 'Choose a key algorithm: a file defining compute_key(level, seed), or one in the Security Manager'
        try:
            compute_key = load_key_source(self._key_file)
        except UdsError as e:
            return str(e)

        async def operation(client: UdsClient):
            unlocked = await client.security_access(level, compute_key)
            text = f'Security level {level} {"unlocked" if unlocked else "was already unlocked"}'
            return None, text, True

        return self._start(bytes([0x27, level]), operation)

    # Security access testing

    @pyqtSlot(int, int, str, result=str)
    def analyse_seeds(self, level: int, count: int, between: str) -> str:
        """Request `count` seeds of a level and report weaknesses (repeats, constant bytes, predictable steps)."""

        async def operation(client: UdsClient):
            report = await client.seed_analysis(level, count, between)
            return None, report.describe(), not report.weak

        return self._start(bytes([0x27, level]), operation)

    @pyqtSlot(int, int, bool, result=str)
    def test_lockout(self, level: int, max_attempts: int, test_reset: bool) -> str:
        """Send wrong keys until the ECU locks out, and check the delay (and whether a reset ends it)."""

        async def operation(client: UdsClient):
            report = await client.lockout_test(level, max_attempts, test_reset=test_reset)
            good = (
                report.locked_after is not None and bool(report.delay_enforced) and report.survives_reset is not False
            )
            return None, report.describe(), good

        return self._start(bytes([0x27, level]), operation)

    # Authentication (0x29)

    @pyqtProperty('QVariantList', constant=True)
    def signature_schemes(self) -> List[Dict[str, str]]:
        return [{'value': key, 'name': name} for key, name in uds_auth.SIGNATURE_SCHEMES.items()]

    @pyqtSlot('QVariantMap', result=str)
    def authenticate(self, options: Dict[str, Any]) -> str:
        """Authentication by certificate exchange. Options: certificate (a client certificate entry of the
        Security Manager, or 'profile:<name>'), scheme (proof-of-ownership signature), bidirectional, trusted (a trusted-certificates
        entry, checking the ECU's certificate). Returns an error, or ''."""
        store = keystore.default_store()
        try:
            # A key store entry, or 'profile:<name>': a security profile's certificate, signing with its key
            certificate, private_key = uds_auth.credentials(store, str(options.get('certificate') or ''))
            trusted = options.get('trusted') or ''
            trusted_certificates = uds_auth.trusted_certificates(store, str(trusted)) if trusted else []
            scheme = str(options.get('scheme') or uds_auth.scheme_for(certificate.public_key()))
            uds_auth.check_scheme(scheme, certificate.public_key())
        except uds_auth.UdsAuthenticationError as e:
            return str(e)
        bidirectional = bool(options.get('bidirectional', False))
        task = (
            uds_auth.VERIFY_CERTIFICATE_BIDIRECTIONAL if bidirectional else uds_auth.VERIFY_CERTIFICATE_UNIDIRECTIONAL
        )
        try:
            configuration = _parse_int(str(options.get('configuration') or '0'), 0xFF, 'communication configuration')
        except ValueError as e:
            return str(e)

        async def operation(client: UdsClient):
            self._authenticated = False  # The ECU ends an authentication when a new one starts
            self._end_session()
            result = await client.authenticate(
                certificate,
                private_key,
                scheme,
                bidirectional,
                trusted_certificates,
                store.revocation_lists(),
                communication_configuration=configuration,
            )
            self._authenticated = result.authenticated
            self._start_session(result, str(options.get('certificate') or ''))
            audit.record(
                'uds authentication', certificate=options.get('certificate'), authenticated=result.authenticated
            )
            text = result.description
            extensions = uds_auth.private_extensions(certificate)
            if extensions:  # Where the ECU may read the tester's role and white lists
                text += '; certificate extensions ' + ', '.join(f'{oid} ({size} B)' for oid, size in extensions)
            return None, text, result.authenticated

        return self._start(bytes([uds_auth.SID, task]), operation)

    @pyqtSlot('QVariantMap', result=str)
    def authenticate_challenge_response(self, options: Dict[str, Any]) -> str:
        """Authentication by challenge-response (0x05, then 0x06 or 0x07). Options: source (a shared key or client
        certificate entry of the Security Manager, or 'profile:<name>'), scheme (for signatures), bidirectional,
        trusted (a trusted-certificates entry checking the ECU's proof), algorithm (the algorithmIndicator: hex or
        a dotted OID), additional (the additional parameter, hex). Returns an error, or ''."""
        store = keystore.default_store()
        source = str(options.get('source') or '')
        bidirectional = bool(options.get('bidirectional', False))
        try:
            prove, verify_server, how = uds_auth.challenge_response_credentials(
                store, source, str(options.get('scheme') or ''), str(options.get('trusted') or '')
            )
            indicator = uds_auth.algorithm_indicator(str(options.get('algorithm') or ''))
            additional = parse_hex_bytes(str(options.get('additional') or ''))
            configuration = _parse_int(str(options.get('configuration') or '0'), 0xFF, 'communication configuration')
        except (uds_auth.UdsAuthenticationError, ValueError) as e:
            return str(e)

        async def operation(client: UdsClient):
            self._authenticated = False  # The ECU ends an authentication when a new one starts
            self._end_session()
            result = await client.authenticate_challenge_response(
                prove,
                bidirectional,
                verify_server,
                indicator=indicator,
                additional_parameter=additional,
                communication_configuration=configuration,
            )
            self._authenticated = result.authenticated
            self._start_session(result, source)
            audit.record(
                'uds authentication', source=source, method='challenge-response', authenticated=result.authenticated
            )
            return None, f'{result.description} (proof: {how})', result.authenticated

        return self._start(bytes([uds_auth.SID, uds_auth.REQUEST_CHALLENGE_FOR_AUTHENTICATION]), operation)

    @pyqtSlot(result=str)
    def deauthenticate(self) -> str:
        async def operation(client: UdsClient):
            text = await client.deauthenticate()
            self._authenticated = False
            self._end_session()
            return None, f'deAuthenticate: {text}', True

        return self._start(bytes([uds_auth.SID, uds_auth.DE_AUTHENTICATE]), operation)

    @pyqtSlot(str, str, result=str)
    def transmit_certificate(self, certificate: str, evaluation_id: str) -> str:
        """transmitCertificate (0x04): a certificate of the Security Manager for a certificateEvaluationId (hex).
        Returns an error, or ''."""
        try:
            value = uds_auth.certificate_to_transmit(keystore.default_store(), certificate)
            evaluation = _parse_int(evaluation_id, 0xFFFF, 'certificate evaluation id')
        except (uds_auth.UdsAuthenticationError, ValueError) as e:
            return str(e)

        async def operation(client: UdsClient):
            text = await client.transmit_certificate(value, evaluation)
            audit.record('uds certificate transmitted', certificate=certificate, evaluation_id=f'0x{evaluation:04X}')
            return None, f'transmitCertificate 0x{evaluation:04X} ({certificate}): {text}', True

        return self._start(bytes([uds_auth.SID, uds_auth.TRANSMIT_CERTIFICATE]), operation)

    @pyqtSlot(result=str)
    def authentication_configuration(self) -> str:
        async def operation(client: UdsClient):
            return None, f'authenticationConfiguration: {await uds_auth.authentication_configuration(client)}', True

        return self._start(bytes([uds_auth.SID, uds_auth.AUTHENTICATION_CONFIGURATION]), operation)

    # Session keys from Authentication, for SecuredDataTransmission

    @pyqtProperty(str, notify=changed)
    def session_key(self) -> str:
        """The session key information of the last authentication, described, or ''."""
        return self._session.describe() if self._session is not None else ''

    def _start_session(self, result, source: str) -> None:
        """Keep an authentication's session key information; secured requests using the session key switch to it."""
        if not (result.authenticated and result.session_key_info):
            return
        self._session, self._session_source = uds_secured.SessionContext.from_result(result), source
        if self._secured and self._secured_source == SESSION_SOURCE:
            try:
                self._secured_provider = uds_secured.session_provider(
                    self._session, source, length=self._secured_length or None
                )
            except uds_secured.SecuredDataError as e:
                self._secured = False
                logger.warning(f'Session key not usable: {e}')
        self.changed.emit()

    def _end_session(self) -> None:
        """An authentication ended (or a new one started): its session key is gone, and so are requests secured with it."""
        self._session, self._session_source = None, ''
        if self._secured and self._secured_source == SESSION_SOURCE:
            self._secured, self._secured_provider = False, None
        self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def authenticated(self) -> bool:
        """Whether the last authentication on this connection completed (and was not ended since)."""
        return self._authenticated

    @pyqtProperty('QVariantList', constant=True)
    def dtc_reports(self) -> List[Dict[str, str]]:
        return [{'value': key, 'name': name} for key, (_, name) in DTC_REPORTS.items()]

    @staticmethod
    def _dtc_row(dtc) -> Dict[str, str]:
        extra = []
        if dtc.severity is not None:
            extra.append(f'severity 0x{dtc.severity:02X}')
        if dtc.fault_detection_counter is not None:
            extra.append(f'fault detection counter {dtc.fault_detection_counter}')
        return {
            'code': dtc.text,
            'value': f'{dtc.code:06X}',
            'status': f'0x{dtc.status:02X}' if dtc.status is not None else '',
            'flags': ', '.join(dtc.status_flags),
            'extra': ', '.join(extra),
            'details': '',
        }

    @pyqtSlot(str, result=str)
    def read_dtcs(self, status_mask: str) -> str:
        return self.read_dtc_report('status', status_mask, '')

    @pyqtSlot(str, str, str, result=str)
    def read_dtc_report(self, report: str, status_mask: str, memory: str) -> str:
        """Read a ReadDTCInformation report (see DTC_REPORTS) into the Trouble codes pane. `status_mask` filters the
        status-mask reports, `memory` is the memory selection of the user-defined memory report (hex)."""
        if report not in DTC_REPORTS:
            return f'Unknown report {report!r}'
        subfunction, name = DTC_REPORTS[report]
        try:
            mask = _parse_int(status_mask or 'FF', 0xFF, 'status mask')
            selection = _parse_int(memory or '00', 0xFF, 'memory selection')
            request = uds_dtc.request(subfunction, status_mask=mask, memory_selection=selection)
        except (ValueError, UdsError) as e:
            return str(e)

        async def operation(client: UdsClient):
            if report == 'status':
                _, dtcs = await client.read_dtcs(mask)
                rows = [self._dtc_row(uds_dtc.DtcRecord(dtc.code, dtc.status)) for dtc in dtcs]
                text = f'{len(rows)} DTC{"s" if len(rows) != 1 else ""} with status mask 0x{mask:02X}'
            else:
                listed = await client.read_dtc_information(subfunction, status_mask=mask, memory_selection=selection)
                rows = [self._dtc_row(dtc) for dtc in listed.dtcs]
                text = f'{name}: {listed.describe()}'
            self._dtcs = rows
            self._dtc_memory = selection if report == 'memory' else None
            return None, text, True

        return self._start(request, operation)

    @pyqtSlot(str, result=str)
    def read_dtc_details(self, code: str) -> str:
        """Read a listed DTC's snapshot (freeze frame) and extended data records (all record numbers), decoding
        snapshot DIDs with the loaded diagnostic description; records of unknown length are shown in hex."""
        try:
            value = _parse_int(code, 0xFFFFFF, 'DTC')
        except ValueError as e:
            return str(e)
        memory = self._dtc_memory
        snapshot_sub = uds_dtc.REPORT_SNAPSHOT_BY_DTC if memory is None else uds_dtc.REPORT_USER_MEMORY_SNAPSHOT_BY_DTC
        extended_sub = (
            uds_dtc.REPORT_EXTENDED_DATA_BY_DTC if memory is None else uds_dtc.REPORT_USER_MEMORY_EXTENDED_DATA_BY_DTC
        )
        request = uds_dtc.request(snapshot_sub, dtc=value, memory_selection=memory or 0)

        async def operation(client: UdsClient):
            parts = []
            for subfunction in (snapshot_sub, extended_sub):
                try:
                    details = await client.read_dtc_information(
                        subfunction, dtc=value, memory_selection=memory or 0, description=self._description
                    )
                    parts.append(details.describe().split(': ', 1)[-1])
                except UdsError as e:  # E.g. the ECU keeps no snapshots: report it, try the other
                    parts.append(f'{uds_dtc.subfunction_name(subfunction)}: {e}')
            text = ' | '.join(parts)
            for row in self._dtcs:
                if row['value'] == f'{value:06X}':
                    row['details'] = text
            return None, f'{uds_dtc.DtcRecord(value).text}: {text}', True

        return self._start(request, operation)

    # Functional addressing

    def _functional_transport(self):
        """A functional transport for this connection (OBD conventions), or None where there is none."""
        if self.is_lin:
            return None
        if self._extended:
            return uds_functional.obd_29bit(self.session, tester=self._request_id & 0xFF, fd=self._connection.fd)
        return uds_functional.obd_11bit(self.session, fd=self._connection.fd)

    @pyqtProperty(bool, constant=True)
    def functional_available(self) -> bool:
        return not self.is_lin and self._addressing() != 'doip'

    @pyqtSlot(result=str)
    def discover_ecus(self) -> str:
        """Send a functional TesterPresent (0x7DF, or 0x18DB33F1 with 29-bit identifiers) and list the ECUs that
        answer, by response identifier."""
        if self._busy:
            return 'A request is already in progress'
        error = self._transport_error()
        if error:
            return error
        if not self.functional_available:
            return 'Functional requests are not available on this connection'
        self._busy = True
        self.changed.emit()
        asyncio.ensure_future(self._discover())
        return ''

    async def _discover(self):
        transport = self._functional_transport()
        request = bytes([0x3E, 0x00])
        try:
            responders = await uds_functional.FunctionalUdsClient(transport).discover()
            width = 8 if self._extended else 3
            names = ', '.join(f'0x{address:0{width}X}' for address in responders)
            text = f'{len(responders)} ECU{"s" if len(responders) != 1 else ""} answered' + (
                f': {names}' if names else ''
            )
            self._record(request, None, f'Functional TesterPresent: {text}', positive=bool(responders), error=False)
        except Exception as e:
            self._record(request, None, str(e), positive=False, error=True)
        finally:
            transport.close()
            self._busy = False
            self.changed.emit()

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
            lambda response: (
                f'Routine 0x{identifier:04X} {ROUTINE_CONTROL_TYPES.get(control_type)}'
                + (f': {format_hex(response[4:])}' if len(response) > 4 else '')
            ),
        )

    @pyqtSlot(bool)
    def set_tester_present(self, enabled: bool):
        if enabled and not self.tester_present and not self._transport_error():
            self._keep_alive_transport = self._transport(receiving=False)
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

    # Periodic data (ReadDataByPeriodicIdentifier 0x2A, DynamicallyDefineDataIdentifier 0x2C)

    periodic_changed = pyqtSignal()

    @pyqtProperty('QVariantList', constant=True)
    def periodic_rates(self) -> List[Dict[str, Any]]:
        return PERIODIC_RATES

    @pyqtProperty(bool, notify=changed)
    def periodic_running(self) -> bool:
        return bool(self._periodic_ids)

    @pyqtProperty('QVariantList', notify=periodic_changed)
    def periodic_rows(self) -> List[Dict[str, Any]]:
        receiver = self._periodic
        if receiver is None:
            return []
        rows = []
        for pdid, item in sorted(receiver.latest.items()):
            decoded = self._description.describe(item.did, item.data) if self._description is not None else None
            rows.append(
                {
                    'did': f'{item.did:04X}',
                    'name': self._did_name(item.did),
                    'data': format_hex(item.data),
                    'decoded': decoded or '',
                    'count': receiver.counts.get(pdid, 0),
                    'rate': f'{receiver.rate(pdid):.1f} /s',
                    'source': 'own CAN id' if item.source == 'uudt' else 'diagnostic channel',
                }
            )
        return rows

    def _refresh_periodic(self):
        if self._periodic_dirty:
            self._periodic_dirty = False
            self.periodic_changed.emit()

    def _on_periodic(self, _item) -> None:
        self._periodic_dirty = True

    @pyqtSlot(str, str, result=str)
    def define_dynamic(self, dddid: str, sources: str) -> str:
        """DynamicallyDefineDataIdentifier: DDDID (hex, e.g. F201) from `DID:position:size` sources or
        `@address:size` memory areas (see uds_periodic.parse_sources). Returns an error or ''."""
        try:
            identifier = _parse_int(dddid, 0xFFFF, 'dynamically defined DID')
            kind, items = uds_periodic.parse_sources(sources)
            if kind == 'did':
                request = uds_periodic.define_by_identifier_request(identifier, items)
            else:
                address_bytes = max(1, max((address.bit_length() + 7) // 8 for address, _ in items))
                size_bytes = max(1, max((size.bit_length() + 7) // 8 for _, size in items))
                request = uds_periodic.define_by_memory_address_request(identifier, items, address_bytes, size_bytes)
        except (ValueError, UdsError) as e:
            return str(e)
        what = 'DID' if kind == 'did' else 'memory'
        count = len(items)
        return self._service(request, lambda _: f'DID 0x{identifier:04X} defined from {count} {what} source(s)')

    @pyqtSlot(str, result=str)
    def clear_dynamic(self, dddid: str) -> str:
        """Clear one dynamically defined DID (hex), or all of them (empty)."""
        try:
            identifier = _parse_int(dddid, 0xFFFF, 'dynamically defined DID') if dddid.strip() else None
        except ValueError as e:
            return str(e)
        request = bytes([0x2C, 0x03]) + (identifier.to_bytes(2, 'big') if identifier is not None else b'')
        text = 'Dynamically defined DIDs cleared' if identifier is None else f'DID 0x{identifier:04X} cleared'
        return self._service(request, lambda _: text)

    @pyqtSlot(str, int, str, result=str)
    def start_periodic(self, identifiers: str, rate: int, can_id: str) -> str:
        """ReadDataByPeriodicIdentifier: send the DIDs (0xF2xx or their low byte, hex, separated by spaces) at the
        rate (1 slow, 2 medium, 3 fast). `can_id` (hex) is the CAN id of the ECU's periodic frames; empty: they come
        on the diagnostic response id as 0x6A messages, which needs listening between requests (turned on).
        Returns an error or ''."""
        if not self.listening_available:
            return 'Periodic data needs ISO-TP on CAN'
        if self._busy:
            return 'A request is already in progress'
        try:
            tokens = identifiers.replace(',', ' ').split()
            pdids = [uds_periodic.periodic_id(_parse_int(token, 0xFFFF, 'periodic DID')) for token in tokens]
            request = uds_periodic.read_periodic_request(rate, pdids)
            frame_id = parse_can_id(can_id, self._extended) if can_id.strip() else None
        except (ValueError, UdsError) as e:
            return str(e)
        if frame_id is None:
            error = self.set_listening(True)
            if error:
                return error
        self._stop_receiver()
        receiver = uds_periodic.PeriodicDataReceiver(description=self._description, on_data=self._on_periodic)
        if frame_id is not None:
            receiver.attach(self.session, frame_id, self._extended)
        self._periodic, self._periodic_id = receiver, frame_id

        async def operation(client: UdsClient):
            await client.call(request)
            self._periodic_ids = pdids
            self._periodic_timer.start()
            names = ', '.join(f'0x{0xF200 | pdid:04X}' for pdid in pdids)
            where = f'CAN id 0x{frame_id:X}' if frame_id is not None else 'the diagnostic channel'
            return None, f'Periodic data {uds_periodic.TRANSMISSION_MODES[rate]}: {names}, on {where}', True

        self.periodic_changed.emit()
        return self._start(request, operation)

    @pyqtSlot(result=str)
    def stop_periodic(self) -> str:
        """stopSending for the started DIDs; the last values stay listed."""
        request = uds_periodic.read_periodic_request(uds_periodic.STOP_SENDING, list(self._periodic_ids))

        async def operation(client: UdsClient):
            try:
                await client.call(request)
            finally:
                self._periodic_ids = []
                self._periodic_timer.stop()
                if self._periodic is not None:
                    self._periodic.close()  # Stops receiving frames; the values stay
                self._periodic_dirty = True
                self._refresh_periodic()
            return None, 'Periodic data stopped', True

        return self._start(request, operation)

    def _stop_receiver(self) -> None:
        if self._periodic is not None:
            self._periodic.close()
        self._periodic = None
        self._periodic_ids = []
        self._periodic_timer.stop()

    def shutdown(self):
        self.set_tester_present(False)
        self._stop_receiver()
        if self._shared is not None:
            self._shared.close()
            self._shared = None
