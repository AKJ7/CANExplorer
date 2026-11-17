"""Kvaser LIN interfaces (Kvaser LIN Leaf, and the LIN channels of Kvaser Hybrid devices) through Kvaser's LINlib:
a python-can bus (interface "kvaser_lin", class KvaserLinBus) presenting LIN in the sllin convention, as master or
slave (see core/lin_adapter.py).

EXPERIMENTAL: written from Kvaser's open-source Python wrapper of LINlib (pycanlib, MIT license: canlib/linlib
dll.py, structures.py, enums.py, channel.py) and tested against a simulated library, not yet on a device. Windows
(linlib.dll, installed with Kvaser's CANlib SDK / drivers) and Linux (liblinlib.so of Kvaser's Linux SDK).

The channel is the CANlib channel number of the LIN channel (`0`, `1`, … as Kvaser's tools number them), with an
optional `@slave`. Used: linInitializeLibrary, linOpenChannel (master/slave), linSetBitrate, linBusOn, linSetupLIN
(enhanced checksum and variable length for LIN 2.x; needs firmware 2.5.1 or later, its failure is ignored),
linWriteMessage (master: header with response), linRequestMessage (master: header only), linUpdateMessage (slave:
the response), linReadMessageWait, linBusOff, linClose. Received flags: TX marks an echo of the adapter's own
transmission, NODATA a header without response, CSUM/PARITY/SYNC/BIT_ERROR an error frame. Timestamps are the
canlib timer in milliseconds.

LINlib numbers its channels like CANlib, but which channel numbers are LIN channels is not known without opening
them, so **Detect** offers nothing for this interface: take the number from Kvaser Device Guide.
"""

import ctypes
import sys
from typing import Any, List, Optional

import can

from can_explorer.core.lin import MAX_FRAME_ID
from can_explorer.core.lin_adapter import DeviceClock, LinAdapterBus, LinEvent

# canlib/linlib/enums.py
CHANNEL_MASTER, CHANNEL_SLAVE = 1, 2
SETUP_ENHANCED_CHECKSUM, SETUP_VARIABLE_DLC = 1, 2
FLAG_TX, FLAG_RX, FLAG_WAKEUP, FLAG_NODATA = 1, 2, 4, 8
FLAG_CSUM_ERROR, FLAG_PARITY_ERROR, FLAG_SYNC_ERROR, FLAG_BIT_ERROR = 16, 32, 64, 128
ERROR_FLAGS = {
    FLAG_CSUM_ERROR: 'checksum error',
    FLAG_PARITY_ERROR: 'parity error',
    FLAG_SYNC_ERROR: 'sync error',
    FLAG_BIT_ERROR: 'bit error',
}
LIN_OK, LIN_ERR_NOMSG, LIN_ERR_TIMEOUT = 0, -1, -11
LIN_ERRORS = {
    -1: 'no messages',
    -3: 'not running',
    -4: 'running',
    -5: 'master only',
    -6: 'slave only',
    -7: 'parameter error',
    -8: 'hardware not found (is the LIN transceiver powered?)',
    -9: 'out of memory',
    -10: 'no channels available',
    -11: 'timeout',
    -12: 'library not initialised',
    -14: 'invalid handle',
    -16: 'error response from the LIN interface',
    -20: 'the device was removed',
    -23: 'access denied',
    -26: 'not implemented',
}
TIMER_WRAP = 1 << (8 * ctypes.sizeof(ctypes.c_ulong))


class MessageInfo(ctypes.Structure):
    """LINlib's LinMessageInfo (pycanlib structures.MessageInfo)."""

    _fields_ = [
        ('timestamp', ctypes.c_ulong),  # Milliseconds, the canlib timer
        ('synchBreakLength', ctypes.c_ulong),
        ('frameLength', ctypes.c_ulong),
        ('bitrate', ctypes.c_ulong),
        ('checkSum', ctypes.c_ubyte),
        ('idPar', ctypes.c_ubyte),
        ('z', ctypes.c_ushort),
        ('synchEdgeTime', ctypes.c_ulong * 4),
        ('byteTime', ctypes.c_ulong * 8),
    ]


def load_library():
    if sys.platform == 'win32':
        name, loader = 'linlib', ctypes.WinDLL
    elif sys.platform.startswith('linux'):
        name, loader = 'liblinlib.so', ctypes.CDLL
    else:
        raise can.CanInterfaceNotImplementedError('Kvaser LINlib is available for Windows and Linux')
    try:
        library = loader(name)
    except OSError as e:
        raise can.CanInterfaceNotImplementedError(
            f"Cannot load {name}: install Kvaser's drivers and CANlib SDK (Windows) or Linux SDK ({e})"
        ) from e
    library.linInitializeLibrary()
    return library


