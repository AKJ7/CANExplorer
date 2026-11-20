"""PEAK-System LIN interfaces (PLIN-USB, and the LIN channels of the PCAN-USB Pro and PCAN-USB Pro FD) through
PEAK's PLIN-API: a python-can bus (interface "plin", class PlinBus) presenting LIN in the sllin convention, as
master or slave (see core/lin_adapter.py).

EXPERIMENTAL: written from PEAK's PLIN-API package (PLinApi.h and the PLIN-API documentation, v3.1.1, 2025) and
tested against a simulated library, not yet on a device. Windows only: PLinApi.dll comes with PEAK's LIN driver
setup (PEAK provides no PLIN-API for Linux or macOS).

The channel is the hardware handle the PLIN-API assigns to a LIN channel, as **Detect** lists them (`1`, `2`; a
PCAN-USB Pro has two), with an optional `@slave`. The adapter is initialised with the connection's baud rate in
the chosen mode; when another application (PLIN-View Pro) already uses it in the same mode and baud rate it is
joined as it is, since re-initialising would disturb that application.

Used: LIN_RegisterClient, LIN_ConnectClient, LIN_GetStatus/LIN_GetHardwareParam (hwpBaudrate),
LIN_InitializeHardware, LIN_SetClientFilter (all 64 identifiers), LIN_Write (master: a publisher frame for a
header with response, a subscriber-autolength frame for a header only; the identifier with its parity bits, the
checksum computed by the hardware), LIN_SetFrameEntry (slave: subscriber-autolength entries so that every frame is
received, a publisher entry for each response set), LIN_Read, LIN_DisconnectClient, LIN_RemoveClient. Received
messages: type mstStandard only; direction dirPublisher marks a response the adapter sent itself; the error flags
MSG_ERR_SLAVE_NOT_RESPONDING without data make a header without response, the others an error frame.
"""

import ctypes
import sys
import time
from typing import Any, Dict, List, Optional

import can

from can_explorer.core.lin import MAX_FRAME_ID, protected_id
from can_explorer.core.lin_adapter import DeviceClock, LinAdapterBus, LinEvent

# PLinApi.h
INVALID_LIN_HANDLE = 0
FRAME_FLAG_RESPONSE_ENABLE = 0x1
MSG_ERR_INCONSISTENT_SYNC, MSG_ERR_ID_PARITY_BIT0, MSG_ERR_ID_PARITY_BIT1 = 0x1, 0x2, 0x4
MSG_ERR_SLAVE_NOT_RESPONDING, MSG_ERR_TIMEOUT, MSG_ERR_CHECKSUM = 0x8, 0x10, 0x20
MSG_ERR_GND_SHORT, MSG_ERR_VBAT_SHORT, MSG_ERR_SLOT_DELAY, MSG_ERR_OTHER_RESPONSE = 0x40, 0x80, 0x100, 0x200
ERROR_NAMES = {
    MSG_ERR_INCONSISTENT_SYNC: 'sync error',
    MSG_ERR_ID_PARITY_BIT0: 'parity error',
    MSG_ERR_ID_PARITY_BIT1: 'parity error',
    MSG_ERR_TIMEOUT: 'incomplete response',
    MSG_ERR_CHECKSUM: 'checksum error',
    MSG_ERR_GND_SHORT: 'bus shorted to ground',
    MSG_ERR_VBAT_SHORT: 'bus shorted to battery',
}
HWP_NAME, HWP_CHANNEL_NUMBER, HWP_BAUDRATE, HWP_SERIAL_NUMBER = 1, 3, 6, 11
MST_STANDARD = 0
DIR_DISABLED, DIR_PUBLISHER, DIR_SUBSCRIBER, DIR_SUBSCRIBER_AUTO_LENGTH = 0, 1, 2, 3
CST_CUSTOM, CST_CLASSIC, CST_ENHANCED, CST_AUTO = 0, 1, 2, 3
MOD_NONE, MOD_SLAVE, MOD_MASTER = 0, 1, 2
ERR_OK, ERR_RCV_QUEUE_EMPTY = 0, 3
LANGUAGE_ENGLISH = 0x09
POLL_INTERVAL = 0.002  # The PLIN-API has no blocking read; poll the queue this often


class TLINMsg(ctypes.Structure):  # noqa: N801 - the names of PLinApi.h (pack 8, natural alignment)
    _fields_ = [
        ('FrameId', ctypes.c_ubyte),
        ('Length', ctypes.c_ubyte),
        ('Direction', ctypes.c_ubyte),
        ('ChecksumType', ctypes.c_ubyte),
        ('Data', ctypes.c_ubyte * 8),
        ('Checksum', ctypes.c_ubyte),
    ]


