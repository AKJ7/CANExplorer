"""Taking part in a FlexRay cluster: a Vector interface as a node (against a simulated XL library) and an Intrepid
interface through icsneopy (against a simulated module)."""

import ctypes
import enum
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from can_explorer.core import flexray_vector as xl
from can_explorer.core.flexray import FlexRayError
from can_explorer.core.flexray_database import load_database
from can_explorer.core.flexray_intrepid import IntrepidFlexRay, node_configuration
from tests.unit.core.test_flexray import FakeXl

DATA = Path(__file__).resolve().parents[2] / 'data'


# Sizes and offsets printed by Vector's vxlapi.h compiled with gcc (pack 4 for the configuration structures,
# pack 8 for the events), as the module documentation says
@pytest.mark.parametrize(
    'structure, field, expected',
    [
        (xl.XLfrClusterConfig, None, 316),
        (xl.XLfrClusterConfig, 'gColdStartAttempts', 16),
        (xl.XLfrClusterConfig, 'pChannelsMTS', 248),
        (xl.XLfrClusterConfig, 'framePresetData', 252),
        (xl.XLfrClusterConfig, 'reserved', 256),
        (xl.XLfrChannelConfig, None, 348),
        (xl.XLfrChannelConfig, 'xlFrClusterConfig', 32),
        (xl.XLfrMode, None, 128),
        (xl.XL_FR_TX_FRAME_EV, None, 266),
        (xl.XL_FR_TX_FRAME_EV, 'payloadLength', 6),
        (xl.XL_FR_TX_FRAME_EV, 'txMode', 7),
        (xl.XL_FR_TX_FRAME_EV, 'data', 12),
        (xl.XL_FR_RX_FRAME_EV, None, 262),
        (xl.XL_FR_RX_FRAME_EV, 'cycleCount', 6),
        (xl.XL_FR_RX_FRAME_EV, 'data', 8),
        (xl.XL_FR_SYMBOL_WINDOW_EV, None, 16),
        (xl.XL_FR_SYMBOL_WINDOW_EV, 'cycleCount', 8),
        (xl.XL_FR_STATUS_EV, None, 8),
        (xl.XL_FR_ERROR_EV, None, 28),
        (xl.XL_FR_ERROR_EV, 'errorInfo', 8),
        (xl.XLfrEvent, None, 512),
    ],
)
def test_vector_node_structures_match_vxlapi(structure, field, expected):
    assert (ctypes.sizeof(structure) if field is None else getattr(structure, field).offset) == expected


class FakeXlNode(FakeXl):
    def __init__(self, configured=True):
        super().__init__()
        self.configured = configured
        self.transmitted = []

    def xlOpenPort(self, port, name, access, permission, queue, version, bus):
        self.calls.append(('open', access.value, permission._obj.value))
        port._obj.value = 3
        return 0

    def xlFrGetChannelConfiguration(self, port, access, config):
        config = config._obj
        if self.configured:
            config.status = xl.XL_FR_CHANNEL_CFG_STATUS_VALID_CLUSTER_CFG
            config.xlFrClusterConfig.gMacroPerCycle = 5000
        return 0

    def xlFrSetConfiguration(self, port, access, config):
        self.calls.append(('configure', config._obj.gMacroPerCycle, config._obj.gNumberOfStaticSlots))
        return 0

    def xlFrSetMode(self, port, access, mode):
        self.calls.append(('mode', mode._obj.frMode, mode._obj.frStartupAttributes))
        return 0

    def xlFrInitStartupAndSync(self, port, access, event):
        tx = event._obj.tagData.frTxFrame
        self.calls.append(('startup', tx.slotID, tx.flags, event._obj.flagsChip))
        return 0

    def xlFrTransmit(self, port, access, event):
        event = event._obj
        tx = event.tagData.frTxFrame
        self.transmitted.append(
            (event.tag, tx.slotID, tx.offset, tx.repetition, tx.payloadLength, tx.txMode, event.flagsChip,
             bytes(tx.data[: tx.payloadLength * 2]))
        )  # fmt: skip
        return 0


def _event(tag, channel=xl.XL_FR_CHANNEL_A, nanoseconds=1000):
    return xl.XLfrEvent(tag=tag, flagsChip=channel, timeStamp=nanoseconds)