def event_from_message(frame_id: int, data: bytes, flags: int, timestamp: float) -> Optional[LinEvent]:
    if flags & FLAG_WAKEUP:
        return None
    frame_id &= MAX_FRAME_ID
    errors = [name for bit, name in ERROR_FLAGS.items() if flags & bit]
    if errors:
        return LinEvent(frame_id, data, timestamp=timestamp, error=', '.join(errors))
    if flags & FLAG_NODATA:
        return LinEvent(frame_id, timestamp=timestamp, no_response=True)
    return LinEvent(frame_id, data, timestamp=timestamp, transmitted=bool(flags & FLAG_TX))


class KvaserLinBus(LinAdapterBus):
    """A Kvaser LIN channel. `library` replaces LINlib (tests)."""

    interface_name = 'Kvaser LIN'

    def __init__(self, channel: Any = None, bitrate: Optional[int] = None, library: Any = None, **kwargs: Any):
        self._library = library
        self._ready = False
        super().__init__(channel, bitrate=bitrate, **kwargs)

    def _check(self, status: int, call: str) -> int:
        if status < 0:
            error = can.CanOperationError if self._ready else can.CanInitializationError
            raise error(f'{call} failed: {LIN_ERRORS.get(status, status)}')
        return status

    def _open(self) -> None:
        try:
            number = int(self.device)
        except ValueError as e:
            raise can.CanInitializationError(f'The channel is a CANlib channel number, got {self.device!r}') from e
        self._lib = self._library if self._library is not None else load_library()
        self._clock = DeviceClock(1e-3, TIMER_WRAP)
        kind = CHANNEL_SLAVE if self.mode == 'slave' else CHANNEL_MASTER
        self._handle = self._check(self._lib.linOpenChannel(number, kind), 'linOpenChannel')
        try:
            self._check(self._lib.linSetBitrate(self._handle, self.baudrate), 'linSetBitrate')
            self._check(self._lib.linBusOn(self._handle), 'linBusOn')
            flags = SETUP_VARIABLE_DLC | (0 if self.checksum == 'classic' else SETUP_ENHANCED_CHECKSUM)
            self._lib.linSetupLIN(self._handle, flags, 0)  # Older firmware lacks it: then the stored settings apply
        except BaseException:
            self._lib.linClose(self._handle)
            raise
        self._ready = True

    @staticmethod
    def _buffer(data: bytes):
        return (ctypes.c_ubyte * 8)(*data)

    def _publish(self, frame_id: int, data: bytes, classic: bool) -> None:
        self._check(self._lib.linWriteMessage(self._handle, frame_id, self._buffer(data), len(data)), 'linWriteMessage')

    def _request(self, frame_id: int, length: int, classic: bool) -> None:
        self._check(self._lib.linRequestMessage(self._handle, frame_id), 'linRequestMessage')

    def _respond(self, frame_id: int, data: bytes, classic: bool) -> None:
        self._check(
            self._lib.linUpdateMessage(self._handle, frame_id, self._buffer(data), len(data)), 'linUpdateMessage'
        )

    def _poll(self, timeout: float) -> List[LinEvent]:
        events: List[LinEvent] = []
        wait = max(0, int(timeout * 1000))
        while True:
            frame_id, length, flags = ctypes.c_uint(0), ctypes.c_uint(0), ctypes.c_uint(0)
            data = (ctypes.c_ubyte * 8)()
            info = MessageInfo()
            status = self._lib.linReadMessageWait(
                self._handle,
                ctypes.byref(frame_id),
                data,
                ctypes.byref(length),
                ctypes.byref(flags),
                ctypes.byref(info),
                ctypes.c_ulong(wait),
            )
            if status in (LIN_ERR_NOMSG, LIN_ERR_TIMEOUT):
                return events
            self._check(status, 'linReadMessageWait')
            event = event_from_message(
                frame_id.value, bytes(data[: min(length.value, 8)]), flags.value, self._clock(info.timestamp)
            )
            if event is not None:
                events.append(event)
            wait = 0  # Then take what else is waiting without blocking

    def _close(self) -> None:
        try:
            self._lib.linBusOff(self._handle)
        finally:
            self._lib.linClose(self._handle)
