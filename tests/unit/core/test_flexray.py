import ctypes
import struct

import pytest

from can_explorer.core import flexray, flexray_vector
from can_explorer.core.flexray import FlexRayError, FlexRayFrame
from can_explorer.core.flexray_database import ClusterSimulator, load_database
from tests.unit.core.test_avtp_acf import _acf_flexray, _ntscf
from tests.unit.core.test_ethernet import eth

DATA = __import__('pathlib').Path(__file__).resolve().parents[2] / 'data'


def test_header_crc_is_crc11_flexray():
    assert flexray.crc11_flexray(b'123456789') == 0x5A3  # The check value of the reveng CRC catalogue

    # The header CRC is that CRC over the 20 header bits; appended to them, the register ends at the catalogue's
    # residue (0x000), which a wrong bit order, length or polynomial would not give
    def register(bits, count):
        crc = 0x01A
        for position in range(count - 1, -1, -1):
            feedback = ((bits >> position) & 1) ^ ((crc >> 10) & 1)
            crc = ((crc << 1) & 0x7FF) ^ (0x385 if feedback else 0)
        return crc

    for sync, startup, slot, words in ((True, False, 12, 4), (False, True, 2047, 127), (True, True, 1, 0)):
        bits = (int(sync) << 19) | (int(startup) << 18) | (slot << 7) | words
        crc = flexray.header_crc(sync, startup, slot, words)
        assert register(bits, 20) == crc and register((bits << 11) | crc, 31) == 0
    assert len({flexray.header_crc(True, True, slot, 4) for slot in range(1, 200)}) > 150  # Depends on the ID


def test_header_round_trip_and_wireshark_layout():
    frame = FlexRayFrame(1.0, 'A', slot=0x1A5, cycle=37, payload=bytes(range(8)), sync=True, startup=True)
    header = flexray.encode_header(frame)
    crc = flexray.header_crc(True, True, 0x1A5, 4)
    # Byte 0: reserved, PPI, NFI (1: data), sync, startup, ID 10..8; byte 1: ID 7..0; then length (7) | CRC (11)
    # | cycle (6), as Wireshark's packet-flexray.h masks them
    assert header[0] == 0x20 | 0x10 | 0x08 | 0x01 and header[1] == 0xA5
    assert header[2] >> 1 == 4 and (((header[2] & 1) << 10) | (header[3] << 2) | (header[4] >> 6)) == crc
    assert header[4] & 0x3F == 37
    fields, length = flexray.decode_header(header)
    assert fields == {
        'preamble': False,
        'null_frame': False,
        'sync': True,
        'startup': True,
        'slot': 0x1A5,
        'header_crc': crc,
        'cycle': 37,
    }
    assert length == 8
    null = flexray.decode_header(flexray.encode_header(FlexRayFrame(0, 'B', 3, 0, bytes(4), null_frame=True)))[0]
    assert null['null_frame']
    with pytest.raises(FlexRayError, match='even number'):
        flexray.encode_header(FlexRayFrame(0, 'A', 1, 0, b'\x01'))
    for bad, message in ((dict(channel='C'), 'channel'), (dict(slot=3000), 'slot'), (dict(cycle=64), 'cycle')):
        with pytest.raises(FlexRayError, match=message):
            FlexRayFrame(**{**dict(timestamp=0, channel='A', slot=1, cycle=0), **bad})


def test_packets_and_errors():
    frame = FlexRayFrame(2.5, 'B', 99, 5, bytes([1, 2, 3, 4]), errors=['frame CRC'])
    packet = flexray.encode_packet(frame)
    assert packet[0] == 0x80 | 0x01 and packet[1] == 0x10  # Channel B, a frame; FCRCERR
    decoded = flexray.decode_packet(packet, 2.5)
    assert (decoded.channel, decoded.slot, decoded.cycle, decoded.payload, decoded.errors) == (
        'B',
        99,
        5,
        bytes([1, 2, 3, 4]),
        ['frame CRC'],
    )
    assert flexray.decode_packet(bytes([0x02, 12]), 0) is None  # A symbol
    truncated = flexray.decode_packet(packet[:-2], 0)  # A measurement device may truncate frames
    assert truncated.payload == bytes([1, 2])
    assert 'frame CRC error' in decoded.describe()


