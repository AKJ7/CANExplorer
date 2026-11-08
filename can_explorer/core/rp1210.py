"""TMC RP1210 adapters: a python-can bus (interface "rp1210", class Rp1210Bus) for the heavy-duty (truck, bus,
off-highway) diagnostic adapters that ship an RP1210 driver — the Nexiq USB-Link, Dearborn / DG Technologies DPA 5,
Noregon DLA+, Cummins INLINE, Dieselogic and others.

The channel is ``<API>:<DeviceID>``, where ``<API>`` is a vendor driver's name as it appears in RP121032.INI
(``NULN2R32`` for a Nexiq USB-Link 2, ``DGDPA5MA`` for a DG DPA 5, …) and ``<DeviceID>`` is one of that vendor's
device ids from its own ``<API>.INI``. ``<API>`` on its own uses the vendor's first device. ``detect()`` (python-can)
lists what is installed.

RP1210 is a Windows API provided as 32-bit DLLs. A 64-bit process cannot load them, so to use an RP1210 adapter run
CANExplorer as 32-bit Python (python.org, "Windows installer (32-bit)"); a clear error names the problem otherwise.

The bus uses the RP1210 CAN protocol: classic CAN with 11 and 29 bit identifiers at the bitrates the adapter offers.
An RP1210 CAN message is a CAN-type byte (0x00 for an 11-bit identifier, 0x01 for a 29-bit one) followed by the
identifier (4 bytes, big endian) and the payload; a received message is prefixed by the adapter's 4-byte big-endian
timestamp. (The RP1210C message layout is defined by the TMC and not reproduced here; this matches the published
filter-command structure — a CAN type byte and a 4-byte header. Validate against your adapter.) RP1210 04.00 as used
here has no CAN FD.
"""

import configparser
import ctypes
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import can

from can_explorer.core.gvret import TimestampClock

logger = logging.getLogger(__name__)

DEFAULT_BITRATE = 500_000
READ_BUFFER = 256  # Bytes; a classic-CAN RP1210 message is at most 4 + 1 + 4 + 8
CONNECT_BUFFER = 8192  # Tx/Rx buffer sizes requested of the driver at ClientConnect

# RP1210_SendCommand command numbers (RP1210C)
CMD_SET_ALL_FILTERS_TO_PASS = 3

# RP1210 CAN identifier type byte
CAN_STANDARD = 0x00  # 11-bit identifier
CAN_EXTENDED = 0x01  # 29-bit identifier

# A client id above this is an error code, not a connection (RP1210: valid client ids are 0..127)
MAX_CLIENT_ID = 127

# A small, well-known subset of RP1210 error codes; RP1210_GetErrorMsg provides the authoritative text at run time
ERRORS = {
    0: 'no error',
    128: 'DLL not initialized',
    129: 'invalid client id',
    130: 'client already connected',
    131: 'client area full (too many connections)',
    132: 'device not connected',
    142: 'message not sent',
    147: 'buffer full',
    453: 'hardware not responding',
}
# Codes for which continuing to read would spin forever: stop and surface them
FATAL_READ_ERRORS = {128, 129, 132}


# Driver enumeration (RP121032.INI and the per-vendor INIs)


@dataclass
class Rp1210Protocol:
    id: int
    string: str  # ProtocolString, e.g. 'CAN', 'J1939', 'ISO15765'
    description: str = ''
    speeds: Tuple[str, ...] = ()


@dataclass
class Rp1210Device:
    id: int
    description: str = ''
    name: str = ''


@dataclass
class Rp1210Vendor:
    """A vendor's RP1210 driver, as described by its INI."""

    api: str  # The driver name, which is also the DLL and INI basename, e.g. 'NULN2R32'
    name: str  # VendorInformation Name
    devices: List[Rp1210Device] = field(default_factory=list)
    protocols: List[Rp1210Protocol] = field(default_factory=list)
    timestamp_weight: float = 1.0  # Microseconds per timestamp tick (VendorInformation TimeStampWeight)

    def supports_can(self) -> bool:
        return any(protocol.string.upper() == 'CAN' for protocol in self.protocols)

    def device(self, device_id: int) -> Optional[Rp1210Device]:
        return next((device for device in self.devices if device.id == device_id), None)


