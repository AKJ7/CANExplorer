"""Flash programming over XCP (the PGM resource): erases and programs an Intel HEX, Motorola S-record or binary
image (`core/flashing.py:load_image`) into an ECU's non-volatile memory, checks it and resets the ECU.

The sequence, after the open-source OpenBLT bootloader's XCP loader and pyxcp, with the optional steps in brackets:
(seed & key for PGM when locked) → [flash kernel: PROGRAM_PREPARE, DOWNLOAD, SET_MTA to its entry point] →
PROGRAM_START → PROGRAM_CLEAR (per range, or functional) → [PROGRAM_FORMAT] → SET_MTA + PROGRAM / PROGRAM_NEXT /
[PROGRAM_MAX] per segment → PROGRAM with no data → [PROGRAM_VERIFY] → [BUILD_CHECKSUM per segment] → PROGRAM_RESET.

Erasing: flash is erased a whole sector at a time, so by default the job clears every sector the image touches,
from the slave's GET_SECTOR_INFO; a slave that describes no sectors gets each segment's range cleared instead.
Functional erasing clears areas the slave knows by function (calibration, code, NVRAM) without addresses. The
addresses of the image are the slave's addresses; with word or dword address granularity sectors are not used.

Compressed or encrypted images: the compression and encryption methods are the manufacturer's (codes 0x80-0xFF of
PROGRAM_FORMAT). The image is either already prepared that way, or `encode(data, address) -> bytes` (e.g. from a
Python file, as for UDS flashing) prepares each segment before it is sent. The checksum check compares the slave's
checksum with the image as it ends up in the flash, i.e. before encoding.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from can_explorer.core.flashing import FlashImage, FlashStep, Segment
from can_explorer.core.xcp import XcpClient, XcpError
from can_explorer.core.xcp_protocol import (
    CLEAR_AREAS,
    CLEAR_CODE_AREAS,
    VERIFY_COMPLETE_FLASH,
    VERIFY_INTERNAL,
    VERIFY_VALUE,
)

ERASE_MODES = ('sectors', 'segments', 'functional', 'none')
ECU_CHECKS = ('none', 'internal', 'value')  # PROGRAM_VERIFY: none, the slave's own routine, or a value sent to it


@dataclass
class XcpFlashOptions:
    erase: str = 'sectors'  # One of ERASE_MODES
    verify: bool = True  # BUILD_CHECKSUM over each segment after programming
    reset: bool = True  # PROGRAM_RESET at the end (starts the new software); otherwise the session stays open
    extension: int = 0  # Address extension of the flash
    fill: int = 0xFF  # Pads a segment to whole address elements
    clear_timeout: float = 10.0  # Seconds one PROGRAM_CLEAR may take
    clear_areas: int = CLEAR_CODE_AREAS  # Functional erase: CLEAR_* bits, or user-defined from 0x100
    kernel: Optional[FlashImage] = None  # Flash kernel downloaded to RAM first (PROGRAM_PREPARE)
    kernel_extension: int = 0
    compression: int = 0  # PROGRAM_FORMAT methods (0: none); PROGRAM_FORMAT is sent when one is not 0
    encryption: int = 0
    encode: Optional[Callable[[bytes, int], bytes]] = None  # Compresses/encrypts a segment's data before sending
    program_max: bool = False  # Send full packets as PROGRAM_MAX
    ecu_check: str = 'none'  # One of ECU_CHECKS
    check_type: int = VERIFY_COMPLETE_FLASH  # PROGRAM_VERIFY verification type
    check_value: int = 0  # PROGRAM_VERIFY verification value (ecu_check 'value')

    def __post_init__(self):
        if self.erase not in ERASE_MODES:
            raise ValueError(f'erase is one of {", ".join(ERASE_MODES)}, not {self.erase!r}')
        if self.ecu_check not in ECU_CHECKS:
            raise ValueError(f'ecu_check is one of {", ".join(ECU_CHECKS)}, not {self.ecu_check!r}')
        for name in ('compression', 'encryption'):
            if not 0 <= getattr(self, name) <= 0xFF:
                raise ValueError(f'The {name} method is a byte (0-255)')
        if self.kernel is not None and not self.kernel.segments:
            raise ValueError('The flash kernel file is empty')

    @property
    def formatted(self) -> bool:
        return bool(self.compression or self.encryption)


def erase_ranges(segments: Sequence[Segment], sectors: Sequence[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """(start, length) to clear: the sectors any segment touches, in address order. Raises XcpError when part of a
    segment lies outside every sector, where the slave could not program it."""
    ordered = sorted((int(s['start']), int(s['length'])) for s in sectors if s['length'])
    chosen: List[Tuple[int, int]] = []
    for segment in segments:
        position = segment.address
        while position < segment.end:
            sector = next(((start, length) for start, length in ordered if start <= position < start + length), None)
            if sector is None:
                raise XcpError(f'0x{position:08X} of the image lies outside the flash sectors the ECU describes')
            if sector not in chosen:
                chosen.append(sector)
            position = sector[0] + sector[1]
    return sorted(chosen)


def area_names(areas: int) -> str:
    names = [name for bit, name in CLEAR_AREAS.items() if areas & bit]
    if areas & ~0xFF:
        names.append(f'user 0x{areas & ~0xFF:X}')
    return ', '.join(names) or 'none'


@dataclass
class XcpFlashJob:
    """Programs `image` into the slave of a connected `XcpClient`. `steps` and `bytes_done` follow the progress
    (as for the UDS `FlashJob`); `on_change` is called at each change."""

    image: FlashImage
    options: XcpFlashOptions = field(default_factory=XcpFlashOptions)
    on_change: Optional[Callable[['XcpFlashJob'], None]] = None
    steps: List[FlashStep] = field(default_factory=list)
    bytes_done: int = 0
    started: float = 0.0
    finished: float = 0.0
    error: str = ''
    sectors: List[Dict[str, Any]] = field(default_factory=list)  # As the slave describes them
    erased: List[Tuple[int, int]] = field(default_factory=list)  # (start, length) cleared

    def __post_init__(self):
        if not self.image.segments:
            raise XcpError('The image is empty')
        options = self.options
        self._kernel = self._add('Download the flash kernel') if options.kernel is not None else None
        self._start = self._add('Start programming')
        self._erase = self._add('Erase') if options.erase != 'none' else None
        self._format = self._add('Set the data format') if options.formatted else None
        self._program = [self._add(f'Program 0x{s.address:08X} ({len(s.data)} bytes)') for s in self.image.segments]
        self._end = self._add('End programming')
        self._ecu_check = self._add('ECU check (PROGRAM_VERIFY)') if options.ecu_check != 'none' else None
        self._verify = [self._add(f'Verify 0x{s.address:08X}') for s in self.image.segments] if options.verify else []
        self._reset = self._add('Reset the ECU') if options.reset else None

    def _add(self, name: str) -> FlashStep:
        step = FlashStep(name)
        self.steps.append(step)
        return step

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

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self)

    def _begin(self, step: FlashStep) -> None:
        step.status = 'running'
        self._changed()

    def _done(self, step: FlashStep, detail: str = '') -> None:
        step.status = 'done'
        if detail:
            step.detail = detail
        self._changed()

    def _padded(self, client: XcpClient, data: bytes) -> bytes:
        granularity = client.address_granularity
        remainder = len(data) % granularity
        return bytes(data) + bytes([self.options.fill]) * ((granularity - remainder) % granularity)

    async def run(self, client: XcpClient) -> None:
        """Run the sequence; raises XcpError (or a transport error) at the first failing step."""
        if not client.connected:
            raise XcpError('Connect to the XCP slave first')
        self.started = time.monotonic()
        options = self.options
        try:
            if self._kernel is not None:
                self._begin(self._kernel)
                self._done(self._kernel, await self._download_kernel(client))

            self._begin(self._start)
            await self._check_capabilities(client)
            info = await client.program_start()
            self._done(
                self._start,
                f'MAX_CTO_PGM {info["max_cto_pgm"]}' + (', block mode' if info['master_block_mode'] else ''),
            )

            if self._erase is not None:
                self._begin(self._erase)
                if options.erase == 'functional':
                    await client.program_clear_functional(options.clear_areas, options.clear_timeout)
                    self._done(self._erase, f'functional: {area_names(options.clear_areas)} areas')
                else:
                    ranges = await self._ranges(client)
                    for start, length in ranges:
                        self._erase.detail = f'0x{start:08X} ({length} bytes)'
                        self._changed()
                        await client.program_clear(start, length, options.extension, options.clear_timeout)
                        self.erased.append((start, length))
                    self._done(self._erase, ', '.join(f'0x{s:08X}+{n}' for s, n in ranges))

            if self._format is not None:
                self._begin(self._format)
                await client.program_format(options.compression, options.encryption)
                self._done(
                    self._format, f'compression 0x{options.compression:02X}, encryption 0x{options.encryption:02X}'
                )

            for segment, step in zip(self.image.segments, self._program, strict=True):
                await self._program_segment(client, segment, step)

            self._begin(self._end)
            await client.program_end()
            self._done(self._end)

            if self._ecu_check is not None:
                self._begin(self._ecu_check)
                if options.ecu_check == 'internal':
                    await client.program_verify(VERIFY_INTERNAL, options.check_type)
                    self._done(self._ecu_check, 'the ECU accepted its own check')
                else:
                    await client.program_verify(VERIFY_VALUE, options.check_type, options.check_value)
                    self._done(self._ecu_check, f'the ECU accepted 0x{options.check_value:08X}')

            for segment, step in zip(self.image.segments if self._verify else [], self._verify, strict=True):
                self._begin(step)
                self._done(step, await self._check(client, segment))

            if self._reset is not None:
                self._begin(self._reset)
                await client.program_reset()
                self._done(self._reset, 'session ended')
        except BaseException as e:
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

    async def _download_kernel(self, client: XcpClient) -> str:
        """PROGRAM_PREPARE with the kernel's size, DOWNLOAD it, and leave the MTA at its entry point, from where
        PROGRAM_START runs it."""
        kernel = self.options.kernel
        extension = self.options.kernel_extension
        first = kernel.segments[0]
        await client.program_prepare(first.address, kernel.size, extension)
        for segment in kernel.segments:
            await client.download(segment.address, self._padded(client, segment.data), extension)
        entry = kernel.start_address if kernel.start_address is not None else first.address
        await client.set_mta(entry, extension)
        return f'{kernel.size} bytes, entry 0x{entry:08X}'

    async def _check_capabilities(self, client: XcpClient) -> None:
        """Refuse early what the slave says it cannot take (GET_PGM_PROCESSOR_INFO is optional: without it, try)."""
        options = self.options
        try:
            info = await client.get_pgm_processor_info()
        except XcpError:
            return
        if options.erase == 'functional' and not info['functional_mode']:
            raise XcpError('The ECU does not support functional erasing: erase by sectors or ranges')
        if options.erase in ('sectors', 'segments') and not info['absolute_mode'] and info['properties']:
            raise XcpError('The ECU only supports functional erasing')
        if info['compression_required'] and not options.compression:
            raise XcpError('The ECU requires compressed data: set the compression method')
        if info['encryption_required'] and not options.encryption:
            raise XcpError('The ECU requires encrypted data: set the encryption method')
        if options.compression and not info['compression_supported']:
            raise XcpError('The ECU does not support compressed data')
        if options.encryption and not info['encryption_supported']:
            raise XcpError('The ECU does not support encrypted data')

    async def _program_segment(self, client: XcpClient, segment: Segment, step: FlashStep) -> None:
        self._begin(step)
        options = self.options
        start = self.bytes_done
        size = len(segment.data)
        data = self._padded(client, segment.data)
        if options.encode is not None:
            data = self._padded(client, bytes(options.encode(data, segment.address)))
        scale = size / len(data) if data else 1.0  # Progress in image bytes, also for encoded data

        def progress(sent: int) -> None:
            done = min(round(sent * scale), size)
            self.bytes_done = start + done
            step.detail = f'{done} / {size} bytes'
            self._changed()

        await client.program(segment.address, data, options.extension, progress, options.program_max)
        self.bytes_done = start + size
        self._done(step, f'{len(data)} bytes sent' if options.encode is not None else '')

    async def _ranges(self, client: XcpClient) -> List[Tuple[int, int]]:
        segments = [Segment(s.address, self._padded(client, s.data)) for s in self.image.segments]
        if self.options.erase == 'sectors' and client.address_granularity == 1:
            try:
                self.sectors = await client.sectors()
            except XcpError:
                self.sectors = []  # GET_PGM_PROCESSOR_INFO / GET_SECTOR_INFO are optional
            if self.sectors:
                return erase_ranges(segments, self.sectors)
        return [(s.address, len(s.data)) for s in segments]

    async def _check(self, client: XcpClient, segment: Segment) -> str:
        data = self._padded(client, segment.data)
        try:
            result = await client.verify_checksum(segment.address, data, self.options.extension)
        except XcpError as e:
            return f'not checked: {e}'  # E.g. a block larger than the slave's checksum limit
        if result['match'] is None:
            return f'not checked: {result["type"]} checksum (0x{result["ecu"]:X})'
        if not result['match']:
            raise XcpError(
                f'Checksum of 0x{segment.address:08X} does not match: ECU 0x{result["ecu"]:X}, '
                f'image 0x{result["expected"]:X} ({result["type"]})'
            )
        return f'{result["type"]} 0x{result["ecu"]:X} matches'
