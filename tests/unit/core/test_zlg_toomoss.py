"""The ZLG and Toomoss interfaces against simulated vendor libraries: the calls they make, with which parameters, and
the frames they exchange. The structure layouts are checked against the vendors' headers."""

import ctypes

import can
import pytest

from can_explorer.core import toomoss, zlg
from can_explorer.core.toomoss import ToomossBus
from can_explorer.core.zlg import ZlgBus

# Sizes and offsets printed by the vendors' own headers (zlgcan.h/canframe.h of ZLG's CAN_lib.zip; usb2can.h and
# usb2canfd.h of Toomoss's USB2XXX_Example) compiled for x86-64, as on 64-bit Windows
VENDOR_LAYOUTS = [
    (zlg.can_frame, None, 16),
    (zlg.can_frame, 'data', 8),
    (zlg.canfd_frame, None, 72),
    (zlg.canfd_frame, 'flags', 5),
    (zlg.ZCAN_Transmit_Data, None, 20),
    (zlg.ZCAN_Receive_Data, 'timestamp', 16),
    (zlg.ZCAN_Receive_Data, None, 24),
    (zlg.ZCAN_TransmitFD_Data, None, 76),
    (zlg.ZCAN_ReceiveFD_Data, 'timestamp', 72),
    (zlg.ZCAN_ReceiveFD_Data, None, 80),
    (zlg.ZCAN_CHANNEL_INIT_CONFIG, None, 32),
    (toomoss.CANFD_MSG, None, 76),
    (toomoss.CANFD_MSG, 'TimeStamp', 8),
    (toomoss.CANFD_MSG, 'Data', 12),
    (toomoss.CANFD_INIT_CONFIG, None, 20),
    (toomoss.CANFD_INIT_CONFIG, 'NBT_BRP', 4),
    (toomoss.CAN_MSG, None, 20),
    (toomoss.CAN_MSG, 'Data', 11),
    (toomoss.CAN_MSG, 'TimeStampHigh', 19),
    (toomoss.CAN_INIT_CONFIG, None, 12),
    (toomoss.CAN_INIT_CONFIG, 'CAN_Mode', 7),
]


@pytest.mark.parametrize('structure, field, expected', VENDOR_LAYOUTS)
def test_structures_match_the_vendor_headers(structure, field, expected):
    actual = ctypes.sizeof(structure) if field is None else getattr(structure, field).offset
    assert actual == expected
    config = zlg.ZCAN_CHANNEL_INIT_CONFIG
    assert config.u.offset + zlg._CanConfig.mode.offset == 19  # can.mode
    assert config.u.offset + zlg._CanFdConfig.mode.offset == 25  # canfd.mode


def _structure(pointer, kind):
    """The structure a byref/array argument points to."""
    if isinstance(pointer, kind):
        return pointer
    return getattr(pointer, '_obj', None) or ctypes.cast(pointer, ctypes.POINTER(kind)).contents


class FakeZlg:
    """zlgcan.dll: one device whose channels loop frames back, echoing sent frames when asked to."""

    def __init__(self, open_ok=True):
        self.open_ok = open_ok
        self.values = []
        self.config = None
        self.pending = {zlg.TYPE_CAN: [], zlg.TYPE_CANFD: []}
        self.sent = []
        self.closed = False

    def ZCAN_OpenDevice(self, device_type, index, reserved):
        self.opened = (device_type, index)
        return 0x1000 if self.open_ok else 0

    def ZCAN_SetValue(self, device, path, value):
        self.values.append((path.decode(), value.decode()))
        return zlg.STATUS_OK

    def ZCAN_InitCAN(self, device, channel, config):
        self.config = zlg.ZCAN_CHANNEL_INIT_CONFIG.from_buffer_copy(_structure(config, zlg.ZCAN_CHANNEL_INIT_CONFIG))
        self.values.append(('init', str(channel)))
        return 0x2000

    def ZCAN_StartCAN(self, channel):
        return zlg.STATUS_OK

    def ZCAN_ResetCAN(self, channel):
        return zlg.STATUS_OK

    def ZCAN_CloseDevice(self, device):
        self.closed = True
        return zlg.STATUS_OK

    def ZCAN_GetReceiveNum(self, channel, kind):
        return len(self.pending[kind])

    def _receive(self, kind, buffer, count):
        items = self.pending[kind][:count]
        del self.pending[kind][:count]
        for target, (frame, timestamp) in zip(buffer, items, strict=False):
            target.frame, target.timestamp = frame, timestamp
        return len(items)

    def ZCAN_Receive(self, channel, buffer, count, wait):
        return self._receive(zlg.TYPE_CAN, buffer, count)

    def ZCAN_ReceiveFD(self, channel, buffer, count, wait):
        return self._receive(zlg.TYPE_CANFD, buffer, count)

    def _transmit(self, kind, item):
        self.sent.append(item)
        if item.transmit_type == zlg.TRANSMIT_SELF_RECEPTION:  # Echoed back, flagged
            frame = type(item.frame).from_buffer_copy(item.frame)
            if kind == zlg.TYPE_CANFD:
                frame.flags |= zlg.TX_ECHO_FLAG
            else:
                frame.pad |= zlg.TX_ECHO_FLAG
            self.pending[kind].append((frame, 5_000_000))
        return 1

    def ZCAN_Transmit(self, channel, item, count):
        return self._transmit(zlg.TYPE_CAN, zlg.ZCAN_Transmit_Data.from_buffer_copy(item._obj))

    def ZCAN_TransmitFD(self, channel, item, count):
        return self._transmit(zlg.TYPE_CANFD, zlg.ZCAN_TransmitFD_Data.from_buffer_copy(item._obj))


