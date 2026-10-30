"""J2534 pass-thru devices: a python-can bus (interface "j2534", class J2534Bus) for vehicle interfaces with an
SAE J2534-1 (version 04.04) driver, such as the Drew Technologies Mongoose and CarDAQ, the Tactrix OpenPort 2.0 and
the J2534 drivers of Bosch, Autel, Kvaser, PEAK and Intrepid.

The channel is the device's name as its driver registers it in Windows (the *PassThruSupport.04.04* registry key),
or the path of a J2534 library. Drivers of CANExplorer's own architecture are loaded into it. 32-bit drivers, which
a 64-bit program cannot load, run in the J2534 bridge (j2534_bridge.py), a separate 32-bit process: the
j2534-bridge32 program of the installers, a 32-bit Python found by the Windows `py` launcher, or the program or
Python named by the environment variable CANEXPLORER_J2534_BRIDGE.

The bus uses the J2534 CAN protocol: classic CAN with 11 and 29 bit identifiers at the bitrates the device offers,
with the device's timestamps. Received own messages are the device's loopback. J2534 04.04 has no listen only mode,
no CAN FD and no error frames.

API: PassThruOpen → PassThruConnect(CAN) → a pass-all filter (J2534 receives nothing without a filter) →
PassThruReadMsgs / PassThruWriteMsgs. A CAN message's data holds the identifier (4 bytes, big endian) followed by
the payload.
"""

import functools
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Deque, Dict, List, Optional, Tuple

import can

from can_explorer.core.gvret import TimestampClock
from can_explorer.core.j2534_driver import (
    CAN_29BIT_ID,
    CAN_ID_BOTH,
    CLEAR_RX_BUFFER,
    ERR_BUFFER_EMPTY,
    ERR_BUFFER_OVERFLOW,
    ERR_INVALID_FLAGS,
    ERR_TIMEOUT,
    LOOPBACK,
    PROTOCOL_CAN,
    START_OF_MESSAGE,
    STATUS_NOERROR,
    TX_INDICATION,
    TX_MSG_TYPE,
    X86,
    PassThruDriver,
    RawMessage,
    load_library,
    process_machine,
)

logger = logging.getLogger(__name__)

ERRORS = {
    0x01: 'not supported',
    0x02: 'invalid channel',
    0x03: 'invalid protocol',
    0x04: 'null parameter',
    0x05: 'invalid IOCTL value',
    0x06: 'invalid flags',
    0x07: 'failed',
    0x08: 'device not connected',
    0x09: 'timeout',
    0x0A: 'invalid message',
    0x0B: 'invalid time interval',
    0x0C: 'limit exceeded',
    0x0D: 'invalid message ID',
    0x0E: 'device in use',
    0x0F: 'invalid IOCTL',
    0x10: 'buffer empty',
    0x11: 'buffer full',
    0x12: 'buffer overflow',
    0x13: 'invalid pin',
    0x14: 'channel in use',
    0x15: 'wrong message protocol',
    0x16: 'invalid filter',
    0x17: 'no flow control',
    0x18: 'not unique',
    0x19: 'invalid bitrate',
    0x1A: 'invalid device',
}
REGISTRY_KEY = r'SOFTWARE\PassThruSupport.04.04'
MACHINES = {0x014C: '32-bit (x86)', 0x8664: '64-bit (x64)', 0xAA64: '64-bit (ARM64)'}
READ_BATCH = 64
READ_WAIT_MS = 10  # Blocking read slice; the driver lock is released between slices so that sends get through
WRITE_TIMEOUT = 0.1
DEFAULT_BITRATE = 500_000
BRIDGE_ENVIRONMENT = 'CANEXPLORER_J2534_BRIDGE'
BRIDGE_SCRIPT = Path(__file__).with_name('j2534_bridge.py')
BRIDGE_PROGRAM = 'j2534-bridge32'  # Folder and program of the bridge in the Windows installers
BRIDGE_PROTOCOL = 1
BRIDGE_START_TIMEOUT = 10.0
BRIDGE_CALL_TIMEOUT = 10.0  # Drivers can take seconds to open a device
BRIDGE_EXIT_TIMEOUT = 2.0  # Seconds for a bridge that closed its output to exit


