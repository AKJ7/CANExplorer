"""LIN adapters with vendor APIs (PEAK PLIN, Kvaser LINlib, Vector XL) against simulated libraries: structure
layouts from the vendors' headers, opening as master/slave, sending in the sllin convention, and received frames."""

import asyncio
import ctypes

import can
import pytest

from can_explorer.core import kvaser_lin, plin, vector_lin
from can_explorer.core.lin import protected_id
from can_explorer.core.lin_adapter import LinEvent, lin_frame_of, parse_lin_channel, uses_classic_checksum
from can_explorer.core.sources import BusConfig, LiveSource


def obj(ref):
    """The ctypes object behind a byref() argument."""
    return ref._obj


# Shared


def test_parse_lin_channel():
    assert parse_lin_channel('1') == ('1', 'master')
    assert parse_lin_channel('2@slave') == ('2', 'slave')
    assert parse_lin_channel(' 0 @ Master ') == ('0', 'master')
    with pytest.raises(ValueError):
        parse_lin_channel('1@monitor')


def test_checksum_model_and_frames_to_send():
    assert uses_classic_checksum(0x3C, 'enhanced') and uses_classic_checksum(0x3D, 'enhanced')
    assert not uses_classic_checksum(0x10, 'enhanced') and uses_classic_checksum(0x10, 'classic')
    assert lin_frame_of(can.Message(arbitration_id=0x10, is_extended_id=False, data=b'\x01\x02')) == (0x10, b'\x01\x02')
    assert lin_frame_of(can.Message(arbitration_id=0x11, is_extended_id=False, is_remote_frame=True, dlc=2)) == (
        0x11,
        b'',
    )
    for bad in (
        can.Message(arbitration_id=0x40, is_extended_id=False, data=b'\x01'),
        can.Message(arbitration_id=0x10, is_extended_id=True, data=b'\x01'),
        can.Message(arbitration_id=0x10, is_extended_id=False, data=b''),
    ):
        with pytest.raises(can.CanOperationError):
            lin_frame_of(bad)


# PEAK PLIN-API


@pytest.mark.parametrize(
    ('structure', 'size', 'offsets'),
    [
        # Sizes and offsets from the comments of PLinApi.h (pack 8)
        (plin.TLINMsg, 13, {'Data': 4, 'Checksum': 12}),
        (plin.TLINRcvMsg, 40, {'Data': 5, 'Checksum': 13, 'ErrorFlags': 16, 'TimeStamp': 24, 'hHw': 32}),
        (plin.TLINFrameEntry, 14, {'Flags': 4, 'InitialData': 6}),
        (plin.TLINHardwareStatus, 8, {'FreeOnSchedulePool': 4, 'ReceiveBufferOverrun': 6}),
    ],
)
def test_plin_structures_match_the_header(structure, size, offsets):
    assert ctypes.sizeof(structure) == size
    for field, offset in offsets.items():
        assert getattr(structure, field).offset == offset


class FakePlin:
    """PLinApi.dll: one hardware (handle 1); records the calls, queues received messages."""

    def __init__(self, mode=plin.MOD_NONE, baudrate=0):
        self.calls = []
        self.status_mode, self.baudrate = mode, baudrate
        self.entries = {}
        self.written = []
        self.queue = []

    def LIN_RegisterClient(self, name, window, client):  # noqa: N802 - the API's names
        obj(client).value = 7
        return 0

    def LIN_ConnectClient(self, client, hardware):  # noqa: N802
        self.calls.append(('connect', hardware))
        return 0 if hardware == 1 else plin.ERR_OK + 5  # errIllegalHardware

    def LIN_GetStatus(self, hardware, status):  # noqa: N802
        obj(status).Mode = self.status_mode
        return 0

    def LIN_GetHardwareParam(self, hardware, parameter, buffer, size):  # noqa: N802
        if parameter == plin.HWP_BAUDRATE:
            obj(buffer).value = self.baudrate
        return 0

    def LIN_InitializeHardware(self, client, hardware, mode, baudrate):  # noqa: N802
        self.calls.append(('init', mode, baudrate))
        return 0

    def LIN_SetClientFilter(self, client, hardware, mask):  # noqa: N802
        self.calls.append(('filter', mask.value))
        return 0

    def LIN_SetFrameEntry(self, client, hardware, entry):  # noqa: N802
        entry = obj(entry)
        self.entries[entry.FrameId] = (entry.Direction, entry.Flags, bytes(entry.InitialData[: entry.Length]))
        return 0

    def LIN_Write(self, client, hardware, message):  # noqa: N802
        message = obj(message)
        self.written.append(
            (message.FrameId, message.Direction, message.Length, message.ChecksumType, bytes(message.Data))
        )
        return 0

    def LIN_Read(self, client, message):  # noqa: N802
        if not self.queue:
            return plin.ERR_RCV_QUEUE_EMPTY
        ctypes.memmove(ctypes.byref(obj(message)), ctypes.byref(self.queue.pop(0)), ctypes.sizeof(plin.TLINRcvMsg))
        return 0

    def LIN_GetErrorText(self, error, language, buffer, size):  # noqa: N802
        buffer.value = b'Hardware handle is invalid.'
        return 0

    def LIN_DisconnectClient(self, client, hardware):  # noqa: N802
        self.calls.append(('disconnect',))
        return 0

    def LIN_RemoveClient(self, client):  # noqa: N802
        self.calls.append(('remove',))
        return 0

    def LIN_GetAvailableHardware(self, buffer, size, count):  # noqa: N802
        buffer[0] = 1
        obj(count).value = 1
        return 0

    def received(self, frame_id, data=b'', direction=plin.DIR_SUBSCRIBER, errors=0, timestamp=1_000_000, kind=0):
        message = plin.TLINRcvMsg(
            Type=kind,
            FrameId=protected_id(frame_id),
            Length=len(data),
            Direction=direction,
            ErrorFlags=errors,
            TimeStamp=timestamp,
        )
        message.Data[: len(data)] = list(data)
        self.queue.append(message)


