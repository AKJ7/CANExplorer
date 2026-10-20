import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.physical_io import (
    PULLS,
    IoBackend,
    LineConfig,
    PhysicalIo,
    io_available,
    list_devices,
    open_device,
    version_text,
)
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

DEFAULT_POLL_MS = 50
POLL_RANGE = (20, 1000)
DEFAULT_REFERENCE = 3.3
SPARKLINE_POINTS = 120
MAX_LOG = 100
UNAVAILABLE = 'USB I/O adapters need the pyusb package: install CANExplorer with the "io" extra'


class PhysicalIoController(QObject):
    """The Physical I/O tool: the digital lines and analogue inputs of an I/O adapter."""

    changed = pyqtSignal()
    status_changed = pyqtSignal()
    devices_changed = pyqtSignal()
    busy_changed = pyqtSignal()
    log_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None, opener: Callable[[str], IoBackend] = open_device):
        super().__init__(parent)
        self._opener = opener
        self._io: Optional[PhysicalIo] = None
        self._devices: List[Dict[str, str]] = []
        self._busy = False
        self._polling = False
        self._poll_errors = 0
        self._reference = DEFAULT_REFERENCE
        self._log_entries: List[Dict[str, Any]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(DEFAULT_POLL_MS)
        self._timer.timeout.connect(self._poll)

    def _log(self, text: str, error: bool = False):
        self._log_entries.append({'text': text, 'error': error})
        del self._log_entries[:-MAX_LOG]
        self.log_changed.emit()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.busy_changed.emit()

    # Devices

    @pyqtProperty(bool, constant=True)
    def available(self) -> bool:
        return io_available()

    @pyqtProperty('QVariantList', notify=devices_changed)
    def devices(self) -> List[Dict[str, str]]:
        if not self._devices:
            self._devices = self._list()
        return list(self._devices)

    @pyqtSlot()
    def refresh_devices(self):
        self._devices = self._list()
        self.devices_changed.emit()

    @staticmethod
    def _list() -> List[Dict[str, str]]:
        return [{'id': device_id, 'description': description} for device_id, description in list_devices()]

    @pyqtProperty(bool, notify=changed)
    def is_open(self) -> bool:
        return self._io is not None

    @pyqtProperty(bool, notify=busy_changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(str, notify=changed)
    def device_name(self) -> str:
        return self._io.info.name if self._io is not None else ''

    @pyqtProperty(str, notify=changed)
    def device_details(self) -> str:
        if self._io is None:
            return ''
        info = self._io.info
        parts = [f'{info.io_count} lines', f'{info.analog_count} analogue inputs ({info.analog_bits} bit)']
        if info.sw_version:
            parts.append(f'firmware {version_text(info.sw_version)}')
        if info.hw_version:
            parts.append(f'hardware {info.hw_version}')
        return ', '.join(parts)

    @pyqtSlot(str, result=str)
    def open_device(self, device_id: str) -> str:
        """Open a device and start polling it. Returns an error or ''."""
        if self._busy:
            return 'An operation is in progress'
        if not device_id:
            return 'Choose a device'
        self._set_busy(True)
        asyncio.ensure_future(self._open(device_id))
        return ''

    async def _open(self, device_id: str):
        self._close()
        try:
            backend = await asyncio.to_thread(self._opener, device_id)
        except Exception as e:
            self._log(str(e), error=True)
        else:
            self._io = PhysicalIo(backend, device_id)
            self._poll_errors = 0
            self._log(f'Opened {backend.info.name}')
            logger.info(f'Opened I/O device {device_id}: {backend.info}')
            self._timer.start()
        finally:
            self._set_busy(False)
            self.changed.emit()
            self.status_changed.emit()

    @pyqtSlot()
    def close_device(self):
        name = self.device_name
        self._close()
        if name:
            self._log(f'Closed {name}')
        self.changed.emit()
        self.status_changed.emit()

    def _close(self):
        self._timer.stop()
        io, self._io = self._io, None
        if io is not None:
            io.close()

    def shutdown(self):
        self._close()

    # Polling

    @pyqtProperty(int, notify=changed)
    def poll_interval(self) -> int:
        return self._timer.interval()

    @pyqtSlot(int)
    def set_poll_interval(self, interval: int):
        interval = min(max(interval, POLL_RANGE[0]), POLL_RANGE[1])
        if interval != self._timer.interval():
            self._timer.setInterval(interval)
            self.changed.emit()

    @pyqtProperty(float, notify=changed)
    def reference_voltage(self) -> float:
        return self._reference

    @pyqtSlot(float)
    def set_reference_voltage(self, volts: float):
        if volts > 0 and volts != self._reference:
            self._reference = volts
            self.changed.emit()
            self.status_changed.emit()

    def _poll(self):
        if self._io is None or self._polling:
            return
        self._polling = True
        asyncio.ensure_future(self._poll_once(self._io))

    async def _poll_once(self, io: PhysicalIo):
        try:
            await io.poll()
            self._poll_errors = 0
        except Exception as e:
            self._poll_errors += 1
            if self._poll_errors == 3 and io is self._io:  # Unplugged: stop instead of failing forever
                self._log(f'{io.info.name} stopped answering: {e}', error=True)
                self.close_device()
        finally:
            self._polling = False
        if io is self._io:
            self.status_changed.emit()

    # Lines

    @pyqtProperty('QVariantList', constant=True)
    def pulls(self) -> List[str]:
        return list(PULLS)

    @pyqtProperty('QVariantList', notify=status_changed)
    def lines(self) -> List[Dict[str, Any]]:
        io = self._io
        if io is None:
            return []
        snapshot = io.snapshot
        result = []
        for line in range(io.info.io_count):
            config = io.configs.get(line, LineConfig())
            output = snapshot.is_output(line) if snapshot is not None else config.output
            result.append(
                {
                    'line': line,
                    'output': output,
                    'level': snapshot.level(line) if snapshot is not None else False,
                    'requested': bool(io.outputs >> line & 1),
                    'pull': config.pull,
                    'active_low': config.active_low,
                    'can_input': io.info.can_input(line),
                    'can_output': io.info.can_output(line),
                }
            )
        return result

    def _start(self, operation: Callable[[PhysicalIo], Awaitable[str]]) -> str:
        if self._io is None:
            return 'Open a device first'
        asyncio.ensure_future(self._run(self._io, operation))
        return ''

    async def _run(self, io: PhysicalIo, operation: Callable[[PhysicalIo], Awaitable[str]]):
        try:
            text = await operation(io)
            if text:
                self._log(text)
        except (ValueError, ConnectionError) as e:
            self._log(str(e), error=True)
        except Exception as e:
            logger.warning(f'I/O operation failed: {e}')
            self._log(str(e), error=True)
        if io is self._io:
            await self._poll_once(io)  # Show the effect at once, not with the next poll

    @pyqtSlot(int, bool, str, bool, result=str)
    def configure_line(self, line: int, output: bool, pull: str, active_low: bool) -> str:
        """Make a line an input (with pull and active low) or an output (starting low)."""
        if pull not in PULLS:
            return f'Unknown pull {pull!r}'

        async def operation(io: PhysicalIo) -> str:
            await io.configure_line(line, LineConfig(output, False, pull, active_low))
            kind = 'output' if output else 'input' + (f', pull-{pull}' if pull != 'none' else '')
            return f'Line {line}: {kind}' + (', active low' if active_low else '')

        return self._start(operation)

    @pyqtSlot(int, bool, result=str)
    def set_output(self, line: int, level: bool) -> str:
        async def operation(io: PhysicalIo) -> str:
            await io.set_output(line, level)
            return f'Line {line} → {"high" if level else "low"}'

        return self._start(operation)

    @pyqtSlot(bool, result=str)
    def set_all_outputs(self, level: bool) -> str:
        async def operation(io: PhysicalIo) -> str:
            mask = sum(1 << line for line, config in io.configs.items() if config.output)
            if not mask:
                raise ValueError('No line is an output')
            await io.set_outputs(mask, mask if level else 0)
            return f'All outputs → {"high" if level else "low"}'

        return self._start(operation)

    @pyqtSlot(result=str)
    def identify(self) -> str:
        async def operation(io: PhysicalIo) -> str:
            await io.identify()
            return f'Identify sent to {io.info.name}'

        return self._start(operation)

    # Analogue inputs

    @pyqtProperty('QVariantList', notify=status_changed)
    def analog(self) -> List[Dict[str, Any]]:
        io = self._io
        if io is None or io.snapshot is None:
            return []
        full = io.info.analog_full_scale
        history = list(io.history)[-SPARKLINE_POINTS:]
        return [
            {
                'channel': channel,
                'raw': raw,
                'volts': raw * self._reference / full,
                'fraction': raw / full,
                'history': [snapshot.analog[channel] for snapshot in history if channel < len(snapshot.analog)],
            }
            for channel, raw in enumerate(io.snapshot.analog)
        ]

    @pyqtProperty('QVariantList', notify=log_changed)
    def log(self) -> List[Dict[str, Any]]:
        return list(self._log_entries)

    @pyqtSlot()
    def clear_log(self):
        self._log_entries.clear()
        self.log_changed.emit()