@dataclass
class J2534Device:
    """A J2534 driver registered in Windows."""

    name: str
    vendor: str
    library: str
    can: bool = True
    machine: Optional[int] = None  # PE machine type of the library, None if unknown

    @property
    def loadable(self) -> bool:
        """Whether this process can load the library itself."""
        return self.machine is None or self.machine == process_machine()

    @property
    def bridgeable(self) -> bool:
        """Whether the 32-bit bridge can load the library."""
        return self.machine == X86 and bridge_command() is not None


@dataclass
class DeviceInfo:
    firmware: str = ''
    dll: str = ''
    api: str = ''
    bridge: bool = False  # The driver runs in the 32-bit bridge


def library_machine(path: str) -> Optional[int]:
    """The PE machine type of a DLL, None if it cannot be read or is no PE file."""
    try:
        with open(path, 'rb') as file:
            header = file.read(0x40)
            if len(header) < 0x40 or header[:2] != b'MZ':
                return None
            file.seek(int.from_bytes(header[0x3C:0x40], 'little'))
            pe = file.read(6)
    except OSError:
        return None
    if pe[:4] != b'PE\0\0':
        return None
    return int.from_bytes(pe[4:6], 'little')


def encode_message(msg: can.Message) -> Tuple[int, bytes]:
    """TxFlags and data of a frame."""
    if msg.is_remote_frame or msg.is_fd or msg.is_error_frame:
        raise can.CanOperationError('J2534 devices send classic CAN data frames only')
    if msg.dlc > 8:
        raise can.CanOperationError(f'Classic CAN frames have up to 8 bytes, got {msg.dlc}')
    data = msg.arbitration_id.to_bytes(4, 'big') + bytes(msg.data[: msg.dlc])
    return CAN_29BIT_ID if msg.is_extended_id else 0, data


def decode_message(message: RawMessage) -> Optional[Tuple[int, bool, bytes, bool]]:
    """(identifier, extended, data, sent by us) of a received CAN message; None for indications and other
    protocols' messages."""
    protocol, rx_status, _timestamp, data = message
    if protocol != PROTOCOL_CAN or rx_status & (START_OF_MESSAGE | TX_INDICATION):
        return None
    if not 4 <= len(data) <= 12:
        return None
    return (
        int.from_bytes(data[:4], 'big') & 0x1FFFFFFF,
        bool(rx_status & CAN_29BIT_ID),
        data[4:],
        bool(rx_status & TX_MSG_TYPE),
    )


# Drivers


def _registry_entries() -> List[Dict[str, Any]]:
    """The values of each device key under PassThruSupport.04.04, in the 64-bit and the 32-bit registry view."""
    if sys.platform != 'win32':
        return []
    import winreg

    entries = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                values: Dict[str, Any] = {'Key': name}
                try:
                    with winreg.OpenKey(root, name) as key:
                        value_index = 0
                        while True:
                            try:
                                value_name, value, _ = winreg.EnumValue(key, value_index)
                            except OSError:
                                break
                            values[value_name] = value
                            value_index += 1
                except OSError:
                    continue
                entries.append(values)
    return entries


def registered_devices() -> List[J2534Device]:
    """The J2534 drivers registered in Windows, one per library."""
    devices: List[J2534Device] = []
    seen = set()
    for entry in _registry_entries():
        name = str(entry.get('Name') or entry['Key'])
        libraries = [entry.get('FunctionLibrary'), entry.get('FunctionLibrary64')]
        for library in filter(None, libraries):
            # Deduplicate by (device, library): the same device appears in both the 64- and 32-bit registry
            # views, but two different devices may share one driver DLL (e.g. an Autel MaxiFlash registers its
            # USB and Bluetooth connections as separate devices using the same library), and both must be listed.
            identity = (name.lower(), str(library).lower())
            if identity in seen:
                continue
            seen.add(identity)
            devices.append(
                J2534Device(
                    name=name,
                    vendor=str(entry.get('Vendor', '')),
                    library=str(library),
                    can=bool(entry.get('CAN', 1)),
                    machine=library_machine(str(library)),
                )
            )
    return devices


def _is_library_path(channel: str) -> bool:
    return any(separator in channel for separator in ('/', '\\')) or channel.lower().endswith(('.dll', '.so', '.dylib'))


def _architecture(machine: Optional[int]) -> str:
    return MACHINES.get(machine or 0, 'of another architecture')