def test_plin_master_initialises_sends_and_receives():
    library = FakePlin()
    bus = plin.PlinBus('1', bitrate=19200, library=library, receive_own_messages=False)
    try:
        assert ('init', plin.MOD_MASTER, 19200) in library.calls
        assert ('filter', 0xFFFFFFFFFFFFFFFF) in library.calls
        bus.send(can.Message(arbitration_id=0x10, is_extended_id=False, data=b'\x01\x02'))
        bus.send(can.Message(arbitration_id=0x3D, is_extended_id=False, is_remote_frame=True, dlc=0))
        published, requested = library.written
        assert published[:4] == (protected_id(0x10), plin.DIR_PUBLISHER, 2, plin.CST_ENHANCED)
        assert published[4][:2] == b'\x01\x02'
        assert requested[:4] == (protected_id(0x3D), plin.DIR_SUBSCRIBER_AUTO_LENGTH, 8, plin.CST_CLASSIC)

        library.received(0x10, b'\x01\x02', direction=plin.DIR_PUBLISHER)  # Our own response: hidden
        library.received(0x3D, b'\x06\x50\x01', timestamp=2_000_000)
        library.received(0x20, errors=plin.MSG_ERR_SLAVE_NOT_RESPONDING | plin.MSG_ERR_TIMEOUT)
        library.received(0x21, b'\x05', errors=plin.MSG_ERR_CHECKSUM)
        library.received(0, kind=5)  # mstOverrun status message: skipped
        response = bus.recv(1)
        assert (response.arbitration_id, response.data, response.is_rx) == (0x3D, b'\x06\x50\x01', True)
        header = bus.recv(1)
        assert header.is_remote_frame and header.arbitration_id == 0x20
        error = bus.recv(1)
        assert error.is_error_frame and error.arbitration_id == 0x21
        assert bus.recv(0.01) is None
    finally:
        bus.shutdown()
    assert ('disconnect',) in library.calls and ('remove',) in library.calls


def test_plin_slave_listens_to_everything_and_sets_responses():
    library = FakePlin()
    bus = plin.PlinBus('1@slave', bitrate=10417, library=library)
    try:
        assert ('init', plin.MOD_SLAVE, 10417) in library.calls
        assert len(library.entries) == 64 and library.entries[5][0] == plin.DIR_SUBSCRIBER_AUTO_LENGTH
        bus.send(can.Message(arbitration_id=0x05, is_extended_id=False, data=b'\xaa\xbb'))
        assert library.entries[5] == (plin.DIR_PUBLISHER, plin.FRAME_FLAG_RESPONSE_ENABLE, b'\xaa\xbb')
        with pytest.raises(can.CanOperationError, match='master'):
            bus.send(can.Message(arbitration_id=0x05, is_extended_id=False, is_remote_frame=True))
    finally:
        bus.shutdown()


