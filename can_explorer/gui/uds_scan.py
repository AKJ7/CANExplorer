import asyncio
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.frames import format_hex, parse_hex_bytes
from can_explorer.core.uds import UdsClient, did_name, service_name
from can_explorer.core import uds_fuzz, uds_scan
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_RESULTS = 2000  # Rows kept for the table; the counters still count every probe
REFRESH_MS = 100
STATE_CHANGING_KINDS = (uds_scan.SWEEP, uds_scan.FUZZ)  # Kinds the GUI only runs once state changes are allowed


def _hex(text: str, maximum: int, what: str, default: Optional[int] = None) -> int:
    text = str(text).strip() if text is not None else ''
    if not text and default is not None:
        return default
    try:
        value = int(text, 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


class UdsScanController(QObject):
    """Interactive UDS discovery for a connection: scan the services, DIDs, subfunctions and routines an ECU
    exposes, or sweep one byte of a request. Runs over the connection's diagnostics transport and addressing."""

    changed = pyqtSignal()
    results_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._diagnostics = connection.diagnostics
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._results: List[Dict[str, Any]] = []
        self._scanned = 0
        self._found = 0
        self._total = 0
        self._truncated = False
        self._state = ''  # '', 'running', 'done', 'stopped', 'failed'
        self._kind = uds_scan.DATA_IDENTIFIERS
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._flush)

    # Properties

    @pyqtProperty(bool, notify=changed)
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @pyqtProperty(str, notify=changed)
    def state(self) -> str:
        return self._state

    @pyqtProperty(str, notify=changed)
    def kind(self) -> str:
        return self._kind

    @pyqtProperty(int, notify=changed)
    def scanned(self) -> int:
        return self._scanned

    @pyqtProperty(int, notify=changed)
    def total(self) -> int:
        return self._total

    @pyqtProperty(int, notify=changed)
    def found(self) -> int:
        return self._found

    @pyqtProperty(float, notify=changed)
    def progress(self) -> float:
        return self._scanned / self._total if self._total else 0.0

    @pyqtProperty(bool, notify=results_changed)
    def truncated(self) -> bool:
        """Whether more rows were found than the table keeps (MAX_RESULTS)."""
        return self._truncated

    @pyqtProperty('QVariantList', notify=results_changed)
    def results(self) -> List[Dict[str, Any]]:
        return list(self._results)

    @pyqtProperty('QVariantList', constant=True)
    def kinds(self) -> List[Dict[str, str]]:
        return [
            {'value': uds_scan.SERVICES, 'name': 'Services'},
            {'value': uds_scan.DATA_IDENTIFIERS, 'name': 'Data identifiers (0x22)'},
            {'value': uds_scan.SUBFUNCTIONS, 'name': 'Subfunctions of a service'},
            {'value': uds_scan.ROUTINES, 'name': 'Routines (0x31)'},
            {'value': uds_scan.SWEEP, 'name': 'Sweep one byte of a request'},
            {'value': uds_scan.FUZZ, 'name': 'Fuzz (mutate requests)'},
        ]

    @pyqtProperty(str, notify=changed)
    def transport_name(self) -> str:
        return self._diagnostics.transport_name

    # Running

    @pyqtSlot('QVariantMap', result=str)
    def start(self, options: Dict[str, Any]) -> str:
        """Start a scan. Options: kind, start, end (hex), service (hex), control_type, template (hex), position,
        width, delay_ms, timeout_ms, enter_session (hex or ''), allow_state_changing. Returns an error or ''."""
        if self.running:
            return 'A scan is already running'
        error = self._diagnostics.transport_error()
        if error:
            return error
        if self._diagnostics.busy:
            return 'A diagnostic request is in progress'
        try:
            plan = self._plan(options)
        except ValueError as e:
            return str(e)
        if plan['kind'] in STATE_CHANGING_KINDS and not options.get('allow_state_changing', False):
            return 'This scan can change the ECU state; enable "Allow state-changing probes" to run it'
        self._kind = plan['kind']
        self._results = []
        self._scanned = self._found = 0
        self._total = plan['total']
        self._truncated = False
        self._state = 'running'
        self._stop = asyncio.Event()
        self._task = asyncio.ensure_future(self._run(plan))
        self._timer.start()
        self.changed.emit()
        self.results_changed.emit()
        return ''

    def _plan(self, options: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(options.get('kind') or uds_scan.DATA_IDENTIFIERS)
        delay = max(0, int(options.get('delay_ms', 0) or 0)) / 1000
        timeout = max(20, int(options.get('timeout_ms', 200) or 200)) / 1000
        enter = options.get('enter_session') or ''
        session = _hex(enter, 0x7F, 'session') if str(enter).strip() else None
        plan: Dict[str, Any] = {'kind': kind, 'delay': delay, 'timeout': timeout, 'session': session}

        if kind == uds_scan.SERVICES:
            services = uds_scan.KNOWN_SERVICES if options.get('known_only', True) else range(0x00, 0x100)
            plan['maker'] = lambda client: uds_scan.scan_services(client, services, delay=delay, stop=self._stop)
            plan['total'] = len(list(services))
        elif kind == uds_scan.DATA_IDENTIFIERS:
            start = _hex(options.get('start'), 0xFFFF, 'start DID', 0x0000)
            end = _hex(options.get('end'), 0xFFFF, 'end DID', 0xFFFF)
            self._check_range(start, end)
            plan['maker'] = lambda client: uds_scan.scan_data_identifiers(
                client, start, end, delay=delay, stop=self._stop
            )
            plan['total'] = end - start + 1
        elif kind == uds_scan.SUBFUNCTIONS:
            service = _hex(options.get('service'), 0xFF, 'service', 0x10)
            start = _hex(options.get('start'), 0xFF, 'start subfunction', 0x00)
            end = _hex(options.get('end'), 0xFF, 'end subfunction', 0x7F)
            self._check_range(start, end)
            plan['maker'] = lambda client: uds_scan.scan_subfunctions(
                client, service, start, end, delay=delay, stop=self._stop
            )
            plan['total'] = end - start + 1
        elif kind == uds_scan.ROUTINES:
            start = _hex(options.get('start'), 0xFFFF, 'start routine', 0x0000)
            end = _hex(options.get('end'), 0xFFFF, 'end routine', 0xFFFF)
            self._check_range(start, end)
            control = int(options.get('control_type', 0x03) or 0x03)
            plan['maker'] = lambda client: uds_scan.scan_routines(
                client, start, end, control, delay=delay, stop=self._stop
            )
            plan['total'] = end - start + 1
        elif kind == uds_scan.SWEEP:
            template = parse_hex_bytes(str(options.get('template') or ''))
            if not template:
                raise ValueError('Enter the request template, e.g. 22 F1 90')
            position = int(options.get('position', 0) or 0)
            width = int(options.get('width', 1) or 1)
            start = _hex(options.get('start'), 0xFFFF, 'start value', 0x00)
            end = _hex(options.get('end'), 0xFFFF, 'end value', 0xFF if width == 1 else 0xFFFF)
            self._check_range(start, end)
            if position < 0 or position + width > len(template):
                raise ValueError('The swept position is outside the template')
            if not 1 <= width <= 2:
                raise ValueError('Sweep width must be 1 or 2 bytes')
            plan['maker'] = lambda client: uds_scan.sweep(
                client, template, position, start, end, width, delay=delay, stop=self._stop
            )
            plan['total'] = end - start + 1
        elif kind == uds_scan.FUZZ:
            seeds = uds_fuzz.parse_seeds(str(options.get('template') or ''))
            count = max(1, int(options.get('count', 500) or 500))
            fuzz_seed_text = str(options.get('fuzz_seed') or '').strip()
            try:
                fuzz_seed = int(fuzz_seed_text) if fuzz_seed_text else None
            except ValueError:
                raise ValueError(f'The reproducibility seed must be a number: {fuzz_seed_text!r}') from None
            liveness = max(0, int(options.get('liveness_every', uds_fuzz.DEFAULT_LIVENESS_EVERY) or 0))
            plan['maker'] = lambda client: uds_fuzz.fuzz(
                client, seeds, count, seed=fuzz_seed, delay=delay, liveness_every=liveness, stop=self._stop
            )
            plan['total'] = count
        else:
            raise ValueError(f'Unknown scan kind {kind!r}')
        return plan

    @staticmethod
    def _check_range(start: int, end: int) -> None:
        if end < start:
            raise ValueError('The end of the range is before its start')

    async def _run(self, plan: Dict[str, Any]) -> None:
        transport = self._diagnostics.create_transport()
        client = UdsClient(transport, timeout=plan['timeout'])
        try:
            if plan['session'] is not None:
                await client.diagnostic_session_control(plan['session'])
            async for result in plan['maker'](client):
                self._scanned += 1
                if result.interesting:
                    self._found += 1
                    self._add(result)
                self._dirty = True
            self._state = 'done' if not self._stop.is_set() else 'stopped'
        except asyncio.CancelledError:
            self._state = 'stopped'
            raise
        except Exception as e:
            logger.warning(f'UDS scan failed: {e}')
            self._state = 'failed'
        finally:
            transport.close()
            self._timer.stop()
            self._flush()
            self.changed.emit()

    def _add(self, result: uds_scan.ScanResult) -> None:
        if len(self._results) >= MAX_RESULTS:
            self._truncated = True
            return
        self._results.append(
            {
                'identifier': self._label(result),
                'request': format_hex(result.request),
                'response': format_hex(result.response) if result.response else '',
                'category': result.category,
                'summary': result.summary,
            }
        )

    def _label(self, result: uds_scan.ScanResult) -> str:
        if result.kind == uds_scan.SERVICES:
            return f'{service_name(result.identifier)} (0x{result.identifier:02X})'
        if result.kind == uds_scan.DATA_IDENTIFIERS:
            return f'0x{result.identifier:04X} {did_name(result.identifier)}'
        if result.kind == uds_scan.ROUTINES:
            return f'Routine 0x{result.identifier:04X}'
        if result.kind == uds_scan.SUBFUNCTIONS:
            return f'Subfunction 0x{result.identifier:02X}'
        return format_hex(result.request)

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.results_changed.emit()
            self.changed.emit()

    @pyqtSlot()
    def stop(self):
        self._stop.set()

    @pyqtSlot()
    def clear(self):
        if self.running:
            return
        self._results = []
        self._scanned = self._found = self._total = 0
        self._truncated = False
        self._state = ''
        self.results_changed.emit()
        self.changed.emit()

    @pyqtSlot(str, result=str)
    def inspect(self, request_hex: str) -> str:
        """Re-send one found request through the Diagnostics view, so it appears in its history."""
        return self._diagnostics.raw_request(request_hex)

    def shutdown(self):
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._timer.stop()
