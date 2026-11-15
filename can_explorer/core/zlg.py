"""ZLG (周立功) CAN and CAN FD interfaces — USBCAN-II, USBCAN-E-U and the USBCANFD series — as a python-can bus
(interface 'zlg').

EXPERIMENTAL: written from ZLG's published development library (CAN_lib.zip from zlg.cn: zlgcan.h, canframe.h, and
the "ZLGCAN接口函数使用手册" V1.21) and tested against a simulated library, not yet on a device. Only what those
documents specify is used:

- ZCAN_OpenDevice(type, index) → ZCAN_SetValue(baud rates) → ZCAN_InitCAN → ZCAN_StartCAN, as the manual's
  development flow (§2.1) shows. Classic CAN devices take the bit rate as "n/baud_rate"; CAN FD devices as
  "n/canfd_abit_baud_rate" and "n/canfd_dbit_baud_rate" (standard values only), with "n/canfd_standard" 0 (ISO).
- The termination resistor of USBCANFD devices: "n/initenal_resistance" (spelt so by ZLG), set after ZCAN_InitCAN.
- Frames: can_frame and canfd_frame (canframe.h), with the EFF/RTR/ERR flags in can_id, BRS/ESI in flags, and
  TX_ECHO_FLAG (0x20) marking echoed sent frames; transmit_type 0 sends, 2 sends with self-reception.
- Received timestamps are microseconds (ZCAN_Receive_Data.timestamp).

zlgcan.dll is a Windows library (it loads its kerneldlls folder next to it), so the interface works on Windows only.
Its folder is found on PATH, or set CANEXPLORER_ZLGCAN to the DLL's path.

The channel is DEVICE[:INDEX[:CHANNEL]], e.g. "USBCANFD-200U:0:1" (the first USBCANFD-200U, its second channel) or
"USBCAN-2E-U". DEVICE is a name below or ZLG's device type number.
"""

import ctypes
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import can

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

LIBRARY_VARIABLE = 'CANEXPLORER_ZLGCAN'

# Device types (zlgcan.h)
DEVICE_TYPES: Dict[str, int] = {
    'USBCAN1': 3,
    'USBCAN2': 4,  # USBCAN-II
    'USBCAN-2E-U': 21,
    'USBCAN-4E-U': 31,
    'USBCAN-8E-U': 34,
    'USBCANFD-200U': 41,
    'USBCANFD-100U': 42,
    'USBCANFD-MINI': 43,
    'USBCANFD-800U': 59,
    'USBCANFD-400U': 76,
    'USBCANFD-800H': 85,
}
CANFD_DEVICES = {41, 42, 43, 59, 76, 85}  # The USBCANFD series: TYPE_CANFD, the canfd_* baud rate keys

# zlgcan.h / canframe.h
STATUS_OK = 1
TYPE_CAN, TYPE_CANFD = 0, 1
CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG, CAN_ID_FLAG = 0x80000000, 0x40000000, 0x20000000, 0x1FFFFFFF
CANFD_BRS, CANFD_ESI = 0x01, 0x02
TX_ECHO_FLAG = 0x20
TRANSMIT_NORMAL, TRANSMIT_SELF_RECEPTION = 0, 2

# The standard baud rates of the manual's property tables
CAN_BAUD_RATES = (1000000, 800000, 500000, 250000, 125000, 100000, 50000)
CANFD_ARBITRATION_RATES = (1000000, 800000, 500000, 250000, 125000, 100000, 50000)
CANFD_DATA_RATES = (5000000, 4000000, 2000000, 1000000, 800000, 500000, 250000, 125000, 100000)

READ_BATCH = 256
READ_WAIT_MS = 50


class can_frame(ctypes.Structure):  # noqa: N801 - the names of canframe.h
    _fields_ = [
        ('can_id', ctypes.c_uint32),
        ('can_dlc', ctypes.c_uint8),
        ('pad', ctypes.c_uint8),
        ('res0', ctypes.c_uint8),
        ('res1', ctypes.c_uint8),
        ('data', ctypes.c_uint8 * 8),
    ]


class canfd_frame(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('can_id', ctypes.c_uint32),
        ('len', ctypes.c_uint8),
        ('flags', ctypes.c_uint8),
        ('res0', ctypes.c_uint8),
        ('res1', ctypes.c_uint8),
        ('data', ctypes.c_uint8 * 64),
    ]


class ZCAN_Transmit_Data(ctypes.Structure):  # noqa: N801
    _fields_ = [('frame', can_frame), ('transmit_type', ctypes.c_uint32)]