def test_capture_round_trip(tmp_path):
    frames = [FlexRayFrame(1000 + i * 0.001, 'AB'[i % 2], 10 + i, i % 64, bytes([i, i])) for i in range(20)]
    path = tmp_path / 'frames.pcap'
    assert flexray.write_capture(path, frames) == 20
    data = path.read_bytes()
    assert struct.unpack_from('<IHHiIII', data, 0)[6] == flexray.LINKTYPE_FLEXRAY
    read = flexray.read_capture(path)
    assert [(f.channel, f.slot, f.cycle, f.payload) for f in read] == [
        (f.channel, f.slot, f.cycle, f.payload) for f in frames
    ]
    assert read[5].timestamp == pytest.approx(frames[5].timestamp, abs=1e-9)


def test_pcapng_with_flexray_and_acf(tmp_path):
    """A pcapng with a LINKTYPE_FLEXRAY interface and an Ethernet interface carrying ACF FlexRay."""

    def block(kind, body):
        body += bytes(-len(body) % 4)
        return struct.pack('<II', kind, len(body) + 12) + body + struct.pack('<I', len(body) + 12)

    section = block(0x0A0D0D0A, struct.pack('<IHHq', 0x1A2B3C4D, 1, 0, -1))
    flexray_interface = block(1, struct.pack('<HHI', flexray.LINKTYPE_FLEXRAY, 0, 0))
    ethernet_interface = block(1, struct.pack('<HHI', flexray.LINKTYPE_ETHERNET, 0, 0))

    def packet(interface, microseconds, data):
        return block(
            6,
            struct.pack('<IIIII', interface, microseconds >> 32, microseconds & 0xFFFFFFFF, len(data), len(data))
            + data,
        )

    native = flexray.encode_packet(FlexRayFrame(0, 'A', 7, 1, bytes([0xAA, 0xBB])))
    tunnelled = eth(
        '91:e0:f0:00:fe:00', '02:00:00:00:00:01', 0x22F0, _ntscf(_acf_flexray(9, 2, bytes([1, 2, 3, 4]), channel=3))
    )
    path = tmp_path / 'mixed.pcapng'
    path.write_bytes(
        section
        + flexray_interface
        + ethernet_interface
        + packet(0, 1_000_000, native)
        + packet(1, 2_000_000, tunnelled)
    )
    frames = flexray.read_capture(path)
    assert [(f.channel, f.slot, f.cycle, f.payload, f.timestamp) for f in frames] == [
        ('A', 7, 1, bytes([0xAA, 0xBB]), 1.0),
        ('A', 9, 2, bytes([1, 2, 3, 4]), 2.0),  # ACF chan 3: both channels
        ('B', 9, 2, bytes([1, 2, 3, 4]), 2.0),
    ]
    empty = tmp_path / 'empty.pcap'
    flexray.write_capture(empty, [])
    with pytest.raises(FlexRayError, match='has no FlexRay frames'):
        flexray.read_capture(empty)
    with pytest.raises(FlexRayError, match='Not a pcap'):
        (tmp_path / 'text.pcap').write_text('hello, this is not a capture file at all')
        flexray.read_capture(tmp_path / 'text.pcap')