def test_vector_node_joins_with_the_channel_configuration_and_sends():
    library = FakeXlNode()
    node = xl.VectorFlexRayNode(library)
    node.start(0x10, coldstart=True, key_slot=12, key_payload=bytes(8), key_channels='A+B')
    assert library.calls[:5] == [
        ('open', 0x10, 0x10),  # Asking for init access
        ('configure', 5000, 0),  # The channel's own configuration, applied
        ('mode', xl.XL_FR_MODE_NORMAL, xl.XL_FR_MODE_COLDSTART_LEADING),
        ('startup', 12, xl.FLAG_STARTUP | xl.FLAG_SYNC, xl.XL_FR_CHANNEL_A | xl.XL_FR_CHANNEL_B),
        ('activate', xl.XL_BUS_TYPE_FLEXRAY, xl.XL_ACTIVATE_RESET_CLOCK),
    ]
    node.transmit(40, 1, 4, 'B', bytes(range(6)))
    assert library.transmitted[-1] == (
        xl.XL_FR_TX_FRAME,
        40,
        1,
        4,
        3,
        xl.XL_FR_TX_MODE_CYCLIC,
        xl.XL_FR_CHANNEL_B,
        bytes(range(6)),
    )
    node.stop_transmit(40)
    assert library.transmitted[-1][5] == xl.XL_FR_TX_MODE_NONE
    with pytest.raises(FlexRayError, match='even number'):
        node.transmit(40, 1, 4, 'B', b'\x01')

    # Received, sent (acknowledged), symbols, the POC state and errors
    rx = _event(xl.XL_FR_RX_FRAME)
    frame = rx.tagData.frRxFrame
    frame.slotID, frame.cycleCount, frame.payloadLength, frame.flags = 12, 3, 2, xl.FLAG_SYNC | 0x0400
    ctypes.memmove(frame.data, b'\x01\x02\x03\x04', 4)
    ack = _event(xl.XL_FR_TXACK_FRAME, xl.XL_FR_CHANNEL_B)
    ack.tagData.frTxFrame.slotID, ack.tagData.frTxFrame.payloadLength, ack.tagData.frTxFrame.offset = 40, 1, 1
    symbol = _event(xl.XL_FR_SYMBOL_WINDOW)
    symbol.tagData.frSymbolWindow.symbol, symbol.tagData.frSymbolWindow.cycleCount = xl.XL_FR_SYMBOL_CAS, 5
    status = _event(xl.XL_FR_STATUS)
    status.tagData.frStatus.statusType = 0x02
    error = _event(xl.XL_FR_ERROR)
    error.tagData.frError.tag = 0x04
    library.events = [rx, ack, symbol, status, error]
    received, sent = node.receive()
    assert (received.slot, received.cycle, received.payload, received.sync, received.errors) == (
        12, 3, b'\x01\x02\x03\x04', True, ['content']
    )  # fmt: skip
    assert (sent.slot, sent.channel, sent.cycle, sent.transmitted) == (40, 'B', 1, True)
    assert [(e.kind, e.text) for e in node.take_events()] == [
        ('symbol', 'CAS'),
        ('status', 'POC state: normal active'),
        ('error', 'Clock correction failure'),
    ]
    assert node.take_events() == []
    node.close()


def test_vector_node_configuration_from_a_file(tmp_path):
    library = FakeXlNode(configured=False)
    node = xl.VectorFlexRayNode(library)
    with pytest.raises(FlexRayError, match='no FlexRay cluster configuration'):
        node.start(0x10)
    path = tmp_path / 'cluster.json'
    path.write_text(json.dumps({'gMacroPerCycle': 3636, 'gNumberOfStaticSlots': '0x3C'}))
    node.start(0x10, xl.load_config_file(path))
    assert ('configure', 3636, 60) in library.calls
    lines = tmp_path / 'cluster.txt'
    lines.write_text('gMacroPerCycle = 3636  # macroticks\ngdStaticSlot: 50,\n')
    assert xl.load_config_file(lines) == {'gMacroPerCycle': 3636, 'gdStaticSlot': 50}
    with pytest.raises(FlexRayError, match='Not XLfrClusterConfig fields: gMacro'):
        xl.config_from_values({'gMacro': 1})
    assert xl.config_values(xl.config_from_values({'pKeySlotUsedForSync': 1}))['pKeySlotUsedForSync'] == 1


# --- Intrepid, against a simulated icsneopy ---


class _Symbol(enum.Enum):
    None_ = 0
    Unknown = 1
    Wakeup = 2
    CAS = 3


def _fake_icsneopy():
    class Channel(enum.Enum):
        A = 1
        B = 2
        AB = 3

    class CRCStatus(enum.Enum):
        OK = 0
        Error = 1
        NoCRC = 2

    class Configuration(SimpleNamespace):
        pass

    class FlexRayMessage(SimpleNamespace):
        def __init__(self, **values):
            defaults = dict(
                slotid=0, cycle=0, cycle_repetition=0, channel=Channel.A, data=(), null_frame=False,
                sync_frame=False, startup_frame=False, payload_preamble=False, header_crc=0,
                header_crc_status=CRCStatus.OK, frame_crc_status=CRCStatus.OK, symbol=_Symbol.None_,
                transmitted=False, error=False, timestamp=0, network=None,
            )  # fmt: skip
            super().__init__(**{**defaults, **values})

    class Controller:
        def __init__(self, number):
            self.number = number
            self.configuration = None
            self.coldstart = None

        def get_network(self):
            return f'FLEXRAY_0{self.number + 1}'

        def set_allow_coldstart(self, allowed):
            self.coldstart = allowed

        def set_configuration(self, cluster, controller):
            self.configuration = (cluster, controller)

        def set_start_when_going_online(self, start):
            self.start = start

    class Device:
        def __init__(self):
            self.controllers = [Controller(0), Controller(1)]
            self.messages = []
            self.sent = []
            self.state = []

        def get_extension(self, name):
            return name == 'FlexRay'

        def describe(self):
            return 'neoVI FIRE 3 FlexRay FD0001'

        def get_flexray_controllers(self):
            return self.controllers

        def open(self):
            self.state.append('open')
            return True

        def go_online(self):
            self.state.append('online')
            return True

        def enable_message_polling(self):
            self.state.append('polling')

        def get_messages(self):
            messages, self.messages = self.messages, []
            return messages, True

        def transmit(self, message):
            self.sent.append(message)
            return True

        def go_offline(self):
            self.state.append('offline')

        def close(self):
            self.state.append('close')

    device = Device()
    module = SimpleNamespace(
        find_all_devices=lambda: [device],
        FlexRayMessage=FlexRayMessage,
        FlexRay=SimpleNamespace(
            Channel=Channel,
            CRCStatus=CRCStatus,
            Symbol=_Symbol,
            Cluster=SimpleNamespace(Configuration=Configuration, SpeedType=SimpleNamespace(FLEXRAY_BAUDRATE_10M='10M')),
            Controller=SimpleNamespace(Configuration=Configuration),
        ),
    )
    return module, device