def test_zlg_channels():
    assert zlg.parse_channel('USBCANFD-200U:1:1') == (41, 1, 1, 'USBCANFD-200U')
    assert zlg.parse_channel('usbcan_2e_u') == (21, 0, 0, 'USBCAN-2E-U')
    assert zlg.parse_channel('41:0:1') == (41, 0, 1, 'USBCANFD-200U')
    with pytest.raises(can.CanInitializationError, match='Unknown ZLG device'):
        zlg.parse_channel('nonsense')
    with pytest.raises(can.CanInitializationError, match='DEVICE'):
        zlg.parse_channel('USBCAN2:x')


def test_zlg_canfd_device():
    library = FakeZlg()
    bus = ZlgBus('USBCANFD-200U:0:1', bitrate=500000, data_bitrate=2000000, fd=True, library=library)
    assert library.opened == (41, 0)
    assert library.values == [
        ('1/canfd_standard', '0'),
        ('1/canfd_abit_baud_rate', '500000'),
        ('1/canfd_dbit_baud_rate', '2000000'),
        ('init', '1'),
        ('1/initenal_resistance', '1'),  # After ZCAN_InitCAN, as the manual says
    ]
    assert library.config.can_type == zlg.TYPE_CANFD and library.config.canfd.mode == 0
    bus.send(
        can.Message(arbitration_id=0x18DA10F1, is_extended_id=True, is_fd=True, bitrate_switch=True, data=bytes(12))
    )
    sent = library.sent[-1]
    assert sent.frame.can_id == 0x18DA10F1 | zlg.CAN_EFF_FLAG and sent.frame.len == 12
    assert sent.frame.flags == zlg.CANFD_BRS and sent.transmit_type == zlg.TRANSMIT_NORMAL
    bus.send(can.Message(arbitration_id=0x123, data=[1, 2]))  # Classic frames on a CAN FD device
    assert library.sent[-1].frame.can_dlc == 2

    received = zlg.canfd_frame(can_id=0x7E8, len=3, flags=zlg.CANFD_BRS)
    received.data[:3] = [0x02, 0x50, 0x03]
    library.pending[zlg.TYPE_CANFD].append((received, 1_000_000))
    error = zlg.can_frame(can_id=zlg.CAN_ERR_FLAG | 0x04, can_dlc=8)
    library.pending[zlg.TYPE_CAN].append((error, 1_500_000))
    first, second = bus.recv(0.5), bus.recv(0.5)
    assert first.is_error_frame and not first.is_fd  # Classic frames are read first
    assert second.is_fd and second.bitrate_switch and second.data == bytes([0x02, 0x50, 0x03]) and second.is_rx
    assert second.timestamp - first.timestamp == pytest.approx(-0.5)  # Microseconds from the device
    assert bus.recv(0.05) is None
    bus.shutdown()
    assert library.closed