def test_plin_joins_a_running_adapter_and_reports_errors():
    library = FakePlin(mode=plin.MOD_MASTER, baudrate=19200)
    bus = plin.PlinBus('1', bitrate=19200, library=library)
    bus.shutdown()
    assert not [call for call in library.calls if call[0] == 'init']  # Not re-initialised
    with pytest.raises(can.CanInitializationError, match='Hardware handle is invalid'):
        plin.PlinBus('2', library=FakePlin())
    with pytest.raises(can.CanInitializationError):
        plin.PlinBus('one', library=FakePlin())
    with pytest.raises(can.CanInitializationError, match='baud'):
        plin.PlinBus('1', bitrate=500_000, library=FakePlin())


def test_plin_listen_only_is_a_quiet_slave():
    library = FakePlin()
    bus = plin.PlinBus('1', listen_only=True, library=library)
    try:
        assert bus.mode == 'slave'
        with pytest.raises(can.CanOperationError, match='listen only'):
            bus.send(can.Message(arbitration_id=0x05, is_extended_id=False, data=b'\x01'))
    finally:
        bus.shutdown()


def test_plin_detect():
    assert plin.PlinBus._detect_available_configs(FakePlin()) == [
        {'interface': 'plin', 'channel': '1', 'name': 'PEAK LIN channel 1'}
    ]


# Kvaser LINlib


class FakeLinlib:
    def __init__(self):
        self.calls = []
        self.queue = []  # (status, id, data, flags, timestamp)

    def linOpenChannel(self, number, kind):  # noqa: N802
        self.calls.append(('open', number, kind))
        return 3 if number == 0 else -8

    def linSetBitrate(self, handle, bps):  # noqa: N802
        self.calls.append(('bitrate', bps))
        return 0

    def linBusOn(self, handle):  # noqa: N802
        self.calls.append(('on',))
        return 0

    def linSetupLIN(self, handle, flags, bps):  # noqa: N802
        self.calls.append(('setup', flags))
        return -26  # Not implemented in old firmware: ignored

    def linWriteMessage(self, handle, frame_id, data, length):  # noqa: N802
        self.calls.append(('write', frame_id, bytes(data[:length])))
        return 0

    def linRequestMessage(self, handle, frame_id):  # noqa: N802
        self.calls.append(('request', frame_id))
        return 0

    def linUpdateMessage(self, handle, frame_id, data, length):  # noqa: N802
        self.calls.append(('update', frame_id, bytes(data[:length])))
        return 0

    def linReadMessageWait(self, handle, frame_id, data, length, flags, info, timeout):  # noqa: N802
        if not self.queue:
            return kvaser_lin.LIN_ERR_NOMSG
        identifier, payload, flag, timestamp = self.queue.pop(0)
        obj(frame_id).value, obj(length).value, obj(flags).value = identifier, len(payload), flag
        data[: len(payload)] = list(payload)
        obj(info).timestamp = timestamp
        return 0

    def linBusOff(self, handle):  # noqa: N802
        self.calls.append(('off',))
        return 0

    def linClose(self, handle):  # noqa: N802
        self.calls.append(('close',))
        return 0


def test_kvaser_lin_master():
    library = FakeLinlib()
    bus = kvaser_lin.KvaserLinBus('0', bitrate=19200, library=library, receive_own_messages=True)
    try:
        assert library.calls[:3] == [('open', 0, kvaser_lin.CHANNEL_MASTER), ('bitrate', 19200), ('on',)]
        assert ('setup', kvaser_lin.SETUP_VARIABLE_DLC | kvaser_lin.SETUP_ENHANCED_CHECKSUM) in library.calls
        bus.send(can.Message(arbitration_id=0x22, is_extended_id=False, data=b'\x10\x20'))
        bus.send(can.Message(arbitration_id=0x23, is_extended_id=False, is_remote_frame=True))
        assert ('write', 0x22, b'\x10\x20') in library.calls and ('request', 0x23) in library.calls
        library.queue += [
            (0x22, b'\x10\x20', kvaser_lin.FLAG_TX, 100),
            (0x23, b'', kvaser_lin.FLAG_RX | kvaser_lin.FLAG_NODATA, 110),
            (0x24, b'\x01', kvaser_lin.FLAG_RX | kvaser_lin.FLAG_CSUM_ERROR, 120),
            (0x00, b'', kvaser_lin.FLAG_WAKEUP, 130),
            (0x25, b'\x09', kvaser_lin.FLAG_RX, 1100),
        ]
        echo, header, error, response = (bus.recv(1) for _ in range(4))
        assert echo.arbitration_id == 0x22 and not echo.is_rx
        assert header.is_remote_frame and header.arbitration_id == 0x23
        assert error.is_error_frame and error.arbitration_id == 0x24
        assert response.data == b'\x09' and response.timestamp - echo.timestamp == pytest.approx(1.0)
    finally:
        bus.shutdown()
    assert library.calls[-2:] == [('off',), ('close',)]


