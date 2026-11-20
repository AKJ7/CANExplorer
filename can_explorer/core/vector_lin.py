"""Vector LIN channels (VN1611, VN1630/VN1640 with a LINpiggy, VN7640 …) through Vector's XL Driver Library: a
python-can bus (interface "vector_lin", class VectorLinBus) presenting LIN in the sllin convention, as master or
slave (see core/lin_adapter.py).

EXPERIMENTAL: written from Vector's vxlapi.h (the XL Driver Library header, "customer version", 2014; the same
header the FlexRay backend uses) and tested against a simulated library, not yet on a device. Windows only
(vxlapi64.dll / vxlapi.dll from the Vector driver setup).

The channel is the index of the channel in the Vector driver configuration (as Vector Hardware Config and
**Detect** list them; only channels with LIN, XL_BUS_COMPATIBLE_LIN, are accepted), with an optional `@slave`.

Used: xlOpenDriver, xlGetDriverConfig, xlOpenPort (XL_BUS_TYPE_LIN, XL_INTERFACE_VERSION), xlLinSetChannelParams
(XLlinStatPar: master/slave, baud rate, LIN 2.0 or 1.3 for the classic checksum), xlLinSetDLC, xlActivateChannel,
xlLinSetSlave (the response a channel gives to a header: a slave's response, and the master's own response before
it sends the header), xlLinSendRequest (master: a header), xlReceive with the XL_LIN_MSG (a frame; flag
XL_LIN_MSGFLAG_TX for an echo of the channel's own response), XL_LIN_NOANS (a header without response),
XL_LIN_ERRMSG/XL_LIN_SYNCERR/XL_LIN_CRCINFO (errors) events, xlDeactivateChannel, xlClosePort, xlCloseDriver.

Unconfirmed: the response lengths. xlLinSetDLC must give the length of each frame before activation; vxlapi.h
defines XL_LIN_UNDEFINED_DLC to leave a length undefined, and this backend sets every frame to it, assuming that
the device then takes the length of what is on the bus. Pass `dlc={0x10: 4, …}` to fix lengths if the device
reports errors instead. A master's own response stays set for its identifier (the XL API has no call to send a
response once), so a later header for that identifier is answered with it again.
"""

import ctypes
import sys
import time
from typing import Any, Dict, List, Optional

import can

from can_explorer.core.lin import MAX_FRAME_ID
from can_explorer.core.lin_adapter import LinAdapterBus, LinEvent

# vxlapi.h
XL_SUCCESS, XL_ERR_QUEUE_IS_EMPTY = 0, 10
XL_BUS_TYPE_LIN = 0x00000002
XL_BUS_COMPATIBLE_LIN = XL_BUS_TYPE_LIN
XL_INTERFACE_VERSION = 3
XL_ACTIVATE_RESET_CLOCK = 8
XL_LIN_MASTER, XL_LIN_SLAVE = 1, 2
XL_LIN_VERSION_1_3, XL_LIN_VERSION_2_0 = 0x01, 0x02
XL_LIN_CALC_CHECKSUM, XL_LIN_CALC_CHECKSUM_ENHANCED = 0x100, 0x200
XL_LIN_UNDEFINED_DLC = 0xFF
XL_LIN_MSGFLAG_TX = 0x40
XL_LIN_MSGFLAG_CRCERROR = 0x81
XL_LIN_MSG, XL_LIN_ERRMSG, XL_LIN_SYNCERR, XL_LIN_NOANS = 20, 21, 22, 23
XL_LIN_WAKEUP, XL_LIN_SLEEP, XL_LIN_CRCINFO = 24, 25, 26
RECEIVE_QUEUE_SIZE = 256  # Events; a power of two


class XLlinStatPar(ctypes.Structure):  # noqa: N801 - the names of vxlapi.h (pack 1)
    _pack_ = 1
    _fields_ = [
        ('LINMode', ctypes.c_uint),
        ('baudrate', ctypes.c_int),
        ('LINVersion', ctypes.c_uint),
        ('reserved', ctypes.c_uint),
    ]


class s_xl_lin_msg(ctypes.Structure):  # noqa: N801
    _pack_ = 1
    _fields_ = [
        ('id', ctypes.c_ubyte),
        ('dlc', ctypes.c_ubyte),
        ('flags', ctypes.c_ushort),
        ('data', ctypes.c_ubyte * 8),
        ('crc', ctypes.c_ubyte),
    ]


class s_xl_lin_crc_info(ctypes.Structure):  # noqa: N801
    _pack_ = 1
    _fields_ = [('id', ctypes.c_ubyte), ('flags', ctypes.c_ubyte)]


class _LinMsgApi(ctypes.Union):
    _pack_ = 1
    _fields_ = [
        ('linMsg', s_xl_lin_msg),
        ('linNoAns', ctypes.c_ubyte),  # s_xl_lin_no_ans: the identifier
        ('linCRCinfo', s_xl_lin_crc_info),
    ]