class ZCAN_Receive_Data(ctypes.Structure):  # noqa: N801
    _fields_ = [('frame', can_frame), ('timestamp', ctypes.c_uint64)]


class ZCAN_TransmitFD_Data(ctypes.Structure):  # noqa: N801
    _fields_ = [('frame', canfd_frame), ('transmit_type', ctypes.c_uint32)]


class ZCAN_ReceiveFD_Data(ctypes.Structure):  # noqa: N801
    _fields_ = [('frame', canfd_frame), ('timestamp', ctypes.c_uint64)]


class _CanConfig(ctypes.Structure):
    _fields_ = [
        ('acc_code', ctypes.c_uint32),
        ('acc_mask', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32),
        ('filter', ctypes.c_uint8),
        ('timing0', ctypes.c_uint8),
        ('timing1', ctypes.c_uint8),
        ('mode', ctypes.c_uint8),
    ]


class _CanFdConfig(ctypes.Structure):
    _fields_ = [
        ('acc_code', ctypes.c_uint32),
        ('acc_mask', ctypes.c_uint32),
        ('abit_timing', ctypes.c_uint32),
        ('dbit_timing', ctypes.c_uint32),
        ('brp', ctypes.c_uint32),
        ('filter', ctypes.c_uint8),
        ('mode', ctypes.c_uint8),
        ('pad', ctypes.c_uint16),
        ('reserved', ctypes.c_uint32),
    ]


class _ConfigUnion(ctypes.Union):
    _fields_ = [('can', _CanConfig), ('canfd', _CanFdConfig)]


class ZCAN_CHANNEL_INIT_CONFIG(ctypes.Structure):  # noqa: N801
    _anonymous_ = ('u',)
    _fields_ = [('can_type', ctypes.c_uint32), ('u', _ConfigUnion)]


def parse_channel(channel: str) -> Tuple[int, int, int, str]:
    """(device type, device index, channel index, device name) of "DEVICE[:INDEX[:CHANNEL]]"."""
    parts = [part.strip() for part in str(channel).split(':')]
    name = parts[0].upper().replace('_', '-')
    if name in DEVICE_TYPES:
        device_type = DEVICE_TYPES[name]
    else:
        try:
            device_type = int(name, 0)
        except ValueError:
            names = ', '.join(DEVICE_TYPES)
            raise can.CanInitializationError(
                f'Unknown ZLG device {parts[0]!r}; one of {names}, or its type number'
            ) from None
        name = next((known for known, number in DEVICE_TYPES.items() if number == device_type), f'type {device_type}')
    try:
        index = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        channel_index = int(parts[2]) if len(parts) > 2 and parts[2] else 0
    except ValueError:
        raise can.CanInitializationError(f'A ZLG channel is DEVICE[:INDEX[:CHANNEL]], not {channel!r}') from None
    return device_type, index, channel_index, name


def load_library(path: str = ''):
    """zlgcan.dll with its function signatures (Windows only)."""
    if sys.platform != 'win32':
        raise can.CanInterfaceNotImplementedError("ZLG's zlgcan library is for Windows only")
    path = path or os.environ.get(LIBRARY_VARIABLE, '') or 'zlgcan.dll'
    try:
        library = ctypes.WinDLL(path)  # __stdcall (FUNC_CALL)
    except OSError as e:
        raise can.CanInitializationError(
            f'Cannot load {path}: install the ZLG driver and its development library, and put zlgcan.dll (with its '
            f'kerneldlls folder) on PATH or set {LIBRARY_VARIABLE}: {e}'
        ) from e
    return declare(library)