def test_intrepid_listens_with_the_device_configuration():
    module, device = _fake_icsneopy()
    intrepid = IntrepidFlexRay(module)
    assert intrepid.channels() == [
        {'name': 'neoVI FIRE 3 FlexRay FD0001 FlexRay 1', 'device': 0, 'controller': 0},
        {'name': 'neoVI FIRE 3 FlexRay FD0001 FlexRay 2', 'device': 0, 'controller': 1},
    ]
    intrepid.start(0, 1)
    assert device.state == ['open', 'online', 'polling'] and device.controllers[1].configuration is None
    message = module.FlexRayMessage
    device.messages = [
        message(slotid=12, cycle=3, data=(1, 2, 3, 4), sync_frame=True, timestamp=1_000_000_000),
        message(slotid=13, cycle=3, channel=module.FlexRay.Channel.AB, timestamp=1_000_100_000,
                frame_crc_status=module.FlexRay.CRCStatus.Error),
        message(symbol=_Symbol.CAS, timestamp=1_000_200_000),
        'not FlexRay',
    ]  # fmt: skip
    first, second_a, second_b = intrepid.receive()
    assert (first.slot, first.cycle, first.payload, first.sync, first.channel) == (
        12,
        3,
        b'\x01\x02\x03\x04',
        True,
        'A',
    )
    assert (second_a.channel, second_b.channel, second_a.errors) == ('A', 'B', ['frame CRC'])
    assert second_a.timestamp - first.timestamp == pytest.approx(100e-6, abs=1e-6)
    assert [(e.kind, e.text) for e in intrepid.take_events()] == [('symbol', 'CAS')]
    intrepid.transmit(40, 1, 4, 'A+B', b'\x0a\x0b')
    sent = device.sent[-1]
    assert (sent.slotid, sent.cycle, sent.cycle_repetition, sent.channel, sent.data, sent.network) == (
        40, 1, 4, module.FlexRay.Channel.AB, (10, 11), 'FLEXRAY_02'
    )  # fmt: skip
    with pytest.raises(FlexRayError, match='even number'):
        intrepid.transmit(40, 1, 4, 'A', b'\x01')
    intrepid.close()
    assert device.state[-2:] == ['offline', 'close']


def test_intrepid_node_configuration_from_the_description():
    db = load_database(DATA / 'flexray_pdus.arxml')
    cluster, controller, missing = node_configuration(db, 'EngineEcu')
    assert cluster['speed'] == 'FLEXRAY_BAUDRATE_10M' and cluster['cycle_duration_micro_sec'] == 5000
    assert cluster['macrotick_duration_micro_sec'] == 1 and cluster['static_slot_macroticks'] == 50
    assert cluster['payload_length_of_static_slot_in_words'] == 4
    assert controller == {
        'key_slot_id': 12,
        'key_slot_used_for_startup': True,
        'key_slot_used_for_sync': True,
        'micro_per_cycle': 200000,
        'channel_a': True,
        'channel_b': True,
    }
    assert 'COLD-START-ATTEMPTS' in missing and 'LISTEN-TIMEOUT' in missing  # The fixture leaves them out
    with pytest.raises(FlexRayError, match="no FlexRay controller of 'Gateway'"):
        node_configuration(db, 'Gateway')

    module, device = _fake_icsneopy()
    intrepid = IntrepidFlexRay(module)
    intrepid.start(0, 0, (cluster, controller), coldstart=True)
    configured_cluster, configured_controller = device.controllers[0].configuration
    assert configured_cluster.speed == '10M' and configured_cluster.number_of_static_slots == 50
    assert configured_controller.key_slot_id == 12 and device.controllers[0].coldstart is True


def test_icsneopy_missing():
    import sys

    saved = sys.modules.get('icsneopy')
    sys.modules['icsneopy'] = None  # Import fails
    try:
        with pytest.raises(FlexRayError, match='pip install icsneopy'):
            IntrepidFlexRay()
    finally:
        if saved is None:
            del sys.modules['icsneopy']
        else:
            sys.modules['icsneopy'] = saved
