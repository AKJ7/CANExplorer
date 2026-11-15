"""Toomoss (图莫斯) USB2XXX adapters — USB2CAN and USB2CANFD — as a python-can bus (interface 'toomoss').

EXPERIMENTAL: written from Toomoss's published examples (gitee.com/toomoss/USB2XXX_Example: the C headers
usb_device.h, usb2can.h and usb2canfd.h, and the Python and C# bindings) and tested against a simulated library, not
yet on a device. Where Toomoss's own bindings disagree, the C header is followed: CANFD_INIT_CONFIG.Mode is a byte,
as in usb2canfd.h, its C# binding and one of its two Python copies (the other declares a 32-bit Mode, which would
shift every bit timing field).

- Devices: USB_ScanDevice lists their handles, USB_OpenDevice opens one.
- CAN FD (usb2canfd.h): CANFD_Init with CANFD_INIT_CONFIG — bit timing from CANFD_GetCANSpeedArg where the library
  has it, otherwise computed from the header's formula, bit rate = 40 MHz / (BRP × (1 + SEG1 + SEG2)), at a 75 %
  sample point as Toomoss's examples use; ISO CRC on, automatic retransmission on, the 120 Ω termination switched
  by `termination`. Frames are CANFD_MSG: IDE/RTR in bits 31/30 of the ID, BRS/ESI/FDF in Flags, TXD (0x80) marking
  sent frames, timestamps in 10 µs.
- Classic CAN (usb2can.h): CAN_Init with the bit timing of CAN_GetCANSpeedArg; CAN_Mode 0 (normal) or 2 (silent,
  for listen only), bit 7 for the termination. Frames are CAN_MSG, timestamps in 100 µs.

The library is USB2XXX.dll (Windows), libUSB2XXX.so (Linux) or libUSB2XXX.dylib (macOS) from Toomoss, with its
libusb; it is found by the system, or set CANEXPLORER_TOOMOSS to its path.

The channel is DEVICE[:CHANNEL]: the adapter in the order USB_ScanDevice finds them (0 for the first) and its CAN
channel (0 = CAN1, 1 = CAN2), e.g. "0:1".
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

LIBRARY_VARIABLE = 'CANEXPLORER_TOOMOSS'
MAX_DEVICES = 20

# usb2canfd.h
CANFD_SUCCESS = 0
CANFD_MSG_FLAG_RTR, CANFD_MSG_FLAG_IDE, CANFD_MSG_FLAG_ID_MASK = 0x40000000, 0x80000000, 0x1FFFFFFF
CANFD_MSG_FLAG_BRS, CANFD_MSG_FLAG_ESI, CANFD_MSG_FLAG_FDF, CANFD_MSG_FLAG_TXD = 0x01, 0x02, 0x04, 0x80
CANFD_CLOCK = 40_000_000
CANFD_ERRORS = {-1: 'not supported by the adapter', -2: 'USB write failed', -3: 'USB read failed', -4: 'command failed'}
# usb2can.h
CAN_SUCCESS = 0
CAN_MODE_NORMAL, CAN_MODE_SILENT, CAN_MODE_TERMINATION = 0, 2, 0x80
CAN_SENT_FLAG = 0x80  # RemoteFlag bit 7: a sent frame

READ_BATCH = 1024
POLL_INTERVAL = 0.005


class CANFD_MSG(ctypes.Structure):  # noqa: N801 - the names of usb2canfd.h
    _fields_ = [
        ('ID', ctypes.c_uint32),
        ('DLC', ctypes.c_uint8),  # The data length in bytes (0..8, 12, 16, 20, 24, 32, 48, 64)
        ('Flags', ctypes.c_uint8),
        ('Res0', ctypes.c_uint8),
        ('TimeStampHigh', ctypes.c_uint8),
        ('TimeStamp', ctypes.c_uint32),  # 10 µs
        ('Data', ctypes.c_uint8 * 64),
    ]


class CANFD_INIT_CONFIG(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('Mode', ctypes.c_uint8),  # 0 normal, 1 self-test (loopback)
        ('ISOCRCEnable', ctypes.c_uint8),
        ('RetrySend', ctypes.c_uint8),
        ('ResEnable', ctypes.c_uint8),  # The internal 120 Ω termination
        ('NBT_BRP', ctypes.c_uint8),
        ('NBT_SEG1', ctypes.c_uint8),
        ('NBT_SEG2', ctypes.c_uint8),
        ('NBT_SJW', ctypes.c_uint8),
        ('DBT_BRP', ctypes.c_uint8),
        ('DBT_SEG1', ctypes.c_uint8),
        ('DBT_SEG2', ctypes.c_uint8),
        ('DBT_SJW', ctypes.c_uint8),
        ('Res0', ctypes.c_uint8 * 8),
    ]


class CAN_MSG(ctypes.Structure):  # noqa: N801 - usb2can.h
    _fields_ = [
        ('ID', ctypes.c_uint32),
        ('TimeStamp', ctypes.c_uint32),  # 100 µs
        ('RemoteFlag', ctypes.c_uint8),  # bit 0 remote frame, bit 7 sent frame
        ('ExternFlag', ctypes.c_uint8),
        ('DataLen', ctypes.c_uint8),
        ('Data', ctypes.c_uint8 * 8),
        ('TimeStampHigh', ctypes.c_uint8),
    ]


class CAN_INIT_CONFIG(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('CAN_BRP', ctypes.c_uint32),
        ('CAN_SJW', ctypes.c_uint8),
        ('CAN_BS1', ctypes.c_uint8),
        ('CAN_BS2', ctypes.c_uint8),
        ('CAN_Mode', ctypes.c_uint8),
        ('CAN_ABOM', ctypes.c_uint8),
        ('CAN_NART', ctypes.c_uint8),
        ('CAN_RFLM', ctypes.c_uint8),
        ('CAN_TXFP', ctypes.c_uint8),
    ]


def library_name() -> str:
    return {'win32': 'USB2XXX.dll', 'darwin': 'libUSB2XXX.dylib'}.get(sys.platform, 'libUSB2XXX.so')


def load_library(path: str = ''):
    """The USB2XXX library with its function signatures."""
    path = path or os.environ.get(LIBRARY_VARIABLE, '') or library_name()
    loader = ctypes.WinDLL if sys.platform == 'win32' else ctypes.CDLL  # WINAPI: __stdcall on Windows
    try:
        library = loader(path)
    except OSError as e:
        raise can.CanInitializationError(
            f'Cannot load {path}: install the Toomoss USB2XXX library (with its libusb) and set '
            f'{LIBRARY_VARIABLE} to its path: {e}'
        ) from e
    return declare(library)


def declare(library):
    i, u8, ptr = ctypes.c_int, ctypes.c_uint8, ctypes.POINTER
    signatures = {
        'USB_ScanDevice': (i, [ptr(ctypes.c_int)]),
        'USB_OpenDevice': (u8, [i]),
        'USB_CloseDevice': (u8, [i]),
        'CANFD_Init': (i, [i, u8, ptr(CANFD_INIT_CONFIG)]),
        'CANFD_StartGetMsg': (i, [i, u8]),
        'CANFD_StopGetMsg': (i, [i, u8]),
        'CANFD_SendMsg': (i, [i, u8, ptr(CANFD_MSG), ctypes.c_uint]),
        'CANFD_GetMsg': (i, [i, u8, ptr(CANFD_MSG), i]),
        'CAN_GetCANSpeedArg': (i, [i, ptr(CAN_INIT_CONFIG), ctypes.c_uint]),
        'CAN_Init': (i, [i, u8, ptr(CAN_INIT_CONFIG)]),
        'CAN_StartGetMsg': (i, [i, u8]),
        'CAN_StopGetMsg': (i, [i, u8]),
        'CAN_SendMsg': (i, [i, u8, ptr(CAN_MSG), ctypes.c_uint]),
        'CAN_GetMsgWithSize': (i, [i, u8, ptr(CAN_MSG), i]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
    speed = getattr(library, 'CANFD_GetCANSpeedArg', None)  # In usb2canfd.h, not in every library version
    if speed is not None:
        speed.restype, speed.argtypes = i, [i, ptr(CANFD_INIT_CONFIG), ctypes.c_uint, ctypes.c_uint]
    return library


def canfd_timing(bitrate: int, maximum_quanta: int) -> Tuple[int, int, int, int]:
    """(BRP, SEG1, SEG2, SJW) for a bit rate from the 40 MHz clock, at a 75 % sample point (as Toomoss's examples:
    500 kbit/s = 1, 59, 20; 2 Mbit/s = 1, 14, 5)."""
    for brp in range(1, 256):
        quanta, remainder = divmod(CANFD_CLOCK, brp * bitrate)
        if remainder or quanta > maximum_quanta:
            continue
        if quanta < 8:
            break
        seg2 = round(quanta * 0.25)
        seg1 = quanta - 1 - seg2
        if seg1 > 255 or seg2 > 255:
            continue
        return brp, seg1, seg2, min(seg2, 2)
    raise can.CanInitializationError(f'{bitrate} bit/s cannot be made from the 40 MHz clock of the adapter')


def parse_channel(channel: str) -> Tuple[int, int]:
    parts = [part.strip() for part in str(channel).split(':')]
    try:
        device = int(parts[0]) if parts[0] else 0
        can_index = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    except ValueError:
        raise can.CanInitializationError(f'A Toomoss channel is DEVICE[:CHANNEL], e.g. 0:1, not {channel!r}') from None
    return device, can_index


class ToomossBus(can.BusABC):
    """One CAN channel of a Toomoss USB2XXX adapter. `library` replaces the USB2XXX library (tests)."""

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
        self.device_number, self.can_index = parse_channel(channel)
        self.bitrate = bitrate or 500000
        self.data_bitrate = data_bitrate or 2000000
        self.fd = fd
        if fd and listen_only:
            raise can.CanInitializationError('Toomoss CAN FD adapters have no listen only mode')
        self._receive_own = receive_own_messages
        self._lib = library if library is not None else load_library()
        self._handle: Optional[int] = None
        self._started = False
        self._lock = threading.Lock()
        self._received: Deque[can.Message] = deque()
        self._offset: Optional[float] = None
        try:
            self._open(listen_only, termination)
        except BaseException:
            self._close()
            raise
        self.channel_info = f'Toomoss {self.device_number}, CAN{self.can_index + 1}'
        super().__init__(channel=channel, **kwargs)

    def _open(self, listen_only: bool, termination: bool) -> None:
        handles = (ctypes.c_int * MAX_DEVICES)()
        found = self._lib.USB_ScanDevice(handles)
        if found <= self.device_number:
            raise can.CanInitializationError(
                f'No Toomoss adapter {self.device_number}: {max(found, 0)} connected (driver and libusb installed?)'
            )
        handle = handles[self.device_number]
        if not self._lib.USB_OpenDevice(handle):
            raise can.CanInitializationError(f'The Toomoss adapter {self.device_number} cannot be opened')
        self._handle = handle
        if self.fd:
            config = CANFD_INIT_CONFIG(Mode=0, ISOCRCEnable=1, RetrySend=1, ResEnable=1 if termination else 0)
            speed = getattr(self._lib, 'CANFD_GetCANSpeedArg', None)
            if speed is None or speed(handle, ctypes.byref(config), self.bitrate, self.data_bitrate) != CANFD_SUCCESS:
                config.NBT_BRP, config.NBT_SEG1, config.NBT_SEG2, config.NBT_SJW = canfd_timing(self.bitrate, 160)
                config.DBT_BRP, config.DBT_SEG1, config.DBT_SEG2, config.DBT_SJW = canfd_timing(self.data_bitrate, 40)
            config.Mode, config.ISOCRCEnable, config.RetrySend = 0, 1, 1
            config.ResEnable = 1 if termination else 0
            self._check(self._lib.CANFD_Init(handle, self.can_index, ctypes.byref(config)), 'CANFD_Init')
            self._check(self._lib.CANFD_StartGetMsg(handle, self.can_index), 'CANFD_StartGetMsg')
        else:
            config = CAN_INIT_CONFIG()
            self._check(self._lib.CAN_GetCANSpeedArg(handle, ctypes.byref(config), self.bitrate), 'CAN_GetCANSpeedArg')
            config.CAN_Mode = (CAN_MODE_SILENT if listen_only else CAN_MODE_NORMAL) | (
                CAN_MODE_TERMINATION if termination else 0
            )
            self._check(self._lib.CAN_Init(handle, self.can_index, ctypes.byref(config)), 'CAN_Init')
            self._check(self._lib.CAN_StartGetMsg(handle, self.can_index), 'CAN_StartGetMsg')
        self._started = True
        logger.info(f'Toomoss {self.device_number} CAN{self.can_index + 1} at {self.bitrate} bit/s')

    def _check(self, result: int, call: str) -> None:
        if result != CANFD_SUCCESS:
            raise can.CanInitializationError(f'Toomoss {call} failed: {CANFD_ERRORS.get(result, result)}')

    def _close(self) -> None:
        try:
            if self._handle is not None:
                if self._started:
                    stop = self._lib.CANFD_StopGetMsg if self.fd else self._lib.CAN_StopGetMsg
                    stop(self._handle, self.can_index)
                self._lib.USB_CloseDevice(self._handle)
        except Exception as e:  # noqa: BLE001 - closing must not raise
            logger.warning(f'Closing the Toomoss adapter failed: {e}')
        self._handle, self._started = None, False

    def _timestamp(self, ticks: int, unit: float) -> float:
        seconds = ticks * unit
        if self._offset is None:
            self._offset = time.time() - seconds
        return self._offset + seconds

    def _read(self) -> None:
        with self._lock:
            if self._handle is None:
                raise can.CanOperationError('The Toomoss adapter is closed')
            if self.fd:
                buffer = (CANFD_MSG * READ_BATCH)()
                count = self._lib.CANFD_GetMsg(self._handle, self.can_index, buffer, READ_BATCH)
            else:
                buffer = (CAN_MSG * READ_BATCH)()
                count = self._lib.CAN_GetMsgWithSize(self._handle, self.can_index, buffer, READ_BATCH)
        if count < 0:
            raise can.CanOperationError(f'Reading the Toomoss adapter failed: {CANFD_ERRORS.get(count, count)}')
        for item in buffer[:count]:
            message = self._fd_message(item) if self.fd else self._can_message(item)
            if message is not None:
                self._received.append(message)

    def _fd_message(self, item: CANFD_MSG) -> Optional[can.Message]:
        sent = bool(item.Flags & CANFD_MSG_FLAG_TXD)
        if sent and not self._receive_own:
            return None
        length = min(item.DLC, 64)
        fd = bool(item.Flags & CANFD_MSG_FLAG_FDF)
        return can.Message(
            timestamp=self._timestamp((item.TimeStampHigh << 32) | item.TimeStamp, 10e-6),
            arbitration_id=item.ID & CANFD_MSG_FLAG_ID_MASK,
            is_extended_id=bool(item.ID & CANFD_MSG_FLAG_IDE),
            is_remote_frame=bool(item.ID & CANFD_MSG_FLAG_RTR),
            is_fd=fd,
            bitrate_switch=fd and bool(item.Flags & CANFD_MSG_FLAG_BRS),
            error_state_indicator=fd and bool(item.Flags & CANFD_MSG_FLAG_ESI),
            dlc=length,
            data=bytes(item.Data[:length]),
            channel=self.can_index,
            is_rx=not sent,
        )

    def _can_message(self, item: CAN_MSG) -> Optional[can.Message]:
        sent = bool(item.RemoteFlag & CAN_SENT_FLAG)
        if sent and not self._receive_own:
            return None
        length = min(item.DataLen, 8)
        return can.Message(
            timestamp=self._timestamp((item.TimeStampHigh << 32) | item.TimeStamp, 100e-6),
            arbitration_id=item.ID,
            is_extended_id=bool(item.ExternFlag),
            is_remote_frame=bool(item.RemoteFlag & 0x01),
            dlc=length,
            data=bytes(item.Data[:length]),
            channel=self.can_index,
            is_rx=not sent,
        )

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._received:
            self._read()
            if self._received:
                break
            if deadline is not None and time.monotonic() >= deadline:
                return None, False
            time.sleep(POLL_INTERVAL)  # The library has no blocking receive
        return self._received.popleft(), False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        if msg.is_fd and not self.fd:
            raise can.CanOperationError('Open the Toomoss adapter with CAN FD to send CAN FD frames')
        with self._lock:
            if self._handle is None:
                raise can.CanOperationError('The Toomoss adapter is closed')
            if self.fd:
                item = CANFD_MSG(DLC=len(msg.data))
                item.ID = (msg.arbitration_id & CANFD_MSG_FLAG_ID_MASK) | (
                    CANFD_MSG_FLAG_IDE if msg.is_extended_id else 0
                )
                item.ID |= CANFD_MSG_FLAG_RTR if msg.is_remote_frame else 0
                item.Flags = (CANFD_MSG_FLAG_FDF if msg.is_fd else 0) | (
                    CANFD_MSG_FLAG_BRS if msg.is_fd and msg.bitrate_switch else 0
                )
                ctypes.memmove(item.Data, bytes(msg.data), len(msg.data))
                sent = self._lib.CANFD_SendMsg(self._handle, self.can_index, ctypes.byref(item), 1)
            else:
                if len(msg.data) > 8:
                    raise can.CanOperationError(f'Classic CAN frames have up to 8 bytes, got {len(msg.data)}')
                item = CAN_MSG(ID=msg.arbitration_id, ExternFlag=1 if msg.is_extended_id else 0)
                item.RemoteFlag = 1 if msg.is_remote_frame else 0
                item.DataLen = msg.dlc if msg.is_remote_frame else len(msg.data)
                ctypes.memmove(item.Data, bytes(msg.data), len(msg.data))
                sent = self._lib.CAN_SendMsg(self._handle, self.can_index, ctypes.byref(item), 1)
        if sent != 1:
            raise can.CanOperationError(f'The Toomoss adapter did not send the frame: {CANFD_ERRORS.get(sent, sent)}')

    def shutdown(self) -> None:
        with self._lock:
            self._close()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """The connected adapters' first two channels, when the USB2XXX library can be loaded."""
        try:
            library = load_library()
        except can.CanError:
            return []
        handles = (ctypes.c_int * MAX_DEVICES)()
        try:
            found = library.USB_ScanDevice(handles)
        except OSError:
            return []
        return [
            {'interface': 'toomoss', 'channel': f'{device}:{index}'}
            for device in range(max(found, 0))
            for index in (0, 1)
        ]
