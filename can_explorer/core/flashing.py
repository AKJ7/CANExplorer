"""ECU flashing over UDS (ISO 14229): RequestDownload (0x34), TransferData (0x36) and RequestTransferExit (0x37),
from Intel HEX, Motorola S-record or binary files.

The programming sequence is a list of steps: by default built from `FlashOptions` (programming session, security
access, erase and download per segment, check routine, reset — optionally framed by the pre-programming steps that
quiet the rest of the bus), or loaded from a JSON **sequence preset** (`load_sequence`) for a manufacturer's own
order of steps. Steps marked `functional` go to all ECUs through a functional client (core/uds_functional.py).
"""

import asyncio
import importlib.util
import json
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from can_explorer.core.frames import parse_hex_bytes
from can_explorer.core.uds import (
    COMMUNICATION_CONTROL_TYPES,
    SESSIONS,
    SessionKeepAlive,
    UdsClient,
    UdsError,
)
from can_explorer.core.uds_file_transfer import transfer_blocks

INTEL_HEX_SUFFIXES = ('.hex', '.ihex', '.ihx', '.h86')
SREC_SUFFIXES = ('.s19', '.s28', '.s37', '.srec', '.mot', '.s', '.sx', '.mhx')
ISOTP_MAX_PAYLOAD = 4095  # Classic ISO-TP; longer blocks need ISO-TP with escape sequence (CAN FD)


class ImageError(ValueError):
    pass


@dataclass
class Segment:
    address: int
    data: bytes

    @property
    def end(self) -> int:
        return self.address + len(self.data)


@dataclass
class FlashImage:
    segments: List[Segment]
    start_address: Optional[int] = None
    path: Optional[Path] = None
    header: str = ''  # S0 record of S-record files

    @property
    def size(self) -> int:
        return sum(len(segment.data) for segment in self.segments)

    @property
    def crc32(self) -> int:
        crc = 0
        for segment in self.segments:
            crc = zlib.crc32(segment.data, crc)
        return crc

    def describe(self) -> str:
        ranges = ', '.join(f'0x{segment.address:08X}–0x{segment.end - 1:08X}' for segment in self.segments)
        return f'{self.size} bytes in {len(self.segments)} segment{"s" if len(self.segments) != 1 else ""}: {ranges}'


def _merge(chunks: Dict[int, bytes], fill_gap: int = 0) -> List[Segment]:
    """Contiguous segments from (address, bytes) chunks; gaps up to fill_gap bytes are filled with 0xFF."""
    segments: List[Segment] = []
    current: Optional[Tuple[int, bytearray]] = None
    for address in sorted(chunks):
        data = chunks[address]
        if current is not None:
            end = current[0] + len(current[1])
            if address < end:
                raise ImageError(f'Overlapping data at 0x{address:08X}')
            if address - end <= fill_gap:
                current[1].extend(b'\xff' * (address - end) + data)
                continue
            segments.append(Segment(current[0], bytes(current[1])))
        current = (address, bytearray(data))
    if current is not None:
        segments.append(Segment(current[0], bytes(current[1])))
    return segments


def parse_intel_hex(text: str, fill_gap: int = 0) -> FlashImage:
    chunks: Dict[int, bytes] = {}
    base = 0
    start = None
    ended = False
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if ended:
            raise ImageError(f'Line {number}: data after the end of file record')
        if not line.startswith(':'):
            raise ImageError(f'Line {number}: Intel HEX records start with ":"')
        try:
            record = bytes.fromhex(line[1:])
        except ValueError:
            raise ImageError(f'Line {number}: invalid hex digits') from None
        if len(record) < 5 or len(record) != record[0] + 5:
            raise ImageError(f'Line {number}: wrong record length')
        if sum(record) & 0xFF:
            raise ImageError(f'Line {number}: checksum error')
        kind, offset, data = record[3], int.from_bytes(record[1:3], 'big'), record[4:-1]
        if kind == 0x00:
            if base + offset in chunks:
                raise ImageError(f'Line {number}: address 0x{base + offset:08X} written twice')
            chunks[base + offset] = data
        elif kind == 0x01:
            ended = True
        elif kind == 0x02:
            base = int.from_bytes(data, 'big') << 4
        elif kind == 0x04:
            base = int.from_bytes(data, 'big') << 16
        elif kind == 0x03:
            start = (int.from_bytes(data[:2], 'big') << 4) + int.from_bytes(data[2:], 'big')
        elif kind == 0x05:
            start = int.from_bytes(data, 'big')
        else:
            raise ImageError(f'Line {number}: unknown record type {kind:02X}')
    if not ended:
        raise ImageError('Missing end of file record')
    return FlashImage(_merge(chunks, fill_gap), start)