def test_kvaser_lin_slave_and_errors():
    library = FakeLinlib()
    bus = kvaser_lin.KvaserLinBus('0@slave', library=library, checksum='classic')
    try:
        assert ('open', 0, kvaser_lin.CHANNEL_SLAVE) in library.calls
        assert ('setup', kvaser_lin.SETUP_VARIABLE_DLC) in library.calls
        bus.send(can.Message(arbitration_id=0x30, is_extended_id=False, data=b'\x01'))
        assert ('update', 0x30, b'\x01') in library.calls
    finally:
        bus.shutdown()
    with pytest.raises(can.CanInitializationError, match='transceiver powered'):
        kvaser_lin.KvaserLinBus('1', library=FakeLinlib())
    with pytest.raises(can.CanInterfaceNotImplementedError):
        kvaser_lin.KvaserLinBus('0', fd=True, library=FakeLinlib())


# Vector XL


def test_vector_lin_structures_match_vxlapi():
    # vxlapi.h: pack 1; s_xl_event is 16 bytes of header and the 32-byte s_xl_tag_data
    assert ctypes.sizeof(vector_lin.XLlinStatPar) == 16
    assert ctypes.sizeof(vector_lin.s_xl_lin_msg) == 13 and vector_lin.s_xl_lin_msg.data.offset == 4
    assert ctypes.sizeof(vector_lin.XLevent) == 48 and vector_lin.XLevent.timeStamp.offset == 8
    assert vector_lin.XLevent.tagData.offset == 16


class FakeXl:
    def __init__(self, permission=True):
        from can.interfaces.vector.xlclass import XLdriverConfig

        self.config = XLdriverConfig()
        self.config.channelCount = 2
        self.config.channel[0].name = b'VN1640A Channel 1'
        self.config.channel[0].channelMask = 1
        self.config.channel[0].channelBusCapabilities = 0x1  # CAN only
        self.config.channel[1].name = b'VN1640A Channel 2'
        self.config.channel[1].channelMask = 2
        self.config.channel[1].channelBusCapabilities = vector_lin.XL_BUS_COMPATIBLE_LIN
        self.permission = permission
        self.calls = []
        self.events = []

    def xlOpenDriver(self):  # noqa: N802
        return 0

    def xlCloseDriver(self):  # noqa: N802
        self.calls.append(('closedriver',))
        return 0

    def xlGetDriverConfig(self, config):  # noqa: N802
        ctypes.memmove(ctypes.byref(obj(config)), ctypes.byref(self.config), ctypes.sizeof(self.config))
        return 0

    def xlOpenPort(self, port, name, mask, permission, size, version, bus_type):  # noqa: N802
        obj(port).value = 5
        obj(permission).value = mask.value if self.permission else 0
        self.calls.append(('open', mask.value, bus_type))
        return 0

    def xlLinSetChannelParams(self, port, mask, parameters):  # noqa: N802
        self.calls.append(('params', parameters.LINMode, parameters.baudrate, parameters.LINVersion))
        return 0

    def xlLinSetDLC(self, port, mask, lengths):  # noqa: N802
        self.calls.append(('dlc', bytes(lengths)))
        return 0

    def xlActivateChannel(self, port, mask, bus_type, flags):  # noqa: N802
        self.calls.append(('activate', flags))
        return 0

    def xlLinSetSlave(self, port, mask, frame_id, data, length, checksum):  # noqa: N802
        self.calls.append(('slave', frame_id, bytes(data[:length]), checksum))
        return 0

    def xlLinSendRequest(self, port, mask, frame_id, flags):  # noqa: N802
        self.calls.append(('request', frame_id))
        return 0

    def xlReceive(self, port, count, event):  # noqa: N802
        if not self.events:
            return vector_lin.XL_ERR_QUEUE_IS_EMPTY
        ctypes.memmove(ctypes.byref(obj(event)), ctypes.byref(self.events.pop(0)), ctypes.sizeof(vector_lin.XLevent))
        return 0

    def xlDeactivateChannel(self, port, mask):  # noqa: N802
        self.calls.append(('deactivate',))
        return 0

    def xlClosePort(self, port):  # noqa: N802
        self.calls.append(('closeport',))
        return 0

    def xlGetErrorString(self, status):  # noqa: N802
        return b'XL_ERROR'

    def lin_event(self, tag, frame_id=0, data=b'', flags=0, nanoseconds=0):
        event = vector_lin.XLevent(tag=tag, timeStamp=nanoseconds)
        if tag == vector_lin.XL_LIN_NOANS:
            event.tagData.linMsgApi.linNoAns = frame_id
        else:
            message = event.tagData.linMsgApi.linMsg
            message.id, message.dlc, message.flags = frame_id, len(data), flags
            message.data[: len(data)] = list(data)
        self.events.append(event)