class TLINRcvMsg(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('Type', ctypes.c_ubyte),
        ('FrameId', ctypes.c_ubyte),
        ('Length', ctypes.c_ubyte),
        ('Direction', ctypes.c_ubyte),
        ('ChecksumType', ctypes.c_ubyte),
        ('Data', ctypes.c_ubyte * 8),
        ('Checksum', ctypes.c_ubyte),
        ('ErrorFlags', ctypes.c_int32),
        ('TimeStamp', ctypes.c_uint64),  # Microseconds
        ('hHw', ctypes.c_uint16),
    ]


class TLINFrameEntry(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('FrameId', ctypes.c_ubyte),
        ('Length', ctypes.c_ubyte),
        ('Direction', ctypes.c_ubyte),
        ('ChecksumType', ctypes.c_ubyte),
        ('Flags', ctypes.c_uint16),
        ('InitialData', ctypes.c_ubyte * 8),
    ]


class TLINHardwareStatus(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('Mode', ctypes.c_ubyte),
        ('Status', ctypes.c_ubyte),
        ('FreeOnSendQueue', ctypes.c_ubyte),
        ('FreeOnSchedulePool', ctypes.c_uint16),
        ('ReceiveBufferOverrun', ctypes.c_uint16),
    ]


def load_library():
    if sys.platform != 'win32':
        raise can.CanInterfaceNotImplementedError('PEAK LIN interfaces are used through the PLIN-API on Windows')
    try:
        return ctypes.WinDLL('PLinApi')
    except OSError as e:
        raise can.CanInterfaceNotImplementedError(
            f'Cannot load PLinApi.dll: install the LIN driver setup from PEAK-System ({e})'
        ) from e


def event_from_message(message: TLINRcvMsg, clock) -> Optional[LinEvent]:
    """A received PLIN message as a LIN event (None for status messages)."""
    if message.Type != MST_STANDARD:
        return None
    frame_id = message.FrameId & MAX_FRAME_ID
    flags = message.ErrorFlags
    length = min(message.Length, 8)
    data = bytes(message.Data[:length])
    timestamp = clock(message.TimeStamp)
    transmitted = message.Direction == DIR_PUBLISHER and not flags & MSG_ERR_OTHER_RESPONSE
    errors = sorted({name for bit, name in ERROR_NAMES.items() if flags & bit})
    if flags & MSG_ERR_SLAVE_NOT_RESPONDING and not data:
        return LinEvent(frame_id, timestamp=timestamp, no_response=True)
    if errors:
        return LinEvent(frame_id, data, timestamp=timestamp, error=', '.join(errors))
    return LinEvent(frame_id, data, timestamp=timestamp, transmitted=transmitted)