def test_zlg_classic_device_and_self_reception():
    library = FakeZlg()
    bus = ZlgBus('USBCAN-2E-U', bitrate=250000, receive_own_messages=True, listen_only=True, library=library)
    assert library.values == [('0/baud_rate', '250000'), ('init', '0')]  # No termination key for these
    assert library.config.can_type == zlg.TYPE_CAN and library.config.can.mode == 1
    assert library.config.can.acc_mask == 0xFFFFFFFF
    bus.send(can.Message(arbitration_id=0x100, data=[0xAA], is_remote_frame=False))
    assert library.sent[-1].transmit_type == zlg.TRANSMIT_SELF_RECEPTION
    echo = bus.recv(0.5)
    assert echo.arbitration_id == 0x100 and not echo.is_rx  # TX_ECHO_FLAG: our own frame
    with pytest.raises(can.CanOperationError, match='cannot send CAN FD'):
        bus.send(can.Message(arbitration_id=0x100, is_fd=True, data=bytes(12)))
    bus.shutdown()
    quiet = ZlgBus('USBCANFD-100U', fd=True, library=FakeZlg())
    library = quiet._lib
    library._transmit(zlg.TYPE_CAN, zlg.ZCAN_Transmit_Data(transmit_type=zlg.TRANSMIT_SELF_RECEPTION))
    assert quiet.recv(0.05) is None  # Echoes are dropped unless asked for
    quiet.shutdown()


def test_zlg_errors():
    with pytest.raises(can.CanInterfaceNotImplementedError, match='classic CAN interface'):
        ZlgBus('USBCAN2', fd=True, library=FakeZlg())
    with pytest.raises(can.CanInitializationError, match='takes the bit rates'):
        ZlgBus('USBCAN2', bitrate=33333, library=FakeZlg())
    with pytest.raises(can.CanInitializationError, match='data bit rates'):
        ZlgBus('USBCANFD-200U', fd=True, data_bitrate=3000000, library=FakeZlg())
    with pytest.raises(can.CanInitializationError, match='Cannot open the ZLG'):
        ZlgBus('USBCAN2', library=FakeZlg(open_ok=False))


class FakeToomoss:
    """The USB2XXX library: adapters whose channels loop frames back, flagging sent frames."""

    def __init__(self, devices=(0x1234,), speed_arg=True):
        self.devices = list(devices)
        self.opened = []
        self.init = {}
        self.pending = []
        self.sent = []
        self.closed = []
        if speed_arg:
            self.CANFD_GetCANSpeedArg = self._speed_arg

    def USB_ScanDevice(self, handles):
        for index, handle in enumerate(self.devices):
            handles[index] = handle
        return len(self.devices)

    def USB_OpenDevice(self, handle):
        self.opened.append(handle)
        return 1

    def USB_CloseDevice(self, handle):
        self.closed.append(handle)
        return 1

    def _speed_arg(self, handle, config, nbt, dbt):
        config = config._obj
        config.NBT_BRP, config.NBT_SEG1, config.NBT_SEG2, config.NBT_SJW = 1, 59, 20, 2
        config.DBT_BRP, config.DBT_SEG1, config.DBT_SEG2, config.DBT_SJW = 1, 14, 5, 2
        return 0

    def CANFD_Init(self, handle, index, config):
        self.init = {'fd': toomoss.CANFD_INIT_CONFIG.from_buffer_copy(config._obj), 'index': index}
        return 0

    def CAN_GetCANSpeedArg(self, handle, config, bitrate):
        config._obj.CAN_BRP, config._obj.CAN_BS1, config._obj.CAN_BS2, config._obj.CAN_SJW = 4, 15, 5, 1
        return 0

    def CAN_Init(self, handle, index, config):
        self.init = {'can': toomoss.CAN_INIT_CONFIG.from_buffer_copy(config._obj), 'index': index}
        return 0

    def CANFD_StartGetMsg(self, handle, index):
        return 0

    CAN_StartGetMsg = CANFD_StartGetMsg

    def CANFD_StopGetMsg(self, handle, index):
        return 0

    CAN_StopGetMsg = CANFD_StopGetMsg

    def CANFD_SendMsg(self, handle, index, item, count):
        message = toomoss.CANFD_MSG.from_buffer_copy(item._obj)
        self.sent.append(message)
        echo = toomoss.CANFD_MSG.from_buffer_copy(message)
        echo.Flags |= toomoss.CANFD_MSG_FLAG_TXD
        self.pending.append(echo)
        return 1

    def CAN_SendMsg(self, handle, index, item, count):
        message = toomoss.CAN_MSG.from_buffer_copy(item._obj)
        self.sent.append(message)
        echo = toomoss.CAN_MSG.from_buffer_copy(message)
        echo.RemoteFlag |= toomoss.CAN_SENT_FLAG
        self.pending.append(echo)
        return 1

    def CANFD_GetMsg(self, handle, index, buffer, size):
        items, self.pending = self.pending[:size], self.pending[size:]
        for slot, item in enumerate(items):
            buffer[slot] = item
        return len(items)

    CAN_GetMsgWithSize = CANFD_GetMsg