def test_vector_lin_master():
    library = FakeXl()
    bus = vector_lin.VectorLinBus('1', bitrate=19200, library=library, dlc={0x10: 2})
    try:
        assert ('open', 2, vector_lin.XL_BUS_TYPE_LIN) in library.calls
        assert ('params', vector_lin.XL_LIN_MASTER, 19200, vector_lin.XL_LIN_VERSION_2_0) in library.calls
        lengths = next(call[1] for call in library.calls if call[0] == 'dlc')
        assert lengths[0x10] == 2 and lengths[0x11] == vector_lin.XL_LIN_UNDEFINED_DLC
        assert ('activate', vector_lin.XL_ACTIVATE_RESET_CLOCK) in library.calls
        assert 'VN1640A Channel 2' in bus.channel_info
        bus.send(can.Message(arbitration_id=0x10, is_extended_id=False, data=b'\x01\x02'))
        assert library.calls[-2:] == [
            ('slave', 0x10, b'\x01\x02', vector_lin.XL_LIN_CALC_CHECKSUM_ENHANCED),
            ('request', 0x10),
        ]
        bus.send(can.Message(arbitration_id=0x3C, is_extended_id=False, data=b'\x01' * 8))
        assert library.calls[-2][3] == vector_lin.XL_LIN_CALC_CHECKSUM  # Diagnostic frames: classic

        library.lin_event(vector_lin.XL_LIN_MSG, 0x10, b'\x01\x02', vector_lin.XL_LIN_MSGFLAG_TX)
        library.lin_event(vector_lin.XL_LIN_MSG, 0x11, b'\x07', nanoseconds=500_000_000)
        library.lin_event(vector_lin.XL_LIN_NOANS, 0x12)
        library.lin_event(vector_lin.XL_LIN_MSG, 0x13, b'\x01', vector_lin.XL_LIN_MSGFLAG_CRCERROR)
        library.lin_event(vector_lin.XL_LIN_SLEEP)
        response, header, error = bus.recv(1), bus.recv(1), bus.recv(1)
        assert (response.arbitration_id, response.data, response.is_rx) == (0x11, b'\x07', True)
        assert header.is_remote_frame and header.arbitration_id == 0x12
        assert error.is_error_frame and error.arbitration_id == 0x13
        assert bus.recv(0.01) is None
    finally:
        bus.shutdown()
    assert library.calls[-3:] == [('deactivate',), ('closeport',), ('closedriver',)]


def test_vector_lin_slave_channel_choice_and_access():
    library = FakeXl()
    bus = vector_lin.VectorLinBus('1@slave', library=library, checksum='classic')
    try:
        assert ('params', vector_lin.XL_LIN_SLAVE, 19200, vector_lin.XL_LIN_VERSION_1_3) in library.calls
        bus.send(can.Message(arbitration_id=0x20, is_extended_id=False, data=b'\x05'))
        assert library.calls[-1] == ('slave', 0x20, b'\x05', vector_lin.XL_LIN_CALC_CHECKSUM)
    finally:
        bus.shutdown()
    with pytest.raises(can.CanInitializationError, match='channels with LIN: 1'):
        vector_lin.VectorLinBus('0', library=FakeXl())
    with pytest.raises(can.CanInitializationError, match='init access'):
        vector_lin.VectorLinBus('1', library=FakeXl(permission=False))
    assert vector_lin.VectorLinBus._detect_available_configs(FakeXl()) == [
        {'interface': 'vector_lin', 'channel': '1', 'name': 'VN1640A Channel 2'}
    ]


def test_lin_event_defaults():
    event = LinEvent(0x10, b'\x01')
    assert not event.transmitted and not event.no_response and not event.error


# The connection passes the LIN baud rate to these adapters


async def test_live_source_passes_the_lin_baud_rate(monkeypatch):
    seen = {}

    def fake_bus(**options):
        seen.update(options)
        raise can.CanInitializationError('stop here')

    monkeypatch.setattr(can, 'Bus', fake_bus)
    for interface, expected in (('plin', 9600), ('socketcan', None)):
        seen.clear()
        source = LiveSource(BusConfig(interface, '1', bus_type='LIN', lin_baudrate=9600))
        with pytest.raises(Exception, match='stop here'):
            source.open(asyncio.get_running_loop())
        assert seen.get('bitrate') == expected
