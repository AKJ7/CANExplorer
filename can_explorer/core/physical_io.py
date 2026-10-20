"""Physical I/O: digital lines and analogue inputs of a USB I/O adapter, for switching outputs (relays, ignition,
wake-up lines) and watching inputs while working with a bus, from the Physical I/O tool or from test scripts.

Devices:
- aio_usb adapters (the I/O interface of STM32 CAN/LIN/I/O adapter firmware, USB 1d50:606f with a
  vendor interface of protocol 0x02): up to 32 digital lines, each an input or an output with pull-up, pull-down
  and active low options, and analogue inputs. Needs the optional pyusb and libusb-package packages
  (`uv sync --extra io`); on Linux a udev rule gives access to the device, on Windows the interface uses WinUSB.
- A simulated device, for trying the tool and for tests.

Scripts use the backends directly; every call blocks briefly:

    from can_explorer.core.physical_io import LineConfig, open_device
    io = open_device('simulated')
    io.configure_line(0, LineConfig(output=True))
    io.set_outputs(mask=0b1, values=0b1)    # Line 0 high
    print(io.read_status().analog_volts(io.info, reference=3.3))
    io.close()
"""

import asyncio
import importlib.util
import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

SIMULATED = 'simulated'

# aio_usb wire protocol: vendor control requests to the interface, packed little-endian structures
AIO_VID, AIO_PID = 0x1D50, 0x606F
AIO_INTERFACE = (0xFF, 0xFF, 0x02)  # class, subclass, protocol
REQUEST_OUT, REQUEST_IN = 0x41, 0xC1  # Vendor request to an interface
REQ_HOST_FORMAT, REQ_DEVICE_CONFIG, REQ_TIMESTAMP, REQ_IDENTIFY = 0, 1, 2, 3
REQ_IO_CONFIG, REQ_IO_SET, REQ_READ_STATUS = 4, 5, 6
CAPS_SIZE = 24
REPORT_HEADER_SIZE = 12
MODE_INPUT, MODE_OUTPUT = 0, 1
FLAG_PULL_UP, FLAG_PULL_DOWN, FLAG_ACTIVE_LOW = 0x01, 0x02, 0x04
FEATURE_TIMESTAMP, FEATURE_AUTO_REPORT, FEATURE_ANALOG = 0x01, 0x02, 0x04
USB_TIMEOUT_MS = 500

PULLS = ('none', 'up', 'down')


def io_available() -> bool:
    """Whether pyusb is installed, for real adapters (checked without importing it)."""
    return importlib.util.find_spec('usb') is not None


def version_text(version: int) -> str:
    """A version word as 'major.minor.patch' (one byte each, major in bits 16-23)."""
    return f'{(version >> 16) & 0xFF}.{(version >> 8) & 0xFF}.{version & 0xFF}'


@dataclass
class DeviceInfo:
    name: str
    io_count: int
    analog_count: int
    analog_bits: int
    input_mask: int  # Lines that can be inputs
    output_mask: int  # Lines that can be outputs
    sw_version: int = 0
    hw_version: int = 0
    features: int = 0

    @classmethod
    def from_caps(cls, name: str, data: bytes) -> 'DeviceInfo':
        """DEVICE_CONFIG: counts and resolution (4 × u8), input and output masks, versions and features (5 × u32)."""
        if len(data) < CAPS_SIZE:
            raise ConnectionError(f'Device configuration too short ({len(data)} of {CAPS_SIZE} bytes)')
        io_count, analog_count, analog_bits = data[0], data[1], data[2]
        words = [int.from_bytes(data[offset : offset + 4], 'little') for offset in range(4, CAPS_SIZE, 4)]
        return cls(name, min(io_count, 32), analog_count, analog_bits or 12, *words)

    def can_input(self, line: int) -> bool:
        return bool(self.input_mask >> line & 1)

    def can_output(self, line: int) -> bool:
        return bool(self.output_mask >> line & 1)

    @property
    def analog_full_scale(self) -> int:
        return (1 << self.analog_bits) - 1


