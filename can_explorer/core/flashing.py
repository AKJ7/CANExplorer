"""ECU flashing over UDS (ISO 14229): RequestDownload (0x34), TransferData (0x36) and RequestTransferExit (0x37),
from Intel HEX, Motorola S-record or binary files."""

import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from can_explorer.core.uds import UdsClient, UdsError

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


@dataclass
class FlashOptions:
    programming_session: bool = True
    security_level: int = 0  # 0: no security access
    compute_key: Optional[Callable[[int, bytes], bytes]] = None
    erase_routine: Optional[int] = 0xFF00  # eraseMemory with address and size; None: no erase
    check_routine: Optional[int] = None  # e.g. 0xFF01 checkProgrammingDependencies
    reset: bool = True
    max_block_length: int = 0  # Limit of the TransferData length (0: the ECU's maximum)
    data_format: int = 0x00  # No compression or encryption
    address_bytes: int = 4
    size_bytes: int = 4


@dataclass
class FlashStep:
    name: str
    status: str = 'pending'  # pending, running, done, failed, skipped
    detail: str = ''


@dataclass
class FlashJob:
    image: FlashImage
    options: FlashOptions
    on_change: Optional[Callable[['FlashJob'], None]] = None
    steps: List[FlashStep] = field(default_factory=list)
    bytes_done: int = 0
    started: float = 0.0
    finished: float = 0.0
    error: str = ''

    def __post_init__(self):
        options = self.options
        if options.programming_session:
            self.steps.append(FlashStep('Programming session'))
        if options.security_level:
            self.steps.append(FlashStep(f'Security access level {options.security_level}'))
        for segment in self.image.segments:
            if options.erase_routine is not None:
                self.steps.append(FlashStep(f'Erase 0x{segment.address:08X} ({len(segment.data)} bytes)'))
            self.steps.append(FlashStep(f'Download 0x{segment.address:08X} ({len(segment.data)} bytes)'))
        if options.check_routine is not None:
            self.steps.append(FlashStep(f'Check routine 0x{options.check_routine:04X}'))
        if options.reset:
            self.steps.append(FlashStep('ECU reset'))

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

    async def run(self, client: UdsClient) -> None:
        """Run the steps; raises UdsError (or a transport error) at the first failing step."""
        self.started = time.monotonic()
        steps = iter(self.steps)
        options = self.options
        try:
            if options.programming_session:
                await self._step(next(steps), client.diagnostic_session_control(0x02))
            if options.security_level:
                if options.compute_key is None:
                    raise UdsError('Security access needs a key file')
                await self._step(next(steps), client.security_access(options.security_level, options.compute_key))
            for segment in self.image.segments:
                if options.erase_routine is not None:
                    memory = self._memory(segment.address, len(segment.data))
                    await self._step(next(steps), client.routine_control(0x01, options.erase_routine, memory))
                step = next(steps)
                await self._step(step, self._download(client, segment, step))
            if options.check_routine is not None:
                await self._step(next(steps), client.routine_control(0x01, options.check_routine))
            if options.reset:
                await self._step(next(steps), client.ecu_reset(0x01))
        except Exception as e:
            self.error = str(e) or type(e).__name__
            for step in self.steps:
                if step.status == 'running':
                    step.status, step.detail = 'failed', self.error
                elif step.status == 'pending':
                    step.status = 'skipped'
            raise
        finally:
            self.finished = time.monotonic()
            self._changed()

    async def _step(self, step: FlashStep, operation) -> None:
        step.status = 'running'
        self._changed()
        await operation
        step.status = 'done'
        self._changed()

    async def _download(self, client: UdsClient, segment: Segment, step: FlashStep) -> None:
        response = await client.call(
            bytes([0x34, self.options.data_format]) + self._memory(segment.address, len(segment.data))
        )
        if len(response) < 2:
            raise UdsError('RequestDownload response too short')
        length_bytes = response[1] >> 4
        if not 1 <= length_bytes <= 4 or len(response) < 2 + length_bytes:
            raise UdsError('RequestDownload response without a valid block length')
        max_block = int.from_bytes(response[2 : 2 + length_bytes], 'big')  # Including SID and counter
        limit = min(max_block, ISOTP_MAX_PAYLOAD)
        if self.options.max_block_length:
            limit = min(limit, self.options.max_block_length)
        chunk = limit - 2
        if chunk < 1:
            raise UdsError(f'The ECU accepts blocks of {max_block} bytes, too short for data')
        counter = 1
        for offset in range(0, len(segment.data), chunk):
            data = segment.data[offset : offset + chunk]
            response = await client.call(bytes([0x36, counter]) + data)
            if len(response) < 2 or response[1] != counter:
                raise UdsError(
                    f'TransferData answered block {response[1] if len(response) > 1 else "?"}, expected {counter}'
                )
            self.bytes_done += len(data)
            step.detail = f'{offset + len(data)} / {len(segment.data)} bytes, blocks of {chunk}'
            self._changed()
            counter = (counter + 1) & 0xFF  # Wraps to 0 after 0xFF
        await client.call(bytes([0x37]))
