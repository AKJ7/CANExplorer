import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.flashing import (
    FlashImage,
    FlashJob,
    FlashOptions,
    ImageError,
    SequenceError,
    load_encoder,
    load_image,
    load_sequence,
)
from can_explorer.core.uds_functional import FunctionalUdsClient
from can_explorer.core.uds import UdsClient, UdsError, load_key_source
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

REFRESH_MS = 100


def _size(value: float) -> str:
    return f'{value / 1024:.1f} KiB' if value >= 1024 else f'{value:.0f} B'


def _hex_option(options: Dict[str, Any], key: str, default: str, maximum: int, what: str) -> int:
    text = str(options.get(key) or default).strip()
    try:
        value = int(text, 16)
    except ValueError:
        raise ValueError(f'Invalid hex {what}: {text!r}') from None
    if not 0 <= value <= maximum:
        raise ValueError(f'{what.capitalize()} out of range: 0x{value:X}')
    return value


class FlashController(QObject):
    """Programming an ECU over UDS with the transport and addressing of a DiagnosticsController."""

    changed = pyqtSignal()
    image_changed = pyqtSignal()

    def __init__(self, diagnostics, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._diagnostics = diagnostics
        self._image: Optional[FlashImage] = None
        self._job: Optional[FlashJob] = None
        self._task: Optional[asyncio.Task] = None
        self._state = ''
        self._running = False
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._sequence_name = ''
        self._sequence: Optional[List[Dict[str, Any]]] = None
        self._encoder_file = ''
        self._encoder = None

    def _refresh(self):
        if self._dirty:
            self._dirty = False
            self.changed.emit()

    def _on_job_change(self, _job: FlashJob):
        self._dirty = True

    # Image

    @pyqtSlot(QUrl, str, result=str)
    def load_file(self, url: QUrl, base_address: str) -> str:
        """Load an Intel HEX, S-record or binary file (placed at base_address, hex). Returns an error or ''."""
        if self.running:
            return 'Flashing is in progress'
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            base = int(base_address.strip() or '0', 16)
            image = load_image(path, base_address=base)
        except ValueError as e:
            return (
                f'Could not load {Path(path).name}: {e}' if isinstance(e, ImageError) else f'Invalid base address: {e}'
            )
        except OSError as e:
            return f'Could not read {Path(path).name}: {e.strerror or e}'
        self._image = image
        self._job, self._state = None, ''
        self.image_changed.emit()
        self.changed.emit()
        return ''

    @pyqtProperty(str, notify=image_changed)
    def file(self) -> str:
        return str(self._image.path) if self._image is not None and self._image.path else ''

    @pyqtProperty(bool, notify=image_changed)
    def binary(self) -> bool:
        """The loaded file is a binary image (without addresses)."""
        return (
            self._image is not None
            and self._image.path is not None
            and self._image.path.suffix.lower()
            in (
                '.bin',
                '.img',
                '',
            )
        )

    @pyqtProperty(str, notify=image_changed)
    def image_text(self) -> str:
        if self._image is None:
            return ''
        text = f'{self._image.describe()} · CRC-32 0x{self._image.crc32:08X}'
        if self._image.start_address is not None:
            text += f' · start 0x{self._image.start_address:08X}'
        if self._image.header:
            text += f' · {self._image.header}'
        return text

    # Progress

    @pyqtProperty(bool, notify=changed)
    def running(self) -> bool:
        return self._running

    @pyqtProperty(str, notify=changed)
    def state(self) -> str:
        """'', 'running', 'done', 'failed' or 'cancelled'."""
        return self._state

    @pyqtProperty(float, notify=changed)
    def progress(self) -> float:
        return self._job.progress if self._job is not None else 0.0

    @pyqtProperty(str, notify=changed)
    def progress_text(self) -> str:
        job = self._job
        if job is None:
            return ''
        text = f'{_size(job.bytes_done)} of {_size(job.total)}'
        rate = job.rate
        if rate > 0:
            text += f' · {_size(rate)}/s'
            if self.running and job.bytes_done < job.total:
                text += f' · {max(0, (job.total - job.bytes_done) / rate):.0f} s left'
        if job.finished and job.started:
            text += f' · {job.finished - job.started:.1f} s'
        return text

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._job.error if self._job is not None else ''

    @pyqtProperty('QVariantList', notify=changed)
    def steps(self) -> List[Dict[str, str]]:
        if self._job is None:
            return []
        return [{'name': step.name, 'status': step.status, 'detail': step.detail} for step in self._job.steps]

    # Sequence preset and encoder

    @pyqtSlot(QUrl, result=str)
    def load_sequence(self, url: QUrl) -> str:
        """Use a JSON sequence preset instead of the step options. Returns an error or ''."""
        try:
            self._sequence_name, self._sequence = load_sequence(url.toLocalFile())
        except SequenceError as e:
            return str(e)
        self.changed.emit()
        return ''

    @pyqtSlot()
    def clear_sequence(self):
        self._sequence_name, self._sequence = '', None
        self.changed.emit()

    @pyqtProperty(str, notify=changed)
    def sequence_name(self) -> str:
        return self._sequence_name

    @pyqtSlot(QUrl, result=str)
    def load_encoder(self, url: QUrl) -> str:
        """A Python file defining encode(data, address) for compressed/encrypted downloads. Returns an error or ''."""
        path = url.toLocalFile()
        try:
            self._encoder = load_encoder(path)
        except SequenceError as e:
            return str(e)
        self._encoder_file = path
        self.changed.emit()
        return ''

    @pyqtSlot()
    def clear_encoder(self):
        self._encoder_file, self._encoder = '', None
        self.changed.emit()

    @pyqtProperty(str, notify=changed)
    def encoder_file(self) -> str:
        return self._encoder_file

    # Running

    def _options(self, options: Dict[str, Any]) -> FlashOptions:
        diagnostics = self._diagnostics
        level = int(options.get('security_level', 0) or 0)
        compute_key = None
        if level and level % 2 == 0:
            raise ValueError('Security access levels for requesting a seed are odd')
        preset_security = self._sequence is not None and any(step['step'] == 'security' for step in self._sequence)
        if (level and self._sequence is None) or preset_security:
            if not diagnostics.key_file:
                raise ValueError('Choose the key algorithm in the Diagnostics view for security access')
            compute_key = load_key_source(diagnostics.key_file)
        return FlashOptions(
            programming_session=bool(options.get('session', True)),
            security_level=level,
            compute_key=compute_key,
            erase_routine=(
                _hex_option(options, 'erase_routine', 'FF00', 0xFFFF, 'erase routine')
                if options.get('erase', True)
                else None
            ),
            check_routine=(
                _hex_option(options, 'check_routine', 'FF01', 0xFFFF, 'check routine')
                if options.get('check', False)
                else None
            ),
            reset=bool(options.get('reset', True)),
            max_block_length=int(options.get('max_block', 0) or 0),
            pre_programming=bool(options.get('pre_programming', False)),
            communication_type=_hex_option(options, 'communication_type', '01', 0xFF, 'communication type'),
            data_format=_hex_option(options, 'data_format', '00', 0xFF, 'data format'),
            encode=self._encoder,
        )

    @pyqtSlot('QVariantMap', result=str)
    def start(self, options: Dict[str, Any]) -> str:
        """Options: session, security_level, erase, erase_routine, check, check_routine, reset, max_block,
        pre_programming, communication_type (hex), data_format (hex). A loaded sequence preset replaces the steps."""
        if self.running:
            return 'Flashing is in progress'
        if self._image is None:
            return 'Open a firmware file first'
        error = self._diagnostics.transport_error()
        if error:
            return error
        if self._diagnostics.busy:
            return 'A diagnostic request is in progress'
        try:
            flash_options = self._options(options)
        except (ValueError, UdsError) as e:
            return str(e)
        if flash_options.max_block_length and flash_options.max_block_length < 3:
            return 'Blocks need at least 3 bytes'
        if flash_options.data_format and self._encoder is None:
            return 'A data format other than 00 needs an encoder (encode(data, address))'
        try:
            self._job = FlashJob(self._image, flash_options, on_change=self._on_job_change, sequence=self._sequence)
        except SequenceError as e:
            return str(e)
        self._state = 'running'
        self._running = True
        self._task = asyncio.ensure_future(self._run(self._job))
        self._timer.start()
        self.changed.emit()
        return ''

    async def _run(self, job: FlashJob):
        transport = self._diagnostics.create_transport()
        functional_transport = None
        if any(step.get('functional') for step in job.sequence) and self._diagnostics.functional_available:
            functional_transport = self._diagnostics._functional_transport()
        try:
            functional = FunctionalUdsClient(functional_transport) if functional_transport is not None else None
            await job.run(UdsClient(transport), functional)
            self._state = 'done'
            logger.info(f'Flashed {job.total} bytes in {job.finished - job.started:.1f} s')
        except asyncio.CancelledError:
            self._state = 'cancelled'
            job.error = 'Cancelled'
            for step in job.steps:
                if step.status == 'running':
                    step.status, step.detail = 'failed', 'Cancelled'
                elif step.status == 'pending':
                    step.status = 'skipped'
        except Exception as e:
            logger.warning(f'Flashing failed: {e}')
            self._state = 'failed'
        finally:
            transport.close()
            if functional_transport is not None:
                functional_transport.close()
            self._running = False
            self._timer.stop()
            self._dirty = False
            self.changed.emit()

    @pyqtSlot()
    def cancel(self):
        if self._running and self._task is not None:
            self._task.cancel()

    def shutdown(self):
        self.cancel()