class _TagData(ctypes.Union):
    """s_xl_tag_data: 32 bytes (its largest member, s_xl_can_msg, is 32 bytes)."""

    _pack_ = 1
    _fields_ = [('linMsgApi', _LinMsgApi), ('raw', ctypes.c_ubyte * 32)]


class XLevent(ctypes.Structure):  # noqa: N801 - s_xl_event
    _pack_ = 1
    _fields_ = [
        ('tag', ctypes.c_ubyte),
        ('chanIndex', ctypes.c_ubyte),
        ('transId', ctypes.c_ushort),
        ('portHandle', ctypes.c_ushort),
        ('flags', ctypes.c_ubyte),
        ('reserved', ctypes.c_ubyte),
        ('timeStamp', ctypes.c_uint64),  # Nanoseconds
        ('tagData', _TagData),
    ]


def load_library():
    if sys.platform != 'win32':
        raise can.CanInterfaceNotImplementedError('Vector interfaces are used through the XL Driver Library on Windows')
    name = 'vxlapi64' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'vxlapi'
    try:
        return ctypes.WinDLL(name)
    except OSError as e:
        raise can.CanInterfaceNotImplementedError(
            f'Cannot load {name}.dll: install the Vector driver setup and the XL Driver Library ({e})'
        ) from e


def event_from_xl(event: XLevent, offset: float) -> Optional[LinEvent]:
    timestamp = offset + event.timeStamp / 1e9
    api = event.tagData.linMsgApi
    if event.tag == XL_LIN_MSG:
        message = api.linMsg
        frame_id = message.id & MAX_FRAME_ID
        data = bytes(message.data[: min(message.dlc, 8)])
        if message.flags & XL_LIN_MSGFLAG_CRCERROR == XL_LIN_MSGFLAG_CRCERROR:
            return LinEvent(frame_id, data, timestamp=timestamp, error='checksum error')
        return LinEvent(frame_id, data, timestamp=timestamp, transmitted=bool(message.flags & XL_LIN_MSGFLAG_TX))
    if event.tag == XL_LIN_NOANS:
        return LinEvent(api.linNoAns & MAX_FRAME_ID, timestamp=timestamp, no_response=True)
    if event.tag == XL_LIN_CRCINFO:
        return LinEvent(api.linCRCinfo.id & MAX_FRAME_ID, timestamp=timestamp, error='checksum error')
    if event.tag in (XL_LIN_ERRMSG, XL_LIN_SYNCERR):
        # These events carry no identifier in vxlapi.h; report them on identifier 0
        return LinEvent(0, timestamp=timestamp, error='sync error' if event.tag == XL_LIN_SYNCERR else 'LIN error')
    return None  # Wake-up, sleep and the other buses' events


def lin_channels(lib) -> List[Dict[str, Any]]:
    """The channels of the driver configuration with LIN: {'index', 'name', 'mask', 'serial'}."""
    from can.interfaces.vector.xlclass import XLdriverConfig

    config = XLdriverConfig()
    if lib.xlGetDriverConfig(ctypes.byref(config)) != XL_SUCCESS:
        return []
    found = []
    for index in range(config.channelCount):
        channel = config.channel[index]
        if channel.channelBusCapabilities & XL_BUS_COMPATIBLE_LIN:
            name = bytes(channel.name).split(b'\0', 1)[0].decode(errors='replace')
            found.append(
                {'index': index, 'name': name, 'mask': int(channel.channelMask), 'serial': int(channel.serialNumber)}
            )
    return found