@dataclass
class LineConfig:
    output: bool = False
    default_state: bool = False  # Level of an output when it is configured
    pull: str = 'none'  # 'none', 'up' or 'down' (inputs)
    active_low: bool = False

    def encode(self) -> bytes:
        """IO_CONFIG: mode, default state, flags, reserved (4 × u8) and the auto report period (u32, unused: 0)."""
        if self.pull not in PULLS:
            raise ValueError(f'Pull must be one of {", ".join(PULLS)}')
        flags = {'none': 0, 'up': FLAG_PULL_UP, 'down': FLAG_PULL_DOWN}[self.pull]
        flags |= FLAG_ACTIVE_LOW if self.active_low else 0
        mode = MODE_OUTPUT if self.output else MODE_INPUT
        return bytes([mode, int(self.default_state), flags, 0]) + (0).to_bytes(4, 'little')


@dataclass
class Snapshot:
    timestamp: float  # Host time of the reading
    device_ms: int
    states: int  # Level of line i in bit i
    directions: int  # Bit i set: line i is an output
    analog: List[int] = field(default_factory=list)  # Raw ADC values

    @classmethod
    def from_report(cls, data: bytes, analog_count: int) -> 'Snapshot':
        """READ_STATUS: timestamp_ms, io_states, io_direction (3 × u32) and the analogue values (u16 each)."""
        if len(data) < REPORT_HEADER_SIZE:
            raise ConnectionError(f'Status report too short ({len(data)} bytes)')
        device_ms, states, directions = (int.from_bytes(data[i : i + 4], 'little') for i in (0, 4, 8))
        count = min(analog_count, (len(data) - REPORT_HEADER_SIZE) // 2)
        analog = [
            int.from_bytes(data[REPORT_HEADER_SIZE + 2 * i : REPORT_HEADER_SIZE + 2 * i + 2], 'little')
            for i in range(count)
        ]
        return cls(time.time(), device_ms, states, directions, analog)

    def level(self, line: int) -> bool:
        return bool(self.states >> line & 1)

    def is_output(self, line: int) -> bool:
        return bool(self.directions >> line & 1)

    def analog_volts(self, info: DeviceInfo, reference: float) -> List[float]:
        return [value * reference / info.analog_full_scale for value in self.analog]


class IoBackend:
    """An I/O device. Every method blocks; `PhysicalIo` calls them on a worker thread."""

    info: DeviceInfo

    def configure_line(self, line: int, config: LineConfig) -> None:
        raise NotImplementedError

    def set_outputs(self, mask: int, values: int) -> None:
        """Drive the output lines in `mask` to the levels in `values` (bit i: line i)."""
        raise NotImplementedError

    def read_status(self) -> Snapshot:
        raise NotImplementedError

    def identify(self) -> None:
        """Make the device show itself (e.g. blink its LED)."""

    def close(self) -> None:
        pass

    def _check_line(self, line: int) -> None:
        if not 0 <= line < self.info.io_count:
            raise ValueError(f'The device has lines 0 to {self.info.io_count - 1}')


# aio_usb adapters


def _usb():
    import usb.core  # The optional dependency, imported only for real devices
    import usb.util

    return usb


def _usb_backend():
    """libusb from the libusb-package wheel where installed (Windows, macOS), else the system's."""
    try:
        import libusb_package
    except ImportError:
        return None
    return libusb_package.get_libusb1_backend()


def _aio_interface(device) -> Optional[int]:
    """The number of the device's aio_usb interface, None for gs_usb devices without one (plain candleLight)."""
    try:
        configuration = device.get_active_configuration()
    except Exception:
        configuration = next(iter(device), None)
    if configuration is None:
        return None
    for interface in configuration:
        if (
            interface.bInterfaceClass,
            interface.bInterfaceSubClass,
            interface.bInterfaceProtocol,
        ) == AIO_INTERFACE:
            return interface.bInterfaceNumber
    return None


def _device_id(device) -> str:
    serial = ''
    try:
        serial = device.serial_number or ''
    except Exception:  # Reading strings needs access to the device
        pass
    return f'aio:{serial}' if serial else f'aio:{device.bus}-{device.address}'


def find_aio_devices() -> List[Tuple[str, str]]:
    """(id, description) of the connected aio_usb adapters."""
    usb = _usb()
    devices = []
    for device in usb.core.find(find_all=True, idVendor=AIO_VID, idProduct=AIO_PID, backend=_usb_backend()):
        try:
            if _aio_interface(device) is None:
                continue
        except Exception:
            continue
        device_id = _device_id(device)
        devices.append((device_id, f'aio_usb I/O adapter {device_id[4:]}'))
    return devices


class AioUsbBackend(IoBackend):
    """The aio_usb interface of a USB adapter."""

    def __init__(self, device_id: str):
        usb = _usb()
        self._usb = usb
        device = next(
            (
                device
                for device in usb.core.find(find_all=True, idVendor=AIO_VID, idProduct=AIO_PID, backend=_usb_backend())
                if _device_id(device) == device_id
            ),
            None,
        )
        if device is None:
            raise ConnectionError(f'{device_id} is not connected')
        interface = _aio_interface(device)
        if interface is None:
            raise ConnectionError(f'{device_id} has no aio_usb interface')
        self._device, self._interface = device, interface
        try:
            if device.is_kernel_driver_active(interface):
                device.detach_kernel_driver(interface)
        except (NotImplementedError, usb.core.USBError):
            pass  # Windows and macOS have no kernel drivers to detach
        try:
            usb.util.claim_interface(device, interface)
            self._request_out(REQ_HOST_FORMAT, 0, (0x0000BEEF).to_bytes(4, 'little'))
            caps = bytes(self._request_in(REQ_DEVICE_CONFIG, 0, CAPS_SIZE))
        except usb.core.USBError as e:
            usb.util.dispose_resources(device)
            raise ConnectionError(
                f'Could not open {device_id}: {e} (on Linux, check the udev rule for 1d50:606f; '
                'on Windows, the interface needs the WinUSB driver)'
            ) from None
        self.info = DeviceInfo.from_caps(f'aio_usb {device_id[4:]}', caps)

    def _request_out(self, request: int, value: int, data: bytes) -> None:
        self._device.ctrl_transfer(REQUEST_OUT, request, value, self._interface, data, USB_TIMEOUT_MS)

    def _request_in(self, request: int, value: int, length: int):
        return self._device.ctrl_transfer(REQUEST_IN, request, value, self._interface, length, USB_TIMEOUT_MS)

    def _transfer(self, call):
        try:
            return call()
        except self._usb.core.USBError as e:
            if getattr(e, 'errno', None) == 32 or 'pipe' in str(e).lower():  # A STALL: the device refused
                raise ValueError('The device rejected the request') from None
            raise ConnectionError(f'{self.info.name}: {e}') from None

    def configure_line(self, line: int, config: LineConfig) -> None:
        self._check_line(line)
        self._transfer(lambda: self._request_out(REQ_IO_CONFIG, line, config.encode()))

    def set_outputs(self, mask: int, values: int) -> None:
        data = (mask & 0xFFFFFFFF).to_bytes(4, 'little') + (values & 0xFFFFFFFF).to_bytes(4, 'little')
        self._transfer(lambda: self._request_out(REQ_IO_SET, 0, data))

    def read_status(self) -> Snapshot:
        length = REPORT_HEADER_SIZE + 2 * self.info.analog_count
        data = self._transfer(lambda: self._request_in(REQ_READ_STATUS, 0, length))
        return Snapshot.from_report(bytes(data), self.info.analog_count)

    def identify(self) -> None:
        self._transfer(lambda: self._request_out(REQ_IDENTIFY, 0, b''))

    def close(self) -> None:
        try:
            self._usb.util.release_interface(self._device, self._interface)
        except Exception:
            pass
        self._usb.util.dispose_resources(self._device)


# A simulated device


class SimulatedIoBackend(IoBackend):
    """Eight lines and four 12 bit analogue inputs. Lines 4 to 7 are wired back to lines 0 to 3, so switching an
    output changes the input next to it; the analogue inputs follow slow waves around 1.65 V."""

    LOOPBACK = 4

    def __init__(self):
        self.info = DeviceInfo(
            'Simulated I/O', 8, 4, 12, 0xFF, 0x0F, 0x010000, 0x01, FEATURE_TIMESTAMP | FEATURE_ANALOG
        )
        self._configs: Dict[int, LineConfig] = {line: LineConfig() for line in range(self.info.io_count)}
        self._outputs = 0
        self._start = time.monotonic()
        self.identified = 0

    def configure_line(self, line: int, config: LineConfig) -> None:
        self._check_line(line)
        if config.output and not self.info.can_output(line):
            raise ValueError(f'Line {line} cannot be an output')
        self._configs[line] = config
        if config.output:
            self._outputs = self._outputs & ~(1 << line) | (int(config.default_state) << line)

    def set_outputs(self, mask: int, values: int) -> None:
        outputs = sum(1 << line for line, config in self._configs.items() if config.output)
        mask &= outputs  # Like the firmware: writes to inputs have no effect
        self._outputs = self._outputs & ~mask | values & mask

    def _input_level(self, line: int) -> bool:
        config = self._configs[line]
        source = line - self.LOOPBACK
        if 0 <= source < self.LOOPBACK and self._configs[source].output:
            level = bool(self._outputs >> source & 1)
        else:
            level = config.pull == 'up'  # Not connected: the pull decides
        return level != config.active_low

    def read_status(self) -> Snapshot:
        elapsed = time.monotonic() - self._start
        states = directions = 0
        for line, config in self._configs.items():
            if config.output:
                directions |= 1 << line
                level = bool(self._outputs >> line & 1)
            else:
                level = self._input_level(line)
            states |= int(level) << line
        full = self.info.analog_full_scale
        analog = [
            round(full / 2 + full * 0.4 * math.sin(elapsed * (0.5 + 0.3 * channel) + channel))
            for channel in range(self.info.analog_count)
        ]
        return Snapshot(time.time(), int(elapsed * 1000) & 0xFFFFFFFF, states, directions, analog)

    def identify(self) -> None:
        self.identified += 1


def list_devices() -> List[Tuple[str, str]]:
    """(id, description) of the connected aio_usb adapters (when pyusb is installed) and the simulated device."""
    devices = []
    if io_available():
        try:
            devices = find_aio_devices()
        except Exception:  # No libusb backend, for example
            devices = []
    return devices + [(SIMULATED, 'Simulated I/O (no hardware)')]


def open_device(device_id: str) -> IoBackend:
    return SimulatedIoBackend() if device_id == SIMULATED else AioUsbBackend(device_id)


# Asynchronous access for the user interface


class PhysicalIo:
    """A device polled on a worker thread: the latest snapshot and a short history for charts."""

    HISTORY = 300

    def __init__(self, backend: IoBackend, device_id: str = ''):
        self.backend = backend
        self.device_id = device_id
        self.info = backend.info
        self.configs: Dict[int, LineConfig] = {}
        self.outputs = 0  # Requested output levels
        self.snapshot: Optional[Snapshot] = None
        self.history: Deque[Snapshot] = deque(maxlen=self.HISTORY)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='physical-io')

    async def _call(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    async def poll(self) -> Snapshot:
        snapshot = await self._call(self.backend.read_status)
        self.snapshot = snapshot
        self.history.append(snapshot)
        return snapshot

    async def configure_line(self, line: int, config: LineConfig) -> None:
        if config.output and not self.info.can_output(line):
            raise ValueError(f'Line {line} cannot be an output')
        if not config.output and not self.info.can_input(line):
            raise ValueError(f'Line {line} cannot be an input')
        await self._call(self.backend.configure_line, line, config)
        self.configs[line] = config
        if config.output:
            self.outputs = self.outputs & ~(1 << line) | (int(config.default_state) << line)

    async def set_output(self, line: int, level: bool) -> None:
        if not self.configs.get(line, LineConfig()).output:
            raise ValueError(f'Line {line} is not an output')
        await self._call(self.backend.set_outputs, 1 << line, int(level) << line)
        self.outputs = self.outputs & ~(1 << line) | (int(level) << line)

    async def set_outputs(self, mask: int, values: int) -> None:
        await self._call(self.backend.set_outputs, mask, values)
        self.outputs = self.outputs & ~mask | values & mask

    async def identify(self) -> None:
        await self._call(self.backend.identify)

    def close(self) -> None:
        """Close the device and stop the worker thread (waits for a running call)."""
        self._executor.submit(self.backend.close)
        self._executor.shutdown(wait=True)
