"""The SAE J2534-1 (04.04) API of a pass-thru driver library, through ctypes.

Only the standard library is used: the 32-bit J2534 bridge (j2534_bridge.py) runs this module in a separate 32-bit
Python, for drivers that a 64-bit CANExplorer cannot load. PassThruDriver offers the calls the CAN bus needs with
plain Python values, so they can be forwarded over the bridge as they are.
"""

import ctypes
import platform
import struct
import sys
from typing import Any, List, Optional, Tuple

ULONG = ctypes.c_uint32  # unsigned long of the Windows API
PROTOCOL_CAN = 5
CAN_29BIT_ID, CAN_ID_BOTH = 0x100, 0x800  # Connect and TxFlags / RxStatus flags
TX_MSG_TYPE, START_OF_MESSAGE, TX_INDICATION = 0x01, 0x02, 0x08  # RxStatus: loopback, ISO-TP indications
PASS_FILTER = 1
SET_CONFIG, CLEAR_RX_BUFFER = 0x02, 0x08
LOOPBACK = 0x03
STATUS_NOERROR, ERR_INVALID_FLAGS, ERR_TIMEOUT = 0x00, 0x06, 0x09
ERR_BUFFER_EMPTY, ERR_BUFFER_OVERFLOW = 0x10, 0x12
MAX_DATA = 4128
X86, X64, ARM64 = 0x014C, 0x8664, 0xAA64  # PE machine types

# A received message: protocol, RxStatus, timestamp (µs), data (for CAN: 4 identifier bytes and the payload)
RawMessage = Tuple[int, int, int, bytes]


class PassThruMsg(ctypes.Structure):
    _fields_ = [
        ('ProtocolID', ULONG),
        ('RxStatus', ULONG),
        ('TxFlags', ULONG),
        ('Timestamp', ULONG),
        ('DataSize', ULONG),
        ('ExtraDataIndex', ULONG),
        ('Data', ctypes.c_ubyte * MAX_DATA),
    ]


class SConfig(ctypes.Structure):
    _fields_ = [('Parameter', ULONG), ('Value', ULONG)]


class SConfigList(ctypes.Structure):
    _fields_ = [('NumOfParams', ULONG), ('ConfigPtr', ctypes.POINTER(SConfig))]


def process_machine() -> Optional[int]:
    """The PE machine type of this Python process (None outside Windows)."""
    if sys.platform != 'win32':
        return None
    if struct.calcsize('P') == 4:
        return X86
    return ARM64 if platform.machine().upper() in ('ARM64', 'AARCH64') else X64


def load_library(path: str) -> Any:
    """Load a J2534 library and declare its functions."""
    loader = ctypes.WinDLL if sys.platform == 'win32' else ctypes.CDLL  # type: ignore[attr-defined]
    library = loader(path)
    pointer, ulong_p, msg_p = ctypes.c_void_p, ctypes.POINTER(ULONG), ctypes.POINTER(PassThruMsg)
    signatures = {
        'PassThruOpen': [pointer, ulong_p],
        'PassThruClose': [ULONG],
        'PassThruConnect': [ULONG, ULONG, ULONG, ULONG, ulong_p],
        'PassThruDisconnect': [ULONG],
        'PassThruReadMsgs': [ULONG, msg_p, ulong_p, ULONG],
        'PassThruWriteMsgs': [ULONG, msg_p, ulong_p, ULONG],
        'PassThruStartMsgFilter': [ULONG, ULONG, msg_p, msg_p, msg_p, ulong_p],
        'PassThruIoctl': [ULONG, ULONG, pointer, pointer],
        'PassThruReadVersion': [ULONG, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p],
        'PassThruGetLastError': [ctypes.c_char_p],
    }
    for name, argtypes in signatures.items():
        function = getattr(library, name)
        function.argtypes = argtypes
        function.restype = ctypes.c_int32
    return library


class PassThruDriver:
    """The J2534 calls of a CAN bus on a loaded library. Each returns the J2534 status code (and its values)."""

    def __init__(self, library: Any):
        self._library = library
        self._buffer: Any = None

    def open(self) -> Tuple[int, int]:
        device_id = ULONG()
        return self._library.PassThruOpen(None, ctypes.byref(device_id)), device_id.value

    def connect(self, device_id: int, protocol: int, flags: int, bitrate: int) -> Tuple[int, int]:
        channel_id = ULONG()
        result = self._library.PassThruConnect(device_id, protocol, flags, bitrate, ctypes.byref(channel_id))
        return result, channel_id.value

    def start_pass_filter(self, channel_id: int, protocol: int, tx_flags: int) -> int:
        """A filter passing every message: mask and pattern of 4 zero bytes (the identifier)."""
        mask = PassThruMsg(ProtocolID=protocol, TxFlags=tx_flags, DataSize=4)
        pattern = PassThruMsg(ProtocolID=protocol, TxFlags=tx_flags, DataSize=4)
        filter_id = ULONG()
        return self._library.PassThruStartMsgFilter(
            channel_id, PASS_FILTER, ctypes.byref(mask), ctypes.byref(pattern), None, ctypes.byref(filter_id)
        )

    def set_config(self, channel_id: int, parameter: int, value: int) -> int:
        config = SConfig(parameter, value)
        config_list = SConfigList(1, ctypes.pointer(config))
        return self._library.PassThruIoctl(channel_id, SET_CONFIG, ctypes.byref(config_list), None)

    def ioctl(self, channel_id: int, ioctl_id: int) -> int:
        """An IOCTL without input and output, such as CLEAR_RX_BUFFER."""
        return self._library.PassThruIoctl(channel_id, ioctl_id, None, None)

    def read_version(self, device_id: int) -> Tuple[int, List[str]]:
        buffers = [ctypes.create_string_buffer(80) for _ in range(3)]
        result = self._library.PassThruReadVersion(device_id, *buffers)
        return result, [buffer.value.decode('latin-1').strip() for buffer in buffers]

    def last_error(self) -> str:
        buffer = ctypes.create_string_buffer(80)
        self._library.PassThruGetLastError(buffer)
        return buffer.value.decode('latin-1').strip()

    def read(self, channel_id: int, count: int, timeout_ms: int) -> Tuple[int, List[RawMessage]]:
        if self._buffer is None or len(self._buffer) < count:
            self._buffer = (PassThruMsg * count)()
        number = ULONG(count)
        result = self._library.PassThruReadMsgs(channel_id, self._buffer, ctypes.byref(number), timeout_ms)
        messages = [
            (
                message.ProtocolID,
                message.RxStatus,
                message.Timestamp,
                bytes(message.Data[: min(message.DataSize, MAX_DATA)]),
            )
            for message in self._buffer[: min(number.value, count)]
        ]
        return result, messages

    def write(self, channel_id: int, protocol: int, tx_flags: int, data: bytes, timeout_ms: int) -> int:
        message = PassThruMsg(ProtocolID=protocol, TxFlags=tx_flags, DataSize=len(data))
        ctypes.memmove(message.Data, data, len(data))
        number = ULONG(1)
        return self._library.PassThruWriteMsgs(channel_id, ctypes.byref(message), ctypes.byref(number), timeout_ms)

    def disconnect(self, channel_id: int) -> int:
        return self._library.PassThruDisconnect(channel_id)

    def close(self, device_id: int) -> int:
        return self._library.PassThruClose(device_id)

    def shutdown(self) -> None:
        """Nothing to release for a library in this process (the bridge's driver ends its process)."""