def test_toomoss_canfd():
    library = FakeToomoss(devices=(0x11, 0x22))
    bus = ToomossBus('1:1', bitrate=500000, data_bitrate=2000000, fd=True, termination=False, library=library)
    assert library.opened == [0x22] and library.init['index'] == 1
    config = library.init['fd']
    assert (config.Mode, config.ISOCRCEnable, config.RetrySend, config.ResEnable) == (0, 1, 1, 0)
    assert (config.NBT_BRP, config.NBT_SEG1, config.NBT_SEG2, config.DBT_SEG1) == (1, 59, 20, 14)
    bus.send(can.Message(arbitration_id=0x1ABCDE, is_extended_id=True, is_fd=True, bitrate_switch=True, data=bytes(16)))
    sent = library.sent[-1]
    assert sent.ID == 0x1ABCDE | toomoss.CANFD_MSG_FLAG_IDE and sent.DLC == 16
    assert sent.Flags == toomoss.CANFD_MSG_FLAG_FDF | toomoss.CANFD_MSG_FLAG_BRS
    assert bus.recv(0.05) is None  # The echo (TXD) is dropped
    received = toomoss.CANFD_MSG(ID=0x7E8, DLC=3, Flags=toomoss.CANFD_MSG_FLAG_FDF, TimeStamp=100, TimeStampHigh=1)
    received.Data[:3] = [1, 2, 3]
    library.pending.append(received)
    message = bus.recv(0.5)
    assert message.is_fd and message.data == bytes([1, 2, 3]) and message.channel == 1 and message.is_rx
    bus.shutdown()
    assert library.closed == [0x22]


def test_toomoss_classic_can_and_timing():
    library = FakeToomoss()
    bus = ToomossBus('0', bitrate=250000, listen_only=True, receive_own_messages=True, library=library)
    config = library.init['can']
    assert config.CAN_Mode == toomoss.CAN_MODE_SILENT | toomoss.CAN_MODE_TERMINATION
    assert (config.CAN_BRP, config.CAN_BS1) == (4, 15)  # As CAN_GetCANSpeedArg filled them
    bus.send(can.Message(arbitration_id=0x123, data=[9, 8], is_extended_id=False))
    echo = bus.recv(0.5)
    assert echo.arbitration_id == 0x123 and echo.data == bytes([9, 8]) and not echo.is_rx
    with pytest.raises(can.CanOperationError, match='Open the Toomoss adapter with CAN FD'):
        bus.send(can.Message(arbitration_id=1, is_fd=True, data=bytes(12)))
    bus.shutdown()

    # Without CANFD_GetCANSpeedArg, the timing comes from the header's formula, at 75 %, as Toomoss's examples
    library = FakeToomoss(speed_arg=False)
    ToomossBus('0', bitrate=500000, data_bitrate=2000000, fd=True, library=library).shutdown()
    config = library.init['fd']
    assert (config.NBT_BRP, config.NBT_SEG1, config.NBT_SEG2) == (1, 59, 20)
    assert (config.DBT_BRP, config.DBT_SEG1, config.DBT_SEG2) == (1, 14, 5)
    assert toomoss.canfd_timing(1000000, 160) == (1, 29, 10, 2)
    for bitrate in (125000, 250000, 500000, 1000000):
        brp, seg1, seg2, _ = toomoss.canfd_timing(bitrate, 160)
        assert toomoss.CANFD_CLOCK // (brp * (1 + seg1 + seg2)) == bitrate


def test_toomoss_errors():
    with pytest.raises(can.CanInitializationError, match='No Toomoss adapter 1'):
        ToomossBus('1', library=FakeToomoss())
    with pytest.raises(can.CanInitializationError, match='no listen only mode'):
        ToomossBus('0', fd=True, listen_only=True, library=FakeToomoss())
    with pytest.raises(can.CanInitializationError, match='DEVICE'):
        toomoss.parse_channel('a:b')
    with pytest.raises(can.CanInitializationError, match='cannot be made from the 40 MHz clock'):
        toomoss.canfd_timing(333333, 160)