class VectorLinBus(LinAdapterBus):
    """A Vector LIN channel. `library` replaces vxlapi (tests); `dlc` fixes response lengths by identifier."""

    interface_name = 'Vector LIN'

    def __init__(
        self,
        channel: Any = None,
        bitrate: Optional[int] = None,
        library: Any = None,
        dlc: Optional[Dict[int, int]] = None,
        app_name: str = 'CANExplorer',
        **kwargs: Any,
    ):
        self._library = library
        self._dlc = dict(dlc or {})
        self._app_name = app_name
        self._ready = False
        super().__init__(channel, bitrate=bitrate, **kwargs)

    def _check(self, status: int, call: str) -> None:
        if status == XL_SUCCESS:
            return
        text = ''
        try:
            text = self._lib.xlGetErrorString(status)
            text = text.decode() if isinstance(text, bytes) else str(text or '')
        except Exception:  # noqa: BLE001 - only for the message
            pass
        error = can.CanOperationError if self._ready else can.CanInitializationError
        raise error(f'{call} failed: {text or status}')

    def _open(self) -> None:
        self._lib = self._library if self._library is not None else load_library()
        self._check(self._lib.xlOpenDriver(), 'xlOpenDriver')
        self._port = ctypes.c_long(-1)
        try:
            channels = {channel['index']: channel for channel in lin_channels(self._lib)}
            try:
                found = channels[int(self.device)]
            except (ValueError, KeyError) as e:
                listed = ', '.join(f'{index} ({item["name"]})' for index, item in channels.items()) or 'none'
                raise can.CanInitializationError(
                    f'{self.device!r} is not a Vector channel with LIN; channels with LIN: {listed}'
                ) from e
            self._mask = ctypes.c_uint64(found['mask'])
            permission = ctypes.c_uint64(found['mask'])
            self._check(
                self._lib.xlOpenPort(
                    ctypes.byref(self._port),
                    self._app_name.encode(),
                    self._mask,
                    ctypes.byref(permission),
                    RECEIVE_QUEUE_SIZE,
                    XL_INTERFACE_VERSION,
                    XL_BUS_TYPE_LIN,
                ),
                'xlOpenPort',
            )
            if permission.value != found['mask']:
                raise can.CanInitializationError(
                    f'No init access to {found["name"]}: another application configures it; close it first'
                )
            parameters = XLlinStatPar(
                LINMode=XL_LIN_SLAVE if self.mode == 'slave' else XL_LIN_MASTER,
                baudrate=self.baudrate,
                LINVersion=XL_LIN_VERSION_1_3 if self.checksum == 'classic' else XL_LIN_VERSION_2_0,
            )
            self._check(self._lib.xlLinSetChannelParams(self._port, self._mask, parameters), 'xlLinSetChannelParams')
            lengths = (ctypes.c_ubyte * 64)(*([XL_LIN_UNDEFINED_DLC] * 64))
            for frame_id, length in self._dlc.items():
                lengths[int(frame_id) & MAX_FRAME_ID] = int(length)
            self._check(self._lib.xlLinSetDLC(self._port, self._mask, lengths), 'xlLinSetDLC')
            self._check(
                self._lib.xlActivateChannel(self._port, self._mask, XL_BUS_TYPE_LIN, XL_ACTIVATE_RESET_CLOCK),
                'xlActivateChannel',
            )
            self._offset = time.time()  # The clock was reset on activation
            self.channel_info = f'Vector {found["name"]} LIN {self.mode} @ {self.baudrate} baud'
        except BaseException:
            if self._port.value != -1:
                self._lib.xlClosePort(self._port)
            self._lib.xlCloseDriver()
            raise
        self._ready = True

    def _set_slave(self, frame_id: int, data: bytes, classic: bool) -> None:
        buffer = (ctypes.c_ubyte * 8)(*data)
        checksum = XL_LIN_CALC_CHECKSUM if classic else XL_LIN_CALC_CHECKSUM_ENHANCED
        self._check(
            self._lib.xlLinSetSlave(self._port, self._mask, frame_id, buffer, len(data), checksum), 'xlLinSetSlave'
        )

    def _publish(self, frame_id: int, data: bytes, classic: bool) -> None:
        self._set_slave(frame_id, data, classic)  # The master answers its own header
        self._request(frame_id, len(data), classic)

    def _request(self, frame_id: int, length: int, classic: bool) -> None:
        self._check(self._lib.xlLinSendRequest(self._port, self._mask, frame_id, 0), 'xlLinSendRequest')

    def _respond(self, frame_id: int, data: bytes, classic: bool) -> None:
        self._set_slave(frame_id, data, classic)

    def _poll(self, timeout: float) -> List[LinEvent]:
        deadline = time.monotonic() + timeout
        events: List[LinEvent] = []
        event = XLevent()
        while True:
            count = ctypes.c_uint(1)
            status = self._lib.xlReceive(self._port, ctypes.byref(count), ctypes.byref(event))
            if status == XL_SUCCESS:
                converted = event_from_xl(event, self._offset)
                if converted is not None:
                    events.append(converted)
                continue
            if status != XL_ERR_QUEUE_IS_EMPTY:
                self._check(status, 'xlReceive')
            if events or time.monotonic() >= deadline:
                return events
            time.sleep(0.002)

    def _close(self) -> None:
        try:
            self._lib.xlDeactivateChannel(self._port, self._mask)
            self._lib.xlClosePort(self._port)
        finally:
            self._lib.xlCloseDriver()

    @staticmethod
    def _detect_available_configs(library: Any = None) -> List[Dict[str, Any]]:
        try:
            lib = library if library is not None else load_library()
        except can.CanError:
            return []
        if lib.xlOpenDriver() != XL_SUCCESS:
            return []
        try:
            return [
                {'interface': 'vector_lin', 'channel': str(item['index']), 'name': item['name']}
                for item in lin_channels(lib)
            ]
        finally:
            lib.xlCloseDriver()