def resolve_channel(channel: str) -> J2534Device:
    """The device of a channel (a registered device's name, or the path of a J2534 library) that this process or
    the bridge can load."""
    if _is_library_path(channel):
        devices = [J2534Device(PureWindowsPath(channel).name, '', channel, machine=library_machine(channel))]
    else:
        devices = [device for device in registered_devices() if device.name.lower() == channel.lower()]
        if not devices:
            names = ', '.join(sorted({device.name for device in registered_devices()})) or 'none'
            raise can.CanInitializationError(f'No J2534 device "{channel}" is registered (registered: {names})')
    for usable in (lambda device: device.loadable, lambda device: device.bridgeable):
        for device in devices:
            if usable(device):
                return device
    device = devices[0]
    hint = (
        'Install a 32-bit Python (python.org, "Windows installer (32-bit)") for the J2534 bridge, or use the '
        'CANExplorer installer, which contains it.'
        if device.machine == X86
        else 'Ask the vendor for a driver of CANExplorer\'s architecture.'
    )
    raise can.CanInitializationError(
        f'The J2534 driver of {device.name} ({device.library}) is {_architecture(device.machine)}; CANExplorer is '
        f'{_architecture(process_machine())} and cannot load it. {hint}'
    )


@functools.lru_cache(maxsize=1)
def bridge_command() -> Optional[Tuple[str, ...]]:
    """The command that starts the 32-bit J2534 bridge, None if there is none."""
    configured = os.environ.get(BRIDGE_ENVIRONMENT)
    if configured:
        if PureWindowsPath(configured).name.lower().startswith('python'):
            return configured, str(BRIDGE_SCRIPT)
        return (configured,)
    if sys.platform != 'win32':
        return None
    # Installed (PyInstaller and Nuitka bundles): the bridge program is in the bundle
    folder = Path(sys.executable).parent
    for program in (folder / BRIDGE_PROGRAM / f'{BRIDGE_PROGRAM}.exe', folder / f'{BRIDGE_PROGRAM}.exe'):
        if program.exists():
            return (str(program),)
    launcher = shutil.which('py')
    if launcher and BRIDGE_SCRIPT.exists():
        try:
            listing = subprocess.run(
                [launcher, '--list-paths'], capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if '-32' in listing:
            return launcher, '-3-32', str(BRIDGE_SCRIPT)
    return None


_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_EOF = object()


class BridgeDriver:
    """The J2534 calls of PassThruDriver, forwarded to a driver running in the J2534 bridge process."""

    def __init__(self, command: Tuple[str, ...], library: str):
        self._lock = threading.Lock()
        self._lines: queue.Queue = queue.Queue()
        try:
            self._process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except OSError as e:
            raise can.CanInitializationError(f'Could not start the J2534 bridge ({" ".join(command)}): {e}') from e
        threading.Thread(target=self._read_lines, name='J2534 bridge', daemon=True).start()
        try:
            hello = self._receive(BRIDGE_START_TIMEOUT)
            if not isinstance(hello, dict) or hello.get('bridge') != BRIDGE_PROTOCOL:
                raise can.CanInitializationError(f'The J2534 bridge ({" ".join(command)}) did not start: {hello}')
            if hello.get('machine') not in (0, X86):
                raise can.CanInitializationError(
                    f'The J2534 bridge runs as {_architecture(hello.get("machine"))}; it needs a 32-bit Python'
                )
            self._call('load', library)
        except BaseException:
            self.shutdown()
            raise

    def _read_lines(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(_EOF)

    def _receive(self, timeout: float) -> Any:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise can.CanOperationError('The J2534 bridge does not answer') from None
        if line is _EOF:
            error = b''
            try:  # The bridge closed its output: it is ending (on Windows it may not be reaped yet)
                self._process.wait(BRIDGE_EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
            if self._process.stderr is not None and self._process.poll() is not None:
                error = self._process.stderr.read()
            detail = error.decode('utf-8', errors='replace').strip().splitlines()
            raise can.CanOperationError(f'The J2534 bridge stopped{": " + detail[-1] if detail else ""}')
        return json.loads(line)

    def _call(self, *request: Any) -> Any:
        with self._lock:
            assert self._process.stdin is not None
            try:
                self._process.stdin.write(json.dumps(request).encode('utf-8') + b'\n')
                self._process.stdin.flush()
            except OSError as e:
                raise can.CanOperationError(f'The J2534 bridge stopped: {e}') from e
            response = self._receive(BRIDGE_CALL_TIMEOUT)
        if 'error' in response:
            raise can.CanOperationError(f'J2534 bridge: {response["error"]}')
        return response['result']

    def open(self) -> Tuple[int, int]:
        return tuple(self._call('open'))  # type: ignore[return-value]

    def connect(self, device_id: int, protocol: int, flags: int, bitrate: int) -> Tuple[int, int]:
        return tuple(self._call('connect', device_id, protocol, flags, bitrate))  # type: ignore[return-value]

    def start_pass_filter(self, channel_id: int, protocol: int, tx_flags: int) -> int:
        return self._call('start_pass_filter', channel_id, protocol, tx_flags)

    def set_config(self, channel_id: int, parameter: int, value: int) -> int:
        return self._call('set_config', channel_id, parameter, value)

    def ioctl(self, channel_id: int, ioctl_id: int) -> int:
        return self._call('ioctl', channel_id, ioctl_id)

    def read_version(self, device_id: int) -> Tuple[int, List[str]]:
        return tuple(self._call('read_version', device_id))  # type: ignore[return-value]

    def last_error(self) -> str:
        return self._call('last_error')

    def read(self, channel_id: int, count: int, timeout_ms: int) -> Tuple[int, List[RawMessage]]:
        result, messages = self._call('read', channel_id, count, timeout_ms)
        return result, [
            (protocol, status, timestamp, bytes.fromhex(data)) for protocol, status, timestamp, data in messages
        ]

    def write(self, channel_id: int, protocol: int, tx_flags: int, data: bytes, timeout_ms: int) -> int:
        return self._call('write', channel_id, protocol, tx_flags, data.hex(), timeout_ms)

    def disconnect(self, channel_id: int) -> int:
        return self._call('disconnect', channel_id)

    def close(self, device_id: int) -> int:
        return self._call('close', device_id)

    def shutdown(self) -> None:
        """End the bridge process."""
        try:
            if self._process.poll() is None and self._process.stdin is not None:
                self._process.stdin.write(b'["exit"]\n')
                self._process.stdin.flush()
                self._process.stdin.close()
            self._process.wait(2)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
            self._process.wait()


# Bus


class J2534Bus(can.BusABC):
    """The CAN bus of a J2534 pass-thru device."""

    def __init__(
        self,
        channel: str,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        fd: bool = False,
        **kwargs: Any,
    ):
        if fd:
            raise can.CanInterfaceNotImplementedError('J2534 devices are used for classic CAN only; turn off CAN FD')
        if listen_only:
            raise can.CanInitializationError('J2534 devices have no listen only mode')
        self.device = resolve_channel(str(channel))
        self.bitrate = bitrate or DEFAULT_BITRATE
        self.info = DeviceInfo(bridge=not self.device.loadable)
        self._lock = threading.Lock()
        self._received: Deque[can.Message] = deque()
        self._clock = TimestampClock()
        self._local_echo = False
        self._device_id: Optional[int] = None
        self._channel_id: Optional[int] = None
        self._driver: Any
        if self.info.bridge:
            command = bridge_command()
            assert command is not None  # resolve_channel only returns such devices with a bridge
            try:
                self._driver = BridgeDriver(command, self.device.library)
            except can.CanOperationError as e:
                raise can.CanInitializationError(f'{self.device.name}: {e}') from e
        else:
            try:
                self._driver = PassThruDriver(load_library(self.device.library))
            except (OSError, AttributeError) as e:
                raise can.CanInitializationError(f'Could not load the J2534 driver {self.device.library}: {e}') from e
        try:
            self._open(receive_own_messages)
        except BaseException:
            self._close()
            raise
        self.channel_info = f'J2534 {self.device.name}{" (32-bit bridge)" if self.info.bridge else ""}'
        super().__init__(channel=channel, **kwargs)

    def _check(self, result: int, call: str, error: type = can.CanOperationError) -> None:
        if result != STATUS_NOERROR:
            try:
                detail = self._driver.last_error()
            except Exception:
                detail = ''
            message = f'{call} failed on {self.device.name}: {ERRORS.get(result, hex(result))}'
            raise error(f'{message} ({detail})' if detail else message, result)

    def _open(self, receive_own_messages: bool) -> None:
        driver = self._driver
        result, device_id = driver.open()
        self._check(result, 'PassThruOpen', can.CanInitializationError)
        self._device_id = device_id
        result, channel_id = driver.connect(device_id, PROTOCOL_CAN, CAN_ID_BOTH, self.bitrate)
        if result == ERR_INVALID_FLAGS:  # Devices that do not receive both identifier lengths at once
            logger.warning(f'{self.device.name} cannot receive 11 and 29 bit identifiers together; using 11 bit')
            result, channel_id = driver.connect(device_id, PROTOCOL_CAN, 0, self.bitrate)
        self._check(result, f'PassThruConnect at {self.bitrate} bit/s', can.CanInitializationError)
        self._channel_id = channel_id
        # J2534 passes nothing without a filter: a mask of 0 passes all, per identifier length
        self._check(driver.start_pass_filter(channel_id, PROTOCOL_CAN, 0), 'PassThruStartMsgFilter')
        driver.start_pass_filter(channel_id, PROTOCOL_CAN, CAN_29BIT_ID)
        if receive_own_messages:
            # Without loopback, sent frames are added locally
            self._local_echo = driver.set_config(channel_id, LOOPBACK, 1) != STATUS_NOERROR
        driver.ioctl(channel_id, CLEAR_RX_BUFFER)
        result, versions = driver.read_version(device_id)
        if result == STATUS_NOERROR:
            self.info.firmware, self.info.dll, self.info.api = versions
        logger.info(f'J2534 {self.device.name} at {self.bitrate} bit/s, {self.info}')

    def _close(self) -> None:
        try:
            if self._channel_id is not None:
                self._driver.disconnect(self._channel_id)
            if self._device_id is not None:
                self._driver.close(self._device_id)
        except Exception as e:
            logger.warning(f'Closing {self.device.name} failed: {e}')
        self._channel_id = self._device_id = None
        self._driver.shutdown()

    def _read(self, wait_ms: int) -> None:
        """Read the device's messages into the received queue, waiting up to wait_ms for the first one."""
        with self._lock:
            if self._channel_id is None:
                raise can.CanOperationError(f'{self.device.name} is closed')
            result, messages = self._driver.read(self._channel_id, READ_BATCH, 0)
            if not messages and wait_ms > 0 and result in (STATUS_NOERROR, ERR_BUFFER_EMPTY, ERR_TIMEOUT):
                result, messages = self._driver.read(self._channel_id, 1, wait_ms)
        if result == ERR_BUFFER_OVERFLOW:
            logger.warning(f'{self.device.name}: receive buffer overflow, frames were lost')
        elif result not in (STATUS_NOERROR, ERR_BUFFER_EMPTY, ERR_TIMEOUT):
            self._check(result, 'PassThruReadMsgs')
        for message in messages:
            decoded = decode_message(message)
            if decoded is None:
                continue
            arbitration_id, extended, data, sent = decoded
            self._received.append(
                can.Message(
                    timestamp=self._clock(message[2]),
                    arbitration_id=arbitration_id,
                    is_extended_id=extended,
                    dlc=len(data),
                    data=data,
                    channel=self.device.name,
                    is_rx=not sent,
                )
            )

    # python-can

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._received:
            remaining = READ_WAIT_MS if deadline is None else int(max(0.0, deadline - time.monotonic()) * 1000)
            self._read(min(remaining, READ_WAIT_MS))
            if not self._received and deadline is not None and time.monotonic() >= deadline:
                return None, False
        return self._received.popleft(), False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        tx_flags, data = encode_message(msg)
        wait_ms = int((WRITE_TIMEOUT if timeout is None else timeout) * 1000)
        with self._lock:
            if self._channel_id is None:
                raise can.CanOperationError(f'{self.device.name} is closed')
            result = self._driver.write(self._channel_id, PROTOCOL_CAN, tx_flags, data, wait_ms)
        if result == ERR_TIMEOUT and wait_ms > 0:
            raise can.CanOperationError(f'Sending on {self.device.name} timed out (is the bus connected?)', result)
        self._check(result, 'PassThruWriteMsgs')
        if self._local_echo:
            echo = msg.__copy__()
            echo.timestamp, echo.is_rx, echo.channel = time.time(), False, self.device.name
            self._received.append(echo)

    def shutdown(self) -> None:
        with self._lock:
            if self._channel_id is not None or self._device_id is not None:
                self._close()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """The registered J2534 drivers with CAN that this process or the 32-bit bridge can load."""
        configs = []
        for device in registered_devices():
            if not device.can:
                continue
            if not (device.loadable or device.bridgeable):
                logger.info(f'J2534 driver {device.name} ({device.library}) is {_architecture(device.machine)}')
                continue
            config = {'interface': 'j2534', 'channel': device.name}
            if config not in configs:
                configs.append(config)
        return configs