def parse_srec(text: str, fill_gap: int = 0) -> FlashImage:
    chunks: Dict[int, bytes] = {}
    start = None
    header = ''
    data_records = 0
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if len(line) < 4 or line[0] not in 'Ss' or not line[1].isdigit():
            raise ImageError(f'Line {number}: S-records start with S0 to S9')
        kind = int(line[1])
        try:
            record = bytes.fromhex(line[2:])
        except ValueError:
            raise ImageError(f'Line {number}: invalid hex digits') from None
        if not record or len(record) != record[0] + 1:
            raise ImageError(f'Line {number}: wrong record length')
        if (sum(record[:-1]) & 0xFF) ^ 0xFF != record[-1]:
            raise ImageError(f'Line {number}: checksum error')
        address_size = {0: 2, 1: 2, 2: 3, 3: 4, 5: 2, 6: 3, 7: 4, 8: 3, 9: 2}.get(kind)
        if address_size is None:
            raise ImageError(f'Line {number}: unknown record type S{kind}')
        address = int.from_bytes(record[1 : 1 + address_size], 'big')
        data = record[1 + address_size : -1]
        if kind == 0:
            header = data.decode('latin-1').rstrip('\x00')
        elif kind in (1, 2, 3):
            if address in chunks:
                raise ImageError(f'Line {number}: address 0x{address:08X} written twice')
            chunks[address] = data
            data_records += 1
        elif kind in (5, 6):
            if address != data_records:
                raise ImageError(f'Line {number}: record count {address}, but {data_records} data records')
        else:
            start = address
    if not chunks:
        raise ImageError('No data records')
    return FlashImage(_merge(chunks, fill_gap), start, header=header)


def intel_hex_text(image: FlashImage, record_size: int = 16) -> str:
    """The Intel HEX file of an image (extended linear addresses, 32 bit)."""

    def record(kind: int, address: int, data: bytes) -> str:
        body = bytes([len(data)]) + address.to_bytes(2, 'big') + bytes([kind]) + data
        return ':' + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()

    lines = []
    upper = None
    for segment in image.segments:
        for offset in range(0, len(segment.data), record_size):
            address = segment.address + offset
            data = segment.data[offset : offset + record_size]
            if (address + len(data) - 1) >> 16 != address >> 16:  # Split at 64 KiB boundaries
                data = data[: 0x10000 - (address & 0xFFFF)]
            if address >> 16 != upper:
                upper = address >> 16
                lines.append(record(0x04, 0, upper.to_bytes(2, 'big')))
            lines.append(record(0x00, address & 0xFFFF, data))
            rest = segment.data[offset + len(data) : offset + record_size]
            if rest:
                upper = (address + len(data)) >> 16
                lines.append(record(0x04, 0, upper.to_bytes(2, 'big')))
                lines.append(record(0x00, 0, rest))
    if image.start_address is not None:
        lines.append(record(0x05, 0, image.start_address.to_bytes(4, 'big')))
    lines.append(record(0x01, 0, b''))
    return '\n'.join(lines) + '\n'


