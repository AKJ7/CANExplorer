"""GUI controller of the VW TP 2.0 / KWP2000 page: a channel to one module, the module scan, measuring blocks (read
once or live), coding records edited field by field, security access and raw requests.

The channel is opened once and kept: TP 2.0 channel tests keep the channel open and `KwpKeepAlive` the diagnostic
session. Module names, measuring-block scaling and coding records come from a label file the user loads
(`core/diagnostic_labels.py`); none is built in. All logic lives in `core/` (`tp20`, `tp20_scan`, `kwp2000_client`,
`measuring_blocks`, `coding`); this controller only runs it and keeps what the view shows.
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.coding import CodingError, CodingRecord, field_changes, write_record
from can_explorer.core.diagnostic_labels import LabelError, LabelFile
from can_explorer.core.frames import format_hex, parse_hex_bytes
from can_explorer.core.kwp2000_client import KEEP_ALIVE_INTERVAL, KwpClient, KwpError, KwpKeepAlive
from can_explorer.core.measuring_blocks import MeasuringBlock, MeasuringBlockError, read_measuring_block
from can_explorer.core.protocols.kwp2000 import describe_payload
from can_explorer.core.tp20 import Tp20Client, Tp20Error
from can_explorer.core.tp20_scan import parse_addresses, scan_modules
from can_explorer.core.uds import UdsError, load_key_source
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_HISTORY = 200
LIVE_INTERVAL = 0.5  # Seconds between live measuring-block reads
SESSION_KEEP_ALIVE = KEEP_ALIVE_INTERVAL  # Idle seconds before TesterPresent keeps the session open
ERRORS = (KwpError, Tp20Error, CodingError, MeasuringBlockError, LabelError, UdsError, ValueError, OSError)


def _hex_byte(text: str, what: str) -> Optional[int]:
    """A byte from hex text, or None for an empty field."""
    text = str(text).strip().lower().removeprefix('0x')
    if not text:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        raise ValueError(f'The {what} is a hex byte, e.g. 01') from None
    if not 0 <= value <= 0xFF:
        raise ValueError(f'The {what} must be 00-FF')
    return value


class Tp20Controller(QObject):
    """VW TP 2.0 / KWP2000 diagnostics over the connection's session."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self._labels: Optional[LabelFile] = None
        self._client: Optional[Tp20Client] = None
        self._kwp: Optional[KwpClient] = None
        self._keep_alive: Optional[KwpKeepAlive] = None
        self._module = 0x01
        self._busy = False
        self._error = ''
        self._history: List[Dict[str, Any]] = []
        # Scan
        self._scan_task: Optional[asyncio.Task] = None
        self._scan_results: List[Dict[str, Any]] = []
        self._scan_position = 0
        self._scan_total = 0
        # Measuring blocks
        self._block: Optional[MeasuringBlock] = None
        self._live_task: Optional[asyncio.Task] = None
        # Coding
        self._record: Optional[CodingRecord] = None
        self._coding_read: Optional[bytes] = None  # As read from the module
        self._coding_edit: Optional[bytes] = None  # As edited
        self._coding_backup: Optional[bytes] = None  # Before the last write
        self._coding_status = ''

    @property
    def session(self):
        return self._connection.session

    def _notify(self) -> None:
        self.changed.emit()

    # Properties: channel and labels

    @pyqtProperty(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        """The last background error (a failed live read, a channel the module closed)."""
        return self._error

    @pyqtProperty(bool, notify=changed)
    def channel_open(self) -> bool:
        return self._client is not None and self._client.is_open

    @pyqtProperty(str, notify=changed)
    def module(self) -> str:
        return f'{self._module:02X}'

    @pyqtProperty(str, notify=changed)
    def module_name(self) -> str:
        return self._labels.module_name(self._module) if self._labels else ''

    @pyqtProperty(str, notify=changed)
    def channel_text(self) -> str:
        if not self.channel_open:
            return ''
        return (
            f'module 0x{self._module:02X}: tester on 0x{self._client.tx_id:03X}, module on 0x{self._client.rx_id:03X}'
        )

    @pyqtProperty(str, notify=changed)
    def labels_name(self) -> str:
        return self._labels.path.name if self._labels and self._labels.path else ''

    @pyqtProperty('QVariantList', notify=changed)
    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    def transport_error(self) -> str:
        if not self._connection.can_transmit:
            return 'This connection cannot send'
        if self.session is None or not self.session.is_open:
            return 'Connect first'
        return ''

    # Running operations

    def _record_history(self, request: bytes, response: Optional[bytes], description: str, positive: bool) -> None:
        self._history.append(
            {
                'request': format_hex(request),
                'request_description': describe_payload(request),
                'response': format_hex(response) if response else '',
                'description': description,
                'positive': positive,
            }
        )
        del self._history[:-MAX_HISTORY]

    def _start(self, operation: Callable[[], Awaitable[None]], needs_channel: bool = True) -> str:
        if self._busy:
            return 'An operation is already in progress'
        error = self.transport_error()
        if error:
            return error
        if needs_channel and not self.channel_open:
            return 'Open a channel to the module first'
        self._busy = True
        self._error = ''
        self._notify()
        asyncio.ensure_future(self._run(operation))
        return ''

    async def _run(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
        except ERRORS as e:
            self._error = str(e)
        except Exception as e:  # Unexpected: keep the page usable
            logger.warning(f'TP 2.0 operation failed: {e}')
            self._error = str(e) or type(e).__name__
        finally:
            self._busy = False
            self._notify()

    # Labels

    @pyqtSlot(QUrl, result=str)
    def load_labels(self, url: QUrl) -> str:
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            self._labels = LabelFile.load(path)
        except LabelError as e:
            return str(e)
        if self._block is not None:
            self._block = self._labels.formula_table(self._module).apply(self._strip(self._block))
        self._notify()
        return ''

    @pyqtSlot()
    def unload_labels(self) -> None:
        self._labels = None
        self._notify()

    @staticmethod
    def _strip(block: MeasuringBlock) -> MeasuringBlock:
        from dataclasses import replace

        return replace(block, values=tuple(replace(v, name='', value=None, unit='', error='') for v in block.values))

    # Channel

    @pyqtSlot(str, str, result=str)
    def open_channel(self, module: str, session: str) -> str:
        if self.channel_open:
            return 'A channel is already open; close it first'
        if self._scan_task is not None:
            return 'Wait for the scan to finish'
        try:
            address = _hex_byte(module, 'module address')
            session_byte = _hex_byte(session, 'session')
        except ValueError as e:
            return str(e)
        if address is None:
            return 'Enter the module address, e.g. 01'

        async def operation():
            self._module = address
            self._clear_module_state()
            client = Tp20Client(self.session, address)
            await client.open()
            self._client = client
            self._kwp = KwpClient(client)
            if session_byte is not None:
                request = bytes([0x10, session_byte])
                result = await self._kwp.request(request)
                self._record_history(request, result.response, result.description, result.positive)
                if not result.positive:
                    self._error = f'The channel is open, but the session was not started: {result.description}'
                    return
            self._keep_alive = KwpKeepAlive(self._kwp, SESSION_KEEP_ALIVE)
            self._keep_alive.start()

        return self._start(operation, needs_channel=False)

    def _clear_module_state(self) -> None:
        self._block = None
        self._record = None
        self._coding_read = self._coding_edit = self._coding_backup = None
        self._coding_status = ''

    @pyqtSlot()
    def close_channel(self) -> None:
        self._stop_live()
        if self._keep_alive is not None:
            self._keep_alive.stop()
            self._keep_alive = None
        client, self._client, self._kwp = self._client, None, None
        if client is not None:
            client.close()  # Sends the disconnect synchronously; no background task to outlive the page
        self._notify()

    # Scan

    @pyqtProperty(bool, notify=changed)
    def scanning(self) -> bool:
        return self._scan_task is not None

    @pyqtProperty(str, notify=changed)
    def scan_progress(self) -> str:
        if not self._scan_total:
            return ''
        found = len(self._scan_results)
        return f'{self._scan_position} of {self._scan_total} addresses, {found} module(s) found'

    @pyqtProperty('QVariantList', notify=changed)
    def scan_results(self) -> List[Dict[str, Any]]:
        return list(self._scan_results)

    @pyqtSlot(str, str, str, result=str)
    def scan(self, addresses: str, identification: str, session: str) -> str:
        if self._scan_task is not None:
            return 'A scan is already running'
        error = self.transport_error()
        if error:
            return error
        if self.channel_open:
            return 'Close the channel first: the scan opens channels of its own'
        try:
            targets = parse_addresses(addresses)
            option = _hex_byte(identification, 'identification option')
            session_byte = _hex_byte(session, 'session') if option is not None else None
        except ValueError as e:
            return str(e)
        self._scan_results = []
        self._scan_position, self._scan_total = 0, len(targets)
        self._error = ''
        task = asyncio.ensure_future(self._run_scan(targets, option, session_byte))
        # A done callback, not a finally: a task cancelled before it starts never runs its body
        task.add_done_callback(self._scan_done)
        self._scan_task = task
        self._notify()
        return ''

    async def _run_scan(self, targets: List[int], option: Optional[int], session_byte: Optional[int]) -> None:
        try:
            results = scan_modules(
                self.session, targets, identification=option, diagnostic_session=session_byte, labels=self._labels
            )
            async for result in results:
                self._scan_position += 1
                if result.found:
                    entry = result.as_dict()
                    entry.setdefault('name', '')
                    entry.setdefault('identification_text', '')
                    entry.setdefault('error', '')
                    self._scan_results.append(entry)
                self._notify()
        except ERRORS as e:
            self._error = str(e)

    def _scan_done(self, task: asyncio.Task) -> None:
        if self._scan_task is task:
            self._scan_task = None
        self._notify()

    @pyqtSlot()
    def stop_scan(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()

    # Measuring blocks

    @pyqtProperty('QVariantMap', notify=changed)
    def block(self) -> Dict[str, Any]:
        if self._block is None:
            return {}
        values = [{'name': v.name, 'text': v.text, 'raw': v.raw, 'error': v.error} for v in self._block.values]
        return {'group': self._block.group, 'values': values, 'unparsed': format_hex(self._block.rest)}

    @pyqtProperty(bool, notify=changed)
    def live(self) -> bool:
        return self._live_task is not None

    async def _read_block(self, group: int) -> None:
        table = self._labels.formula_table(self._module) if self._labels else None
        self._block = await read_measuring_block(self._kwp, group, table)

    @staticmethod
    def _group(text: str) -> int:
        try:
            group = int(str(text).strip(), 10)
        except ValueError:
            raise ValueError('The group is a number, 0-255') from None
        if not 0 <= group <= 255:
            raise ValueError('The group is a number, 0-255')
        return group

    @pyqtSlot(str, result=str)
    def read_group(self, group: str) -> str:
        try:
            number = self._group(group)
        except ValueError as e:
            return str(e)
        return self._start(lambda: self._read_block(number))

    @pyqtSlot(bool, str, result=str)
    def set_live(self, on: bool, group: str) -> str:
        if not on:
            self._stop_live()
            self._notify()
            return ''
        if not self.channel_open:
            return 'Open a channel to the module first'
        try:
            number = self._group(group)
        except ValueError as e:
            return str(e)
        self._stop_live()
        self._live_task = asyncio.ensure_future(self._run_live(number))
        self._notify()
        return ''

    async def _run_live(self, group: int) -> None:
        try:
            while self.channel_open:
                try:
                    await self._read_block(group)
                except ERRORS as e:
                    self._error = str(e)
                    return
                self._notify()
                await asyncio.sleep(LIVE_INTERVAL)
        finally:
            # Let a cancel propagate (cancel-safe), but do not clear a newer live task started since
            if self._live_task is asyncio.current_task():
                self._live_task = None
            self._notify()

    def _stop_live(self) -> None:
        if self._live_task is not None:
            self._live_task.cancel()
            self._live_task = None

    # Coding

    @pyqtProperty('QVariantList', notify=changed)
    def coding_records(self) -> List[str]:
        return [r.name for r in self._labels.coding_records(self._module)] if self._labels else []

    @pyqtProperty(str, notify=changed)
    def coding_record(self) -> str:
        return f'{self._record.name} ({self._record.location()})' if self._record else ''

    @pyqtProperty(str, notify=changed)
    def coding_hex(self) -> str:
        return format_hex(self._coding_edit) if self._coding_edit is not None else ''

    @pyqtProperty(str, notify=changed)
    def coding_original_hex(self) -> str:
        return format_hex(self._coding_read) if self._coding_read is not None else ''

    @pyqtProperty(str, notify=changed)
    def coding_backup_hex(self) -> str:
        return format_hex(self._coding_backup) if self._coding_backup is not None else ''

    @pyqtProperty(bool, notify=changed)
    def coding_modified(self) -> bool:
        return self._coding_edit is not None and self._coding_edit != self._coding_read

    @pyqtProperty(str, notify=changed)
    def coding_status(self) -> str:
        return self._coding_status

    @pyqtProperty('QVariantList', notify=changed)
    def coding_fields(self) -> List[Dict[str, Any]]:
        if self._record is None or self._coding_edit is None:
            return []
        original = {v.field.name: v.value for v in self._record.decode(self._coding_read or b'')}
        fields = []
        for value in self._record.decode(self._coding_edit):
            definition = value.field
            options = [{'value': v, 'text': f'{v} — {name}'} for v, name in sorted(definition.values.items())]
            fields.append(
                {
                    'name': definition.name,
                    'location': definition.location(),
                    'value': value.value,
                    'text': value.text,
                    'maximum': definition.maximum,
                    'options': options,
                    'description': definition.description,
                    'changed': original.get(definition.name) != value.value,
                }
            )
        return fields

    @pyqtProperty('QVariantList', notify=changed)
    def coding_changes(self) -> List[str]:
        """The edits as text, for the confirmation before writing."""
        if not self.coding_modified:
            return []
        if self._record.fields:
            lines = [
                f'{f.name}: {f.text(before)} → {f.text(after)}'
                for f, before, after in field_changes(self._record, self._coding_read, self._coding_edit)
            ]
            if lines:
                return lines
        return [f'{format_hex(self._coding_read)} → {format_hex(self._coding_edit)}']

    def _resolve_record(self, name: str, location: str) -> CodingRecord:
        if str(location).strip():
            protocol, colon, identifier = str(location).strip().partition(':')
            if not colon:
                identifier, protocol = protocol, 'kwp'
            if protocol.strip().lower() != 'kwp':
                raise CodingError('This page reads KWP2000 records (local identifiers); use MCP for UDS records')
            value = _hex_byte(identifier, 'local identifier')
            if value is None:
                raise CodingError('Enter the local identifier, e.g. 9A')
            return CodingRecord(f'local identifier 0x{value:02X}', 'kwp', value)
        if self._labels is None:
            raise CodingError('Load a label file, or enter the local identifier of the record')
        record = self._labels.coding_record(self._module, name)
        if record.protocol != 'kwp':
            raise CodingError(f'{record.name} is a UDS record; this page reads KWP2000 records (use MCP for UDS)')
        return record

    @pyqtSlot(str, str, result=str)
    def read_coding(self, name: str, location: str) -> str:
        try:
            record = self._resolve_record(name, location)
        except ValueError as e:  # CodingError, LabelError
            return str(e)

        async def operation():
            data = await record.read(self._kwp)
            self._record, self._coding_read, self._coding_edit = record, data, data
            self._coding_status = f'Read {len(data)} bytes'

        return self._start(operation)

    @pyqtSlot(str, int, result=str)
    def set_field(self, name: str, value: int) -> str:
        if self._record is None or self._coding_edit is None:
            return 'Read the coding first'
        try:
            self._coding_edit = self._record.apply(self._coding_edit, {name: int(value)})
        except CodingError as e:
            return str(e)
        self._notify()
        return ''

    @pyqtSlot(str, result=str)
    def set_coding_bytes(self, text: str) -> str:
        if self._record is None or self._coding_read is None:
            return 'Read the coding first'
        try:
            data = parse_hex_bytes(text)
        except ValueError as e:
            return str(e)
        if len(data) != len(self._coding_read):
            return f'The record is {len(self._coding_read)} bytes, got {len(data)}'
        self._coding_edit = data
        self._notify()
        return ''

    @pyqtSlot()
    def revert_coding(self) -> None:
        self._coding_edit = self._coding_read
        self._notify()

    @pyqtSlot(result=str)
    def write_coding(self) -> str:
        if self._record is None or self._coding_read is None:
            return 'Read the coding first'
        if not self.coding_modified:
            return 'Nothing changed'
        record, before, target = self._record, self._coding_read, self._coding_edit

        async def operation():
            result = await write_record(self._kwp, record, target, expected_before=before)
            self._coding_backup = result.before
            if result.verified:
                self._coding_read = self._coding_edit = result.read_back
                self._coding_status = 'Written and read back: the module kept the new coding'
            else:
                kept = format_hex(result.read_back) if result.read_back is not None else 'nothing'
                self._coding_read = self._coding_edit = result.read_back or before
                self._coding_status = f'Written, but the module reads back {kept}: it did not keep the new coding'
            logger.info(
                f'Coding of module 0x{self._module:02X} {record.name}: {format_hex(before)} -> {format_hex(target)}'
            )

        return self._start(operation)

    # Security access and raw requests

    @pyqtSlot(str, str, result=str)
    def security_access(self, level: str, source: str) -> str:
        try:
            number = int(str(level).strip() or '1', 16)
            if not 1 <= number <= 0x7F or number % 2 == 0:
                return 'The level is odd, 01-7F'
            source = str(source).strip()
            if not source:
                return 'Choose a key file (compute_key) or enter keystore:<entry>'
            if source.startswith('file://'):
                source = QUrl(source).toLocalFile()
            compute_key = load_key_source(source)
        except (ValueError, UdsError) as e:
            return str(e)

        async def operation():
            unlocked = await self._kwp.security_access(number, compute_key)
            description = f'Level {number} unlocked' if unlocked else f'Level {number} was already unlocked'
            self._record_history(bytes([0x27, number]), None, description, True)

        return self._start(operation)

    @pyqtSlot(str, result=str)
    def raw_request(self, payload: str) -> str:
        try:
            request = parse_hex_bytes(payload)
        except ValueError as e:
            return str(e)
        if not request:
            return 'Enter the request bytes, e.g. 21 01'

        async def operation():
            result = await self._kwp.request(request)
            self._record_history(request, result.response, result.description, result.positive)

        return self._start(operation)

    @pyqtSlot()
    def clear_history(self) -> None:
        self._history = []
        self._notify()

    def shutdown(self) -> None:
        self.stop_scan()
        self._stop_live()
        if self._keep_alive is not None:
            self._keep_alive.stop()
            self._keep_alive = None
        if self._client is not None:
            self._client.close()  # Sends the disconnect without waiting
            self._client = None