class PlinBus(LinAdapterBus):
    """A LIN channel of a PEAK interface. `library` replaces PLinApi.dll (tests)."""

    interface_name = 'PLIN'

    def __init__(self, channel: Any = None, bitrate: Optional[int] = None, library: Any = None, **kwargs: Any):
        self._library = library
        self._ready = False
        super().__init__(channel, bitrate=bitrate, **kwargs)

    def _open(self) -> None:
        try:
            self.hardware = int(self.device)
        except ValueError as e:
            raise can.CanInitializationError(
                f'The channel is a PLIN hardware handle (1, 2, …), got {self.device!r}'
            ) from e
        self._lib = self._library if self._library is not None else load_library()
        self._client = ctypes.c_ubyte(INVALID_LIN_HANDLE)
        self._clock = DeviceClock(1e-6)
        self._check(self._lib.LIN_RegisterClient(b'CANExplorer', 0, ctypes.byref(self._client)), 'LIN_RegisterClient')
        try:
            self._check(self._lib.LIN_ConnectClient(self._client, self.hardware), 'LIN_ConnectClient')
            self._initialise()
            self._check(
                self._lib.LIN_SetClientFilter(self._client, self.hardware, ctypes.c_uint64(0xFFFFFFFFFFFFFFFF)),
                'LIN_SetClientFilter',
            )
        except BaseException:
            self._lib.LIN_RemoveClient(self._client)
            raise
        self._ready = True

    def _check(self, status: int, call: str) -> None:
        if status == ERR_OK:
            return
        text = ''
        try:
            buffer = ctypes.create_string_buffer(256)
            if self._lib.LIN_GetErrorText(status, LANGUAGE_ENGLISH, buffer, 256) == ERR_OK:
                text = buffer.value.decode(errors='replace')
        except Exception:  # noqa: BLE001 - only for the message
            pass
        error = can.CanOperationError if self._ready else can.CanInitializationError
        raise error(f'{call} failed: {text or status}')

    def _initialise(self) -> None:
        mode = MOD_SLAVE if self.mode == 'slave' else MOD_MASTER
        status = TLINHardwareStatus()
        baudrate = ctypes.c_int(0)
        joined = False
        if self._lib.LIN_GetStatus(self.hardware, ctypes.byref(status)) == ERR_OK and status.Mode == mode:
            if (
                self._lib.LIN_GetHardwareParam(self.hardware, HWP_BAUDRATE, ctypes.byref(baudrate), 4) == ERR_OK
                and baudrate.value == self.baudrate
            ):
                joined = True  # Already running as asked, e.g. by PLIN-View Pro: do not disturb it
        if not joined:
            self._check(
                self._lib.LIN_InitializeHardware(self._client, self.hardware, mode, self.baudrate),
                'LIN_InitializeHardware',
            )
            if mode == MOD_SLAVE:
                for frame_id in range(MAX_FRAME_ID + 1):  # Receive every frame, answer none yet
                    entry = TLINFrameEntry(
                        FrameId=frame_id, Length=8, Direction=DIR_SUBSCRIBER_AUTO_LENGTH, ChecksumType=CST_AUTO
                    )
                    self._check(
                        self._lib.LIN_SetFrameEntry(self._client, self.hardware, ctypes.byref(entry)),
                        'LIN_SetFrameEntry',
                    )

    def _write(self, frame_id: int, direction: int, data: bytes, length: int, classic: bool) -> None:
        message = TLINMsg(
            FrameId=protected_id(frame_id),
            Length=length,
            Direction=direction,
            ChecksumType=CST_CLASSIC if classic else CST_ENHANCED,
        )
        message.Data[: len(data)] = list(data)
        self._check(self._lib.LIN_Write(self._client, self.hardware, ctypes.byref(message)), 'LIN_Write')

    def _publish(self, frame_id: int, data: bytes, classic: bool) -> None:
        self._write(frame_id, DIR_PUBLISHER, data, len(data), classic)

    def _request(self, frame_id: int, length: int, classic: bool) -> None:
        # The length is not used for autolength frames, but must be 1 to 8
        direction = DIR_SUBSCRIBER if length else DIR_SUBSCRIBER_AUTO_LENGTH
        self._write(frame_id, direction, b'', length or 8, classic)

    def _respond(self, frame_id: int, data: bytes, classic: bool) -> None:
        entry = TLINFrameEntry(
            FrameId=frame_id,
            Length=len(data),
            Direction=DIR_PUBLISHER,
            ChecksumType=CST_CLASSIC if classic else CST_ENHANCED,
            Flags=FRAME_FLAG_RESPONSE_ENABLE,
        )
        entry.InitialData[: len(data)] = list(data)
        self._check(self._lib.LIN_SetFrameEntry(self._client, self.hardware, ctypes.byref(entry)), 'LIN_SetFrameEntry')

    def _poll(self, timeout: float) -> List[LinEvent]:
        deadline = time.monotonic() + timeout
        events: List[LinEvent] = []
        message = TLINRcvMsg()
        while True:
            status = self._lib.LIN_Read(self._client, ctypes.byref(message))
            if status == ERR_OK:
                event = event_from_message(message, self._clock)
                if event is not None:
                    events.append(event)
                continue
            if status != ERR_RCV_QUEUE_EMPTY:
                self._check(status, 'LIN_Read')
            if events or time.monotonic() >= deadline:
                return events
            time.sleep(POLL_INTERVAL)

    def _close(self) -> None:
        try:
            self._lib.LIN_DisconnectClient(self._client, self.hardware)
        finally:
            self._lib.LIN_RemoveClient(self._client)

    @staticmethod
    def _detect_available_configs(library: Any = None) -> List[Dict[str, Any]]:
        """The LIN channels the PLIN-API knows, by hardware handle."""
        try:
            lib = library if library is not None else load_library()
        except can.CanError:
            return []
        handles = (ctypes.c_uint16 * 16)()
        count = ctypes.c_int(0)
        if lib.LIN_GetAvailableHardware(handles, ctypes.sizeof(handles), ctypes.byref(count)) != ERR_OK:
            return []
        configs = []
        for handle in handles[: min(count.value, 16)]:
            name = ctypes.create_string_buffer(48)
            number = ctypes.c_int(0)
            lib.LIN_GetHardwareParam(handle, HWP_NAME, name, 48)
            lib.LIN_GetHardwareParam(handle, HWP_CHANNEL_NUMBER, ctypes.byref(number), 4)
            label = name.value.decode(errors='replace') or 'PEAK LIN'
            configs.append({'interface': 'plin', 'channel': str(handle), 'name': f'{label} channel {number.value + 1}'})
        return configs