def test_fibex_cluster_decodes_frames():
    db = load_database(DATA / 'flexray_fibex.xml')
    assert db.cluster == 'Powertrain'
    engine, diagnostics = db.definitions
    assert (engine.name, engine.slot, engine.channels, engine.cycles) == ('EngineStatus', 7, {'A', 'B'}, 'every cycle')
    assert (diagnostics.slot, diagnostics.channels, diagnostics.cycles) == (33, {'B'}, 'from 2, every 8')
    # EngineSpeed 2400 rpm (×0.25, little endian), Gear "Second", OilPressure 35.5 (÷10, big endian, MSB at bit 39)
    payload = bytes.fromhex('8025020016300000')
    definition, values = db.decode(FlexRayFrame(0, 'B', 7, 63, payload))
    assert definition is engine and values == {'EngineSpeed': 2400.0, 'Gear': 'Second', 'OilPressure': 35.5}
    assert (
        db.describe(FlexRayFrame(0, 'A', 7, 0, payload))
        == 'EngineStatus: EngineSpeed=2400, Gear=Second, OilPressure=35.5'
    )
    assert db.definition(FlexRayFrame(0, 'B', 33, 10, bytes(4))) is diagnostics  # 2 + 8
    assert db.definition(FlexRayFrame(0, 'B', 33, 3, bytes(4))) is None  # Not one of its cycles
    assert db.definition(FlexRayFrame(0, 'A', 33, 10, bytes(4))) is None  # Not on channel A
    assert db.describe(FlexRayFrame(0, 'A', 7, 0, bytes(8), null_frame=True)) == 'EngineStatus (null frame)'


def test_arxml_cluster_decodes_frames():
    db = load_database(DATA / 'flexray.arxml')
    names = {d.name: d for d in db.definitions}
    assert (names['FrEngine'].channels, names['FrWheels'].cycles, names['FrStatus'].cycles) == (
        {'A', 'B'},
        'from 1, every 4',
        'from 3, every 64',
    )
    assert names['FrEngine'].signal_names == ['EngineSpeed', 'GearPosition', 'OilPressure']
    assert db.definition(FlexRayFrame(0, 'A', 50, 3, bytes(8))).name == 'FrStatus'
    assert db.definition(FlexRayFrame(0, 'A', 50, 4, bytes(8))) is None
    with pytest.raises(FlexRayError, match='no FlexRay cluster'):
        load_database(DATA / 'example.arxml')  # CAN only
    with pytest.raises(FlexRayError, match='neither FIBEX nor AUTOSAR'):
        load_database(DATA / 'example.cdd')


def test_simulated_cluster_follows_the_schedule():
    db = load_database(DATA / 'flexray.arxml')
    simulator = ClusterSimulator(db, cycle_time=0.005, start=0.0)
    frames = simulator.frames_until(0.05)  # 10 cycles
    assert simulator.cycles == 10
    by_slot = {}
    for frame in frames:
        by_slot.setdefault((frame.slot, frame.channel), []).append(frame.cycle)
    assert by_slot[(12, 'A')] == list(range(10)) and by_slot[(12, 'B')] == list(range(10))
    assert by_slot[(40, 'A')] == [1, 5, 9] and by_slot[(50, 'A')] == [3]
    assert all(db.definition(frame) is not None for frame in frames)
    assert [f.timestamp for f in frames] == sorted(f.timestamp for f in frames)
    assert simulator.frames_until(0.05) == []  # Nothing new


# --- The Vector spy, against a simulated XL library ---


# Sizes and offsets printed by Vector's vxlapi.h (XL_FR_SPY_FRAME_EV and s_xl_fr_event) compiled with pack(8)
@pytest.mark.parametrize(
    'structure, field, expected',
    [
        (flexray_vector.XL_FR_SPY_FRAME_EV, None, 276),
        (flexray_vector.XL_FR_SPY_FRAME_EV, 'payloadLength', 12),
        (flexray_vector.XL_FR_SPY_FRAME_EV, 'cycleCount', 13),
        (flexray_vector.XL_FR_SPY_FRAME_EV, 'frameCRC', 16),
        (flexray_vector.XL_FR_SPY_FRAME_EV, 'data', 20),
        (flexray_vector.XLfrEvent, None, 512),
        (flexray_vector.XLfrEvent, 'flagsChip', 12),
        (flexray_vector.XLfrEvent, 'timeStamp', 16),
        (flexray_vector.XLfrEvent, 'tagData', 32),
    ],
)
def test_vector_structures_match_vxlapi(structure, field, expected):
    assert (ctypes.sizeof(structure) if field is None else getattr(structure, field).offset) == expected