def _windir() -> Path:
    import os

    return Path(os.environ.get('WINDIR') or r'C:\Windows')


def _int(text: Any, default: int = 0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _int_list(text: str) -> List[int]:
    values = []
    for part in str(text).split(','):
        part = part.strip()
        if part:
            try:
                values.append(int(part))
            except ValueError:
                continue
    return values


def _float(text: Any, default: float = 1.0) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def _parser() -> configparser.ConfigParser:
    # strict=False tolerates the duplicate sections/keys some vendor INIs carry; interpolation=None keeps '%' literal
    return configparser.ConfigParser(interpolation=None, strict=False)


def installed_apis() -> List[str]:
    """The driver names listed under [RP1210Support] APIImplementations in RP121032.INI."""
    parser = _parser()
    try:
        if not parser.read(_windir() / 'RP121032.INI'):
            return []
    except (OSError, configparser.Error) as e:
        logger.debug('Could not read RP121032.INI: %s', e)
        return []
    listed = parser.get('RP1210Support', 'APIImplementations', fallback='')
    return [name.strip() for name in listed.split(',') if name.strip()]


def read_vendor(api: str) -> Optional[Rp1210Vendor]:
    """Parse one vendor's ``<API>.INI``; None if it is missing or has no [VendorInformation]."""
    parser = _parser()
    try:
        if not parser.read(_windir() / f'{api}.INI'):
            return None
    except (OSError, configparser.Error) as e:
        logger.debug('Could not read %s.INI: %s', api, e)
        return None
    if not parser.has_section('VendorInformation'):
        return None
    info = parser['VendorInformation']
    devices = []
    for device_id in _int_list(info.get('Devices', '')):
        section = f'DeviceInformation{device_id}'
        entry = parser[section] if parser.has_section(section) else {}
        devices.append(
            Rp1210Device(
                id=_int(entry.get('DeviceID', device_id), device_id),
                description=entry.get('DeviceDescription', ''),
                name=entry.get('DeviceName', ''),
            )
        )
    protocols = []
    for protocol_id in _int_list(info.get('Protocols', '')):
        section = f'ProtocolInformation{protocol_id}'
        if not parser.has_section(section):
            continue
        entry = parser[section]
        protocols.append(
            Rp1210Protocol(
                id=protocol_id,
                string=entry.get('ProtocolString', ''),
                description=entry.get('ProtocolDescription', ''),
                speeds=tuple(speed.strip() for speed in entry.get('ProtocolSpeed', '').split(',') if speed.strip()),
            )
        )
    return Rp1210Vendor(
        api=api,
        name=info.get('Name', api),
        devices=devices,
        protocols=protocols,
        timestamp_weight=_float(info.get('TimeStampWeight', '1')),
    )


def installed_vendors() -> List[Rp1210Vendor]:
    """Every installed RP1210 vendor driver whose INI could be read."""
    vendors = []
    for api in installed_apis():
        vendor = read_vendor(api)
        if vendor is not None:
            vendors.append(vendor)
    return vendors


def resolve_channel(channel: str) -> Tuple[Rp1210Vendor, int]:
    """The (vendor, device id) of a ``<API>[:<DeviceID>]`` channel."""
    api, _, device_text = channel.strip().partition(':')
    api = api.strip()
    if not api:
        raise can.CanInitializationError('RP1210 channel must be "<API>:<DeviceID>", e.g. NULN2R32:1')
    vendor = read_vendor(api)
    if vendor is None:
        installed = ', '.join(installed_apis()) or 'none'
        raise can.CanInitializationError(f'No RP1210 driver "{api}" is installed (installed: {installed})')
    device_text = device_text.strip()
    if device_text:
        try:
            device_id = int(device_text)
        except ValueError:
            raise can.CanInitializationError(f'RP1210 device id must be a number, got {device_text!r}') from None
    elif vendor.devices:
        device_id = vendor.devices[0].id
    else:
        raise can.CanInitializationError(f'The RP1210 driver {api} lists no devices')
    return vendor, device_id


# The RP1210 DLL


def _declare(dll: Any) -> None:
    """Set the argument and return types of the RP1210 functions this backend calls (all return a 16-bit short)."""
    short, long_, char_p = ctypes.c_short, ctypes.c_long, ctypes.c_char_p
    dll.RP1210_ClientConnect.argtypes = [long_, short, char_p, long_, long_, short]
    dll.RP1210_ClientConnect.restype = short
    dll.RP1210_ClientDisconnect.argtypes = [short]
    dll.RP1210_ClientDisconnect.restype = short
    dll.RP1210_SendMessage.argtypes = [short, char_p, short, short, short]
    dll.RP1210_SendMessage.restype = short
    dll.RP1210_ReadMessage.argtypes = [short, char_p, short, short]
    dll.RP1210_ReadMessage.restype = short
    dll.RP1210_SendCommand.argtypes = [short, short, char_p, short]
    dll.RP1210_SendCommand.restype = short
    dll.RP1210_GetErrorMsg.argtypes = [short, char_p]
    dll.RP1210_GetErrorMsg.restype = short


def load_api(api: str) -> Any:
    """Load a vendor's RP1210 DLL (Windows only) with its function signatures declared."""
    if not hasattr(ctypes, 'WinDLL'):
        raise can.CanInterfaceNotImplementedError('RP1210 is a Windows API and is not available on this platform')
    last: OSError
    for name in (api, str(_windir() / 'SysWOW64' / f'{api}.dll'), str(_windir() / 'System32' / f'{api}.dll')):
        try:
            dll = ctypes.WinDLL(name)
            _declare(dll)
            return dll
        except OSError as e:
            last = e
            if getattr(e, 'winerror', None) == 193:  # ERROR_BAD_EXE_FORMAT: a 32-bit DLL in a 64-bit process
                raise can.CanInitializationError(
                    f'The RP1210 driver {api}.dll is 32-bit; run CANExplorer as 32-bit Python to use RP1210 adapters'
                ) from e
        except AttributeError as e:  # A DLL missing an RP1210 export is not an RP1210 driver
            raise can.CanInitializationError(f'{api}.dll is not a valid RP1210 driver: {e}') from e
    raise can.CanInitializationError(f'Could not load the RP1210 driver {api}.dll: {last}') from last


# Message translation


def encode_message(msg: can.Message) -> bytes:
    """An RP1210 CAN message body (type byte, 4-byte big-endian identifier, payload) for a frame."""
    if msg.is_remote_frame or msg.is_fd or msg.is_error_frame:
        raise can.CanOperationError('RP1210 sends classic CAN data frames only')
    if msg.dlc > 8:
        raise can.CanOperationError(f'Classic CAN frames have up to 8 bytes, got {msg.dlc}')
    can_type = CAN_EXTENDED if msg.is_extended_id else CAN_STANDARD
    return bytes([can_type]) + (msg.arbitration_id & 0x1FFFFFFF).to_bytes(4, 'big') + bytes(msg.data[: msg.dlc])


def decode_message(raw: bytes, timestamp_weight: float) -> Optional[Tuple[int, int, bool, bytes]]:
    """(timestamp µs, identifier, extended, data) of a received RP1210 CAN message; None if it is too short."""
    if len(raw) < 9:  # timestamp (4) + type (1) + identifier (4)
        return None
    timestamp = int.from_bytes(raw[0:4], 'big')
    body = raw[4:]
    extended = body[0] == CAN_EXTENDED
    identifier = int.from_bytes(body[1:5], 'big') & 0x1FFFFFFF
    return int(timestamp * timestamp_weight), identifier, extended or identifier > 0x7FF, body[5:]


# Bus


class Rp1210Bus(can.BusABC):
    """The CAN bus of a TMC RP1210 adapter."""

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
            raise can.CanInterfaceNotImplementedError('RP1210 (04.00) is classic CAN only; turn off CAN FD')
        if listen_only:
            raise can.CanInitializationError('RP1210 has no listen-only mode')
        self.vendor, self.device_id = resolve_channel(str(channel))
        if not self.vendor.supports_can():
            raise can.CanInitializationError(f'The RP1210 driver {self.vendor.api} does not offer the CAN protocol')
        self.bitrate = bitrate or DEFAULT_BITRATE
        self._lock = threading.Lock()
        self._received: Deque[can.Message] = deque()
        self._clock = TimestampClock()
        self._local_echo = receive_own_messages
        self._client_id: Optional[int] = None
        self._dll = load_api(self.vendor.api)
        try:
            self._connect()
        except BaseException:
            self._disconnect()
            raise
        self.channel_info = f'RP1210 {self.vendor.name} device {self.device_id}'
        super().__init__(channel=channel, **kwargs)

    def _error(self, code: int) -> str:
        code = abs(int(code))
        buffer = ctypes.create_string_buffer(256)
        try:
            if self._dll.RP1210_GetErrorMsg(code, buffer) == 0 and buffer.value:
                return f'{buffer.value.decode("latin-1").strip()} ({code})'
        except OSError:
            pass
        return ERRORS.get(code, f'error {code}')

    def _connect(self) -> None:
        protocol = f'CAN:Baud={self.bitrate // 1000}'.encode('ascii')
        client_id = self._dll.RP1210_ClientConnect(0, self.device_id, protocol, CONNECT_BUFFER, CONNECT_BUFFER, 0)
        if client_id < 0 or client_id > MAX_CLIENT_ID:
            raise can.CanInitializationError(
                f'RP1210_ClientConnect to {self.vendor.api} failed: {self._error(client_id)}'
            )
        self._client_id = client_id
        # RP1210 passes no traffic to the client until a filter is set; an empty "pass all" opens the gate
        result = self._dll.RP1210_SendCommand(CMD_SET_ALL_FILTERS_TO_PASS, client_id, b'', 0)
        if result != 0:
            logger.warning('%s: could not set the pass-all filter: %s', self.vendor.api, self._error(result))
        logger.info('RP1210 %s device %d at %d bit/s', self.vendor.name, self.device_id, self.bitrate)

    def _disconnect(self) -> None:
        with self._lock:
            client_id, self._client_id = self._client_id, None
        if client_id is not None:
            try:
                self._dll.RP1210_ClientDisconnect(client_id)
            except OSError as e:
                logger.warning('Disconnecting RP1210 %s failed: %s', self.vendor.api, e)

    def _read_once(self) -> None:
        buffer = ctypes.create_string_buffer(READ_BUFFER)
        with self._lock:
            if self._client_id is None:
                raise can.CanOperationError('The RP1210 bus is closed')
            count = self._dll.RP1210_ReadMessage(self._client_id, buffer, READ_BUFFER, 0)
        if count <= 0:
            if count < 0 and abs(count) in FATAL_READ_ERRORS:
                raise can.CanOperationError(f'RP1210_ReadMessage failed: {self._error(count)}', abs(count))
            return
        decoded = decode_message(buffer.raw[:count], self.vendor.timestamp_weight)
        if decoded is None:
            return
        timestamp_us, identifier, extended, data = decoded
        self._received.append(
            can.Message(
                timestamp=self._clock(timestamp_us),
                arbitration_id=identifier,
                is_extended_id=extended,
                dlc=len(data),
                data=data,
                channel=self.channel_info,
                is_rx=True,
            )
        )

    # python-can

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._received:
            self._read_once()
            if not self._received:
                if deadline is not None and time.monotonic() >= deadline:
                    return None, False
                time.sleep(0.001)  # RP1210 ReadMessage has no blocking timeout, so poll
        return self._received.popleft(), False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        body = encode_message(msg)
        with self._lock:
            if self._client_id is None:
                raise can.CanOperationError('The RP1210 bus is closed')
            result = self._dll.RP1210_SendMessage(self._client_id, body, len(body), 0, 0)
        if result != 0:
            raise can.CanOperationError(f'RP1210_SendMessage failed: {self._error(result)}', result)
        if self._local_echo:
            echo = msg.__copy__()
            echo.timestamp, echo.is_rx, echo.channel = time.time(), False, self.channel_info
            with self._lock:
                self._received.append(echo)

    def shutdown(self) -> None:
        self._disconnect()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """The installed RP1210 drivers that offer the CAN protocol, one config per device."""
        configs: List[Dict[str, Any]] = []
        for vendor in installed_vendors():
            if not vendor.supports_can():
                continue
            for device in vendor.devices or [Rp1210Device(id=1)]:
                configs.append({'interface': 'rp1210', 'channel': f'{vendor.api}:{device.id}'})
        return configs