def load_image(path, base_address: int = 0, fill_gap: int = 0) -> FlashImage:
    """Read an Intel HEX, S-record or binary file (binary files are placed at base_address)."""
    path = Path(path)
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    text = raw[:1].decode('latin-1')
    if suffix in INTEL_HEX_SUFFIXES or (suffix not in SREC_SUFFIXES and text == ':' and _is_text(raw)):
        image = parse_intel_hex(raw.decode('latin-1'), fill_gap)
    elif suffix in SREC_SUFFIXES or (
        text in 'Ss' and _is_text(raw) and raw[:2].upper() in (b'S0', b'S1', b'S2', b'S3')
    ):
        image = parse_srec(raw.decode('latin-1'), fill_gap)
    else:
        if not raw:
            raise ImageError('The file is empty')
        image = FlashImage([Segment(base_address, raw)])
    image.path = path
    if not image.segments:
        raise ImageError('The file contains no data')
    return image


def _is_text(raw: bytes) -> bool:
    return all(byte in b'\r\n\t' or 32 <= byte < 127 for byte in raw[:256])


# Programming sequence


class SequenceError(ValueError):
    pass


@dataclass
class FlashOptions:
    programming_session: bool = True
    security_level: int = 0  # 0: no security access
    compute_key: Optional[Callable[[int, bytes], bytes]] = None
    erase_routine: Optional[int] = 0xFF00  # eraseMemory with address and size; None: no erase
    check_routine: Optional[int] = None  # e.g. 0xFF01 checkProgrammingDependencies
    reset: bool = True
    max_block_length: int = 0  # Limit of the TransferData length (0: the ECU's maximum)
    data_format: int = 0x00  # dataFormatIdentifier: compression (high nibble) and encryption (low nibble) method
    address_bytes: int = 4
    size_bytes: int = 4
    # Pre-programming: before programming, all ECUs (functionally) go to the extended session, stop updating DTCs
    # and stop sending normal messages, kept there by a functional TesterPresent; afterwards they are restored
    pre_programming: bool = False
    communication_type: int = 0x01  # CommunicationControl communicationType: 0x01 normal, 0x03 normal and NM
    restore: bool = True
    # Encodes a segment's data before the download (compression/encryption the ECU expects for `data_format`):
    # encode(data, address) -> bytes. The schemes are manufacturer specific, so they are the user's own code.
    encode: Optional[Callable[[bytes, int], bytes]] = None
    encoded_size: bool = False  # RequestDownload memorySize: the encoded size instead of the segment's size