def declare(library):
    void, uint = ctypes.c_void_p, ctypes.c_uint
    signatures = {
        'ZCAN_OpenDevice': (void, [uint, uint, uint]),
        'ZCAN_CloseDevice': (uint, [void]),
        'ZCAN_SetValue': (uint, [void, ctypes.c_char_p, ctypes.c_void_p]),
        'ZCAN_InitCAN': (void, [void, uint, ctypes.POINTER(ZCAN_CHANNEL_INIT_CONFIG)]),
        'ZCAN_StartCAN': (uint, [void]),
        'ZCAN_ResetCAN': (uint, [void]),
        'ZCAN_GetReceiveNum': (uint, [void, ctypes.c_uint8]),
        'ZCAN_Transmit': (uint, [void, ctypes.POINTER(ZCAN_Transmit_Data), uint]),
        'ZCAN_TransmitFD': (uint, [void, ctypes.POINTER(ZCAN_TransmitFD_Data), uint]),
        'ZCAN_Receive': (uint, [void, ctypes.POINTER(ZCAN_Receive_Data), uint, ctypes.c_int]),
        'ZCAN_ReceiveFD': (uint, [void, ctypes.POINTER(ZCAN_ReceiveFD_Data), uint, ctypes.c_int]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
    return library


class ZlgBus(can.BusABC):
    """One channel of a ZLG CAN or CAN FD interface. `library` replaces zlgcan.dll (tests)."""

    def __init__(
        self,
        channel: str,
        bitrate: Optional[int] = None,
        data_bitrate: Optional[int] = None,
        fd: bool = False,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        termination: bool = True,
        library: Any = None,
        **kwargs: Any,
    ):
        self.device_type, self.device_index, self.channel_index, self.device_name = parse_channel(channel)
        self.is_fd_device = self.device_type in CANFD_DEVICES
        if fd and not self.is_fd_device:
            raise can.CanInterfaceNotImplementedError(f'The ZLG {self.device_name} is a classic CAN interface')
        self.bitrate = bitrate or 500000
        self.data_bitrate = data_bitrate or 2000000
        self.fd = fd
        self._receive_own = receive_own_messages
        self._lib = library if library is not None else load_library()
        self._device = None
        self._channel = None
        self._lock = threading.Lock()
        self._received: Deque[can.Message] = deque()
        self._offset: Optional[float] = None
        try:
            self._open(listen_only, termination)
        except BaseException:
            self._close()
            raise
        self.channel_info = f'ZLG {self.device_name} {self.device_index}, channel {self.channel_index}'
        super().__init__(channel=channel, **kwargs)

    def _set(self, key: str, value: str) -> None:
        path = f'{self.channel_index}/{key}'.encode('ascii')
        if self._lib.ZCAN_SetValue(self._device, path, value.encode('ascii')) != STATUS_OK:
            raise can.CanInitializationError(f'ZLG {self.device_name}: setting {key} to {value} failed')

    def _open(self, listen_only: bool, termination: bool) -> None:
        self._device = self._lib.ZCAN_OpenDevice(self.device_type, self.device_index, 0)
        if not self._device:
            raise can.CanInitializationError(
                f'Cannot open the ZLG {self.device_name} {self.device_index} (connected, driver installed?)'
            )
        config = ZCAN_CHANNEL_INIT_CONFIG()
        if self.is_fd_device:
            if self.bitrate not in CANFD_ARBITRATION_RATES or self.data_bitrate not in CANFD_DATA_RATES:
                raise can.CanInitializationError(
                    f'The ZLG {self.device_name} takes the bit rates '
                    f'{", ".join(map(str, CANFD_ARBITRATION_RATES))} and data bit rates '
                    f'{", ".join(map(str, CANFD_DATA_RATES))}'
                )
            if self.device_type != DEVICE_TYPES['USBCANFD-800U']:  # Fixed to ISO (manual)
                self._set('canfd_standard', '0')
            self._set('canfd_abit_baud_rate', str(self.bitrate))
            self._set('canfd_dbit_baud_rate', str(self.data_bitrate))
            config.can_type = TYPE_CANFD
            config.canfd.mode = 1 if listen_only else 0
        else:
            if self.bitrate not in CAN_BAUD_RATES:
                raise can.CanInitializationError(
                    f'The ZLG {self.device_name} takes the bit rates {", ".join(map(str, CAN_BAUD_RATES))}'
                )
            self._set('baud_rate', str(self.bitrate))
            config.can_type = TYPE_CAN
            config.can.acc_code, config.can.acc_mask = 0, 0xFFFFFFFF  # Receive everything
            config.can.mode = 1 if listen_only else 0
        self._channel = self._lib.ZCAN_InitCAN(self._device, self.channel_index, ctypes.byref(config))
        if not self._channel:
            raise can.CanInitializationError(f'ZLG {self.device_name}: channel {self.channel_index} cannot start')
        if self.is_fd_device:
            self._set('initenal_resistance', '1' if termination else '0')
        if self._lib.ZCAN_StartCAN(self._channel) != STATUS_OK:
            raise can.CanInitializationError(f'ZLG {self.device_name}: channel {self.channel_index} cannot start')
        logger.info(f'ZLG {self.device_name} {self.device_index}/{self.channel_index} at {self.bitrate} bit/s')

    def _close(self) -> None:
        try:
            if self._channel:
                self._lib.ZCAN_ResetCAN(self._channel)
            if self._device:
                self._lib.ZCAN_CloseDevice(self._device)
        except Exception as e:  # noqa: BLE001 - closing must not raise
            logger.warning(f'Closing the ZLG {self.device_name} failed: {e}')
        self._channel = self._device = None

    def _timestamp(self, microseconds: int) -> float:
        if self._offset is None:
            self._offset = time.time() - microseconds / 1e6
        return self._offset + microseconds / 1e6

    def _message(self, frame, timestamp_us: int, fd: bool) -> Optional[can.Message]:
        can_id = frame.can_id
        flags = frame.flags if fd else frame.pad
        echoed = bool(flags & TX_ECHO_FLAG)
        if echoed and not self._receive_own:
            return None
        length = frame.len if fd else frame.can_dlc
        return can.Message(
            timestamp=self._timestamp(timestamp_us),
            arbitration_id=can_id & CAN_ID_FLAG,
            is_extended_id=bool(can_id & CAN_EFF_FLAG),
            is_remote_frame=bool(can_id & CAN_RTR_FLAG),
            is_error_frame=bool(can_id & CAN_ERR_FLAG),
            is_fd=fd,
            bitrate_switch=bool(fd and frame.flags & CANFD_BRS),
            error_state_indicator=bool(fd and frame.flags & CANFD_ESI),
            dlc=length,
            data=bytes(frame.data[: min(length, 64 if fd else 8)]),
            channel=self.channel_index,
            is_rx=not echoed,
        )

    def _read(self, wait_ms: int) -> None:
        with self._lock:
            if not self._channel:
                raise can.CanOperationError(f'The ZLG {self.device_name} is closed')
            for fd in (False, True) if self.is_fd_device else (False,):
                pending = self._lib.ZCAN_GetReceiveNum(self._channel, TYPE_CANFD if fd else TYPE_CAN)
                if not pending:
                    continue
                count = min(int(pending), READ_BATCH)
                buffer = ((ZCAN_ReceiveFD_Data if fd else ZCAN_Receive_Data) * count)()
                receive = self._lib.ZCAN_ReceiveFD if fd else self._lib.ZCAN_Receive
                got = receive(self._channel, buffer, count, 0)
                for item in buffer[: int(got)]:
                    message = self._message(item.frame, item.timestamp, fd)
                    if message is not None:
                        self._received.append(message)
        if not self._received and wait_ms > 0:
            time.sleep(min(wait_ms, 5) / 1000)  # The receive functions' wait is not specified for polling here

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._received:
            remaining = READ_WAIT_MS if deadline is None else int(max(0.0, deadline - time.monotonic()) * 1000)
            self._read(min(remaining, READ_WAIT_MS))
            if not self._received and deadline is not None and time.monotonic() >= deadline:
                return None, False
        return self._received.popleft(), False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        transmit_type = TRANSMIT_SELF_RECEPTION if self._receive_own else TRANSMIT_NORMAL
        can_id = msg.arbitration_id & CAN_ID_FLAG
        can_id |= (CAN_EFF_FLAG if msg.is_extended_id else 0) | (CAN_RTR_FLAG if msg.is_remote_frame else 0)
        if msg.is_fd:
            if not self.is_fd_device:
                raise can.CanOperationError(f'The ZLG {self.device_name} cannot send CAN FD frames')
            item = ZCAN_TransmitFD_Data(transmit_type=transmit_type)
            item.frame.can_id, item.frame.len = can_id, len(msg.data)
            item.frame.flags = CANFD_BRS if msg.bitrate_switch else 0
            ctypes.memmove(item.frame.data, bytes(msg.data), len(msg.data))
            transmit, data = self._lib.ZCAN_TransmitFD, item
        else:
            if len(msg.data) > 8:
                raise can.CanOperationError(f'Classic CAN frames have up to 8 bytes, got {len(msg.data)}')
            item = ZCAN_Transmit_Data(transmit_type=transmit_type)
            item.frame.can_id, item.frame.can_dlc = can_id, msg.dlc if msg.is_remote_frame else len(msg.data)
            ctypes.memmove(item.frame.data, bytes(msg.data), len(msg.data))
            transmit, data = self._lib.ZCAN_Transmit, item
        with self._lock:
            if not self._channel:
                raise can.CanOperationError(f'The ZLG {self.device_name} is closed')
            sent = transmit(self._channel, ctypes.byref(data), 1)
        if sent != 1:
            raise can.CanOperationError(f'The ZLG {self.device_name} did not send the frame (bus connected?)')

    def shutdown(self) -> None:
        with self._lock:
            self._close()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """ZLG devices cannot be listed without opening every type: enter the channel, e.g. USBCANFD-200U:0:0."""
        return []