class FakeXl:
    def __init__(self):
        self.calls = []
        self.events = []

    def xlOpenDriver(self):
        return 0

    def xlCloseDriver(self):
        self.calls.append('close driver')
        return 0

    def xlGetDriverConfig(self, config):
        config = config._obj
        config.channelCount = 2
        config.channel[0].name = b'VN1630 Channel 1'
        config.channel[0].channelBusCapabilities = 0x1  # CAN only
        config.channel[1].name = b'VN7610 FlexRay'
        config.channel[1].channelBusCapabilities = 0x4
        config.channel[1].channelMask = 0x10
        return 0

    def xlOpenPort(self, port, name, access, permission, queue, version, bus):
        self.calls.append(('open', access.value, version, bus))
        port._obj.value = 3
        return 0

    def xlFrActivateSpy(self, port, access, mode):
        self.calls.append(('spy', mode))
        return 0

    def xlActivateChannel(self, port, access, bus, flags):
        self.calls.append(('activate', bus, flags))
        return 0

    def xlFrReceive(self, port, event):
        if not self.events:
            return flexray_vector.XL_ERR_QUEUE_IS_EMPTY
        source = self.events.pop(0)  # Kept referenced while it is copied
        ctypes.memmove(ctypes.addressof(event._obj), ctypes.addressof(source), ctypes.sizeof(event._obj))
        return 0

    def xlDeactivateChannel(self, port, access):
        self.calls.append('deactivate')
        return 0

    def xlClosePort(self, port):
        self.calls.append('close port')
        return 0


def spy_event(slot, cycle, data, channel=flexray_vector.XL_FR_CHANNEL_A, flags=0, error=0, nanoseconds=1500):
    event = flexray_vector.XLfrEvent(tag=flexray_vector.XL_FR_SPY_FRAME, flagsChip=channel, timeStamp=nanoseconds)
    spy = event.tagData.frSpyFrame
    spy.slotID, spy.cycleCount, spy.payloadLength, spy.headerFlags, spy.frameError = (
        slot,
        cycle,
        len(data) // 2,
        flags,
        error,
    )
    ctypes.memmove(spy.data, bytes(data), len(data))
    return event


def test_vector_spy_flow():
    library = FakeXl()
    spy = flexray_vector.VectorFlexRaySpy(library)
    assert spy.channels() == [{'name': 'VN7610 FlexRay', 'mask': 0x10, 'serial': 0}]
    spy.start(0x10)
    assert library.calls[:3] == [
        ('open', 0x10, flexray_vector.XL_INTERFACE_VERSION_V4, flexray_vector.XL_BUS_TYPE_FLEXRAY),
        ('spy', flexray_vector.XL_FR_SPY_MODE_ASYNCHRONOUS),
        ('activate', flexray_vector.XL_BUS_TYPE_FLEXRAY, flexray_vector.XL_ACTIVATE_RESET_CLOCK),
    ]
    library.events = [
        spy_event(12, 4, bytes(range(8)), flags=flexray_vector.FLAG_SYNC),
        spy_event(
            13, 4, bytes(2), channel=flexray_vector.XL_FR_CHANNEL_B, flags=flexray_vector.FLAG_NULLFRAME, error=0x02
        ),
    ]
    first, second = spy.receive()
    assert (first.channel, first.slot, first.cycle, first.payload, first.sync) == ('A', 12, 4, bytes(range(8)), True)
    assert (second.channel, second.null_frame, second.errors) == ('B', True, ['header CRC'])
    assert spy.receive() == []
    spy.close()
    assert library.calls[-3:] == ['deactivate', 'close port', 'close driver']