def load_encoder(path: str | Path) -> Callable[[bytes, int], bytes]:
    """Load `encode(data: bytes, address: int) -> bytes` from a Python file (the file is executed)."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f'can_explorer_encoder_{path.stem}', path)
    if spec is None or spec.loader is None:
        raise SequenceError(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise SequenceError(f'Error in encoder {path.name}: {e}') from e
    function = getattr(module, 'encode', None)
    if not callable(function):
        raise SequenceError(f'{path.name} does not define encode(data, address)')
    return function


STEP_KINDS = (
    'session',
    'security',
    'dtc_setting',
    'communication_control',
    'keep_alive',
    'routine',
    'request',
    'write_did',
    'program',
    'check',
    'reset',
    'wait',
)


def default_sequence(options: FlashOptions) -> List[Dict[str, Any]]:
    """The steps of `options` as a sequence (the format of a JSON preset)."""
    steps: List[Dict[str, Any]] = []
    if options.pre_programming:
        steps += [
            {'step': 'session', 'session': 0x03, 'functional': True},
            {'step': 'keep_alive', 'functional': True},
            {'step': 'dtc_setting', 'on': False, 'functional': True},
            {
                'step': 'communication_control',
                'control': 0x03,
                'communication': options.communication_type,
                'functional': True,
            },
        ]
    if options.programming_session:
        steps.append({'step': 'session', 'session': 0x02})
    if options.security_level:
        steps.append({'step': 'security', 'level': options.security_level})
    steps.append({'step': 'program', 'erase_routine': options.erase_routine})
    if options.check_routine is not None:
        steps.append({'step': 'check', 'routine': options.check_routine})
    if options.reset:
        steps.append({'step': 'reset', 'type': 0x01})
    if options.pre_programming and options.restore:
        steps += [
            {
                'step': 'communication_control',
                'control': 0x00,
                'communication': options.communication_type,
                'functional': True,
            },
            {'step': 'dtc_setting', 'on': True, 'functional': True},
            {'step': 'session', 'session': 0x01, 'functional': True},
        ]
    return steps


def _number(value: Any, what: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise SequenceError(f'{what}: expected a number')
    if isinstance(value, str):
        text = value.strip().lower()
        try:
            value = int(text[2:] if text.startswith('0x') else text, 16)  # Hex, as identifiers are written
        except ValueError:
            raise SequenceError(f'{what}: {value!r} is not a hex number') from None
    if not isinstance(value, int) or not 0 <= value <= maximum:
        raise SequenceError(f'{what}: expected 0-0x{maximum:X}')
    return value


def check_sequence(steps: List[Dict[str, Any]]) -> None:
    """Raise SequenceError for an invalid sequence (unknown steps, missing or invalid parameters)."""
    if not isinstance(steps, list) or not steps:
        raise SequenceError('A sequence is a non-empty list of steps')
    if sum(1 for step in steps if isinstance(step, dict) and step.get('step') == 'program') != 1:
        raise SequenceError('A sequence has exactly one "program" step')
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get('step') not in STEP_KINDS:
            raise SequenceError(f'Step {index}: "step" must be one of {", ".join(STEP_KINDS)}')
        kind, where = step['step'], f'Step {index} ({step["step"]})'
        if kind == 'session':
            _number(step.get('session'), f'{where} session', 0x7F)
        elif kind == 'security':
            if _number(step.get('level'), f'{where} level', 0x7F) % 2 == 0:
                raise SequenceError(f'{where}: security access levels for requesting a seed are odd')
        elif kind == 'communication_control':
            _number(step.get('control', 0), f'{where} control', 0x05)
            _number(step.get('communication', 1), f'{where} communication', 0xFF)
        elif kind in ('routine', 'check'):
            _number(step.get('routine'), f'{where} routine', 0xFFFF)
        elif kind == 'request':
            try:
                if not parse_hex_bytes(str(step.get('payload', ''))):
                    raise ValueError('empty')
            except ValueError as e:
                raise SequenceError(f'{where} payload: {e}') from None
        elif kind == 'write_did':
            _number(step.get('did'), f'{where} did', 0xFFFF)
        elif kind == 'wait':
            seconds = step.get('seconds')
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds <= 600:
                raise SequenceError(f'{where}: seconds must be 0-600')
        elif kind == 'program' and step.get('erase_routine') is not None:
            _number(step['erase_routine'], f'{where} erase_routine', 0xFFFF)


def load_sequence(path: str | Path) -> Tuple[str, List[Dict[str, Any]]]:
    """A sequence preset: JSON {"name": ..., "steps": [{"step": "session", "session": 2}, ...]}. Returns
    (name, steps)."""
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise SequenceError(f'Cannot read {path.name}: {e}') from e
    if not isinstance(document, dict):
        raise SequenceError(f'{path.name}: expected an object with "steps"')
    steps = document.get('steps')
    check_sequence(steps)
    return str(document.get('name') or path.stem), steps


def _step_name(step: Dict[str, Any]) -> str:
    kind = step['step']
    if step.get('name'):
        return str(step['name'])
    everyone = ' (all ECUs)' if step.get('functional') else ''
    if kind == 'session':
        session = _number(step['session'], 'session', 0x7F)
        if session == 0x02 and not everyone:
            return 'Programming session'
        return f'{SESSIONS.get(session, f"Session 0x{session:02X}").capitalize()} session{everyone}'
    if kind == 'security':
        return f'Security access level {_number(step["level"], "level", 0x7F)}'
    if kind == 'dtc_setting':
        return f'DTC setting {"on" if step.get("on") else "off"}{everyone}'
    if kind == 'communication_control':
        control = _number(step.get('control', 0), 'control', 0x05)
        return f'Communication control {COMMUNICATION_CONTROL_TYPES[control]}{everyone}'
    if kind == 'keep_alive':
        return f'Tester present{everyone or " (kept alive)"}'
    if kind == 'check':
        return f'Check routine 0x{_number(step["routine"], "routine", 0xFFFF):04X}'
    if kind == 'routine':
        return f'Routine 0x{_number(step["routine"], "routine", 0xFFFF):04X}{everyone}'
    if kind == 'request':
        return f'Request {step["payload"]}{everyone}'
    if kind == 'write_did':
        return f'Write DID 0x{_number(step["did"], "did", 0xFFFF):04X}'
    if kind == 'reset':
        return 'ECU reset'
    if kind == 'wait':
        return f'Wait {step["seconds"]} s'
    return kind


@dataclass
class FlashStep:
    name: str
    status: str = 'pending'  # pending, running, done, failed, skipped
    detail: str = ''


@dataclass
class FlashJob:
    """Programs an image. `sequence` (a list of steps, see `load_sequence`) replaces the steps of `options`;
    `options` still give the key function, block limits, data format and encoder."""

    image: FlashImage
    options: FlashOptions
    on_change: Optional[Callable[['FlashJob'], None]] = None
    sequence: Optional[List[Dict[str, Any]]] = None
    steps: List[FlashStep] = field(default_factory=list)
    bytes_done: int = 0
    started: float = 0.0
    finished: float = 0.0
    error: str = ''

    def __post_init__(self):
        if self.sequence is None:
            self.sequence = default_sequence(self.options)
        else:
            check_sequence(self.sequence)
        self._plan: List[Tuple[FlashStep, Dict[str, Any], Optional[Segment], str]] = []
        for spec in self.sequence:
            if spec['step'] == 'program':
                for segment in self.image.segments:
                    if spec.get('erase_routine', self.options.erase_routine) is not None:
                        self._add(f'Erase 0x{segment.address:08X} ({len(segment.data)} bytes)', spec, segment, 'erase')
                    self._add(
                        f'Download 0x{segment.address:08X} ({len(segment.data)} bytes)', spec, segment, 'download'
                    )
            else:
                self._add(_step_name(spec), spec, None, spec['step'])

    def _add(self, name: str, spec: Dict[str, Any], segment: Optional[Segment], action: str) -> None:
        step = FlashStep(name)
        self.steps.append(step)
        self._plan.append((step, spec, segment, action))

    @property
    def total(self) -> int:
        return self.image.size

    @property
    def progress(self) -> float:
        return self.bytes_done / self.total if self.total else 0.0

    @property
    def rate(self) -> float:
        """Bytes per second."""
        elapsed = (self.finished or time.monotonic()) - self.started
        return self.bytes_done / elapsed if self.started and elapsed > 0 else 0.0

    def _changed(self):
        if self.on_change is not None:
            self.on_change(self)

    def _memory(self, address: int, size: int) -> bytes:
        options = self.options
        if address >= 1 << (8 * options.address_bytes) or size >= 1 << (8 * options.size_bytes):
            raise UdsError(f'0x{address:X} or {size} bytes do not fit the address and length format')
        return (
            bytes([(options.size_bytes << 4) | options.address_bytes])
            + address.to_bytes(options.address_bytes, 'big')
            + size.to_bytes(options.size_bytes, 'big')
        )

    async def run(self, client: UdsClient, functional=None) -> None:
        """Run the steps; raises UdsError (or a transport error) at the first failing step. `functional` (a
        FunctionalUdsClient) receives the functional steps; without one they go to this ECU only."""
        self.started = time.monotonic()
        keep_alive: Optional[SessionKeepAlive] = None
        try:
            for step, spec, segment, action in self._plan:
                if action == 'keep_alive':
                    target = functional if spec.get('functional', True) and functional is not None else client.transport
                    keep_alive = SessionKeepAlive(target, float(spec.get('interval', 2.0)))
                    await self._step(step, self._noop())
                    keep_alive.start()
                    continue
                await self._step(step, self._action(client, functional, step, spec, segment, action))
        except Exception as e:
            self.error = str(e) or type(e).__name__
            for step in self.steps:
                if step.status == 'running':
                    step.status, step.detail = 'failed', self.error
                elif step.status == 'pending':
                    step.status = 'skipped'
            raise
        finally:
            if keep_alive is not None:
                keep_alive.stop()
            self.finished = time.monotonic()
            self._changed()

    @staticmethod
    async def _noop() -> None:
        return None

    def _action(self, client, functional, step: FlashStep, spec, segment, action) -> Awaitable:
        options = self.options
        everyone = bool(spec.get('functional')) and functional is not None
        if spec.get('functional') and functional is None:
            step.detail = 'sent to this ECU only (no functional addressing)'
        if action == 'session':
            session = _number(spec['session'], 'session', 0x7F)
            if everyone:
                return functional.diagnostic_session_control(session)
            return client.diagnostic_session_control(session)
        if action == 'security':
            if options.compute_key is None:
                raise UdsError('Security access needs a key file')
            return client.security_access(_number(spec['level'], 'level', 0x7F), options.compute_key)
        if action == 'dtc_setting':
            if everyone:
                return functional.control_dtc_setting(bool(spec.get('on')))
            return client.control_dtc_setting(bool(spec.get('on')))
        if action == 'communication_control':
            control = _number(spec.get('control', 0), 'control', 0x05)
            communication = _number(spec.get('communication', 1), 'communication', 0xFF)
            if everyone:
                return functional.communication_control(control, communication)
            return client.communication_control(control, communication)
        if action in ('routine', 'check'):
            routine = _number(spec['routine'], 'routine', 0xFFFF)
            control = _number(spec.get('control', 1), 'control', 0xFF)
            data = parse_hex_bytes(str(spec.get('data', '')))
            if everyone:
                return functional.request(bytes([0x31, control, routine >> 8, routine & 0xFF]) + data)
            return client.routine_control(control, routine, data)
        if action == 'request':
            payload = parse_hex_bytes(str(spec['payload']))
            return functional.request(payload) if everyone else client.call(payload)
        if action == 'write_did':
            return client.write_data_by_identifier(
                _number(spec['did'], 'did', 0xFFFF), parse_hex_bytes(str(spec.get('data', '')))
            )
        if action == 'reset':
            return client.ecu_reset(_number(spec.get('type', 1), 'type', 0x7F))
        if action == 'wait':
            return asyncio.sleep(float(spec['seconds']))
        if action == 'erase':
            routine = _number(spec.get('erase_routine', options.erase_routine), 'erase_routine', 0xFFFF)
            return client.routine_control(0x01, routine, self._memory(segment.address, len(segment.data)))
        if action == 'download':
            return self._download(client, segment, step)
        raise SequenceError(f'Unknown step {action!r}')

    async def _step(self, step: FlashStep, operation) -> None:
        step.status = 'running'
        self._changed()
        await operation
        step.status = 'done'
        self._changed()

    async def _download(self, client: UdsClient, segment: Segment, step: FlashStep) -> None:
        options = self.options
        data = bytes(options.encode(segment.data, segment.address)) if options.encode is not None else segment.data
        size = len(data) if options.encoded_size else len(segment.data)
        response = await client.call(bytes([0x34, options.data_format]) + self._memory(segment.address, size))
        if len(response) < 2:
            raise UdsError('RequestDownload response too short')
        length_bytes = response[1] >> 4
        if not 1 <= length_bytes <= 4 or len(response) < 2 + length_bytes:
            raise UdsError('RequestDownload response without a valid block length')
        max_block = int.from_bytes(response[2 : 2 + length_bytes], 'big')  # Including SID and counter
        start = self.bytes_done
        scale = len(segment.data) / len(data) if data else 1.0  # Progress in image bytes, also for encoded data
        chunk = 0

        def progress(done: int, total: int) -> None:
            self.bytes_done = start + round(done * scale)
            step.detail = f'{done} / {total} bytes, blocks of {chunk}'
            self._changed()

        limit = min(options.max_block_length or ISOTP_MAX_PAYLOAD, ISOTP_MAX_PAYLOAD)
        chunk = min(max_block, limit) - 2
        if chunk < 1:
            raise UdsError(f'The ECU accepts blocks of {max_block} bytes, too short for data')
        await transfer_blocks(client, data, max_block, limit, progress)
        self.bytes_done = start + len(segment.data)
