import asyncio
import uuid

import can
import cantools
import pytest

from can_explorer.core.restbus import RestbusSimulator
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource

DBC = """
VERSION ""
BU_: ECU1 TESTER
BO_ 256 EngineData: 8 ECU1
 SG_ Rpm : 0|16@1+ (1,0) [0|8000] "rpm" TESTER
BO_ 512 TesterMsg: 8 TESTER
 SG_ Req : 0|8@1+ (1,0) [0|255] "" ECU1
BA_DEF_ BO_ "GenMsgCycleTime" INT 0 10000;
BA_DEF_DEF_ "GenMsgCycleTime" 0;
BA_ "GenMsgCycleTime" BO_ 256 10;
"""


@pytest.fixture
def database():
    return cantools.database.load_string(DBC, 'dbc')


@pytest.fixture
def channel():
    return f'restbus-{uuid.uuid4().hex}'


def _session(channel):
    return BusSession(LiveSource(BusConfig(interface='virtual', channel=channel)))


async def wait_until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out'
        await asyncio.sleep(0.01)


def test_messages_and_defaults(database, channel):
    sim = RestbusSimulator(_session(channel), database.messages)
    by_name = {entry.message.name: entry for entry in sim.messages()}
    assert set(by_name) == {'EngineData', 'TesterMsg'}
    # Only the message with a cycle time is enabled, at its period; the event message is listed but off.
    assert by_name['EngineData'].enabled and by_name['EngineData'].period == pytest.approx(0.01)
    assert not by_name['TesterMsg'].enabled and not by_name['TesterMsg'].has_cycle_time
    assert sim.enabled_count == 1
    assert len(by_name['EngineData'].data) == 8


def test_exclude_sender_disables_the_node(database, channel):
    sim = RestbusSimulator(_session(channel), database.messages)
    assert sim.exclude_sender('ECU1') == 1  # EngineData is sent by ECU1
    assert sim.enabled_count == 0
    assert sim.exclude_sender('ECU1') == 0  # Already off


def test_set_value_re_encodes(database, channel):
    sim = RestbusSimulator(_session(channel), database.messages)
    key = (256, False)
    before = sim.get(key).data
    sim.set_value(key, 'Rpm', 2000)
    after = sim.get(key).data
    assert after != before
    assert database.get_message_by_name('EngineData').decode(after)['Rpm'] == 2000


async def test_transmits_enabled_messages(database, channel):
    peer = can.Bus(interface='virtual', channel=channel)
    session = _session(channel)
    session.start()
    sim = RestbusSimulator(session, database.messages)
    received = []
    try:
        sim.start()
        assert sim.running
        await wait_until(lambda: (_drain(peer, received), any(m.arbitration_id == 256 for m in received))[1])
        # EngineData (256) is transmitted; TesterMsg (512) is not (no cycle time).
        assert all(m.arbitration_id == 256 for m in received)
        sim.stop()
        assert not sim.running
        received.clear()
        await asyncio.sleep(0.05)
        _drain(peer, received)
        assert received == []  # Nothing after stop
    finally:
        sim.stop()
        session.stop()
        peer.shutdown()


async def test_disabled_message_can_be_enabled_while_running(database, channel):
    peer = can.Bus(interface='virtual', channel=channel)
    session = _session(channel)
    session.start()
    sim = RestbusSimulator(session, database.messages)
    sim.set_enabled((256, False), False)  # Turn the only cyclic message off before starting
    try:
        sim.start()
        sim.set_enabled((512, False), True)  # Enable the event message at the default period while running
        await wait_until(lambda: _received_id(peer, 512))
    finally:
        sim.stop()
        session.stop()
        peer.shutdown()


def _drain(bus, into):
    while True:
        message = bus.recv(timeout=0)
        if message is None:
            return
        into.append(message)


def _received_id(bus, arbitration_id):
    while True:
        message = bus.recv(timeout=0)
        if message is None:
            return False
        if message.arbitration_id == arbitration_id:
            return True


# Send modes, per-frame values, timing and configuration

RICH_DBC = """
VERSION ""
BU_: ECU1 TESTER
BO_ 256 Engine: 8 ECU1
 SG_ Rpm : 0|16@1+ (1,0) [0|8000] "rpm" TESTER
 SG_ AliveCounter : 16|4@1+ (1,0) [0|14] "" TESTER
 SG_ Checksum : 31|8@0+ (1,0) [0|255] "" TESTER
BO_ 257 Door: 2 ECU1
 SG_ DoorOpen : 0|1@1+ (1,0) [0|1] "" TESTER
BO_ 258 Warning: 2 ECU1
 SG_ Lamp : 0|2@1+ (1,0) [0|3] "" TESTER
BO_ 259 Unused: 1 ECU1
 SG_ X : 0|8@1+ (1,0) [0|255] "" TESTER
BO_ 260 Multiplexed: 8 ECU1
 SG_ Page M : 0|8@1+ (1,0) [0|3] "" TESTER
 SG_ A m0 : 8|8@1+ (1,0) [0|255] "" TESTER
 SG_ B m1 : 8|8@1+ (1,0) [0|255] "" TESTER
 SG_ Gear : 16|2@1+ (1,0) [0|3] "" TESTER
BO_ 2147484160 FdData: 16 ECU1
 SG_ Big : 0|8@1+ (1,0) [0|255] "" TESTER
BA_DEF_ BO_ "GenMsgCycleTime" INT 0 10000;
BA_DEF_ BO_ "GenMsgSendType" ENUM "Cyclic","Spontaneous","CyclicIfActive","NoMsgSendType";
BA_DEF_ BO_ "GenMsgCycleTimeFast" INT 0 10000;
BA_DEF_ BO_ "GenMsgNrOfRepetition" INT 0 100;
BA_DEF_ BO_ "GenMsgDelayTime" INT 0 10000;
BA_DEF_ BO_ "GenMsgStartDelayTime" INT 0 10000;
BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN","ExtendedCAN","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","StandardCAN_FD","ExtendedCAN_FD";
BA_DEF_ BO_ "CANFD_BRS" ENUM "0","1";
BA_DEF_ SG_ "GenSigInactiveValue" INT 0 100000;
BA_DEF_DEF_ "GenMsgCycleTime" 0;
BA_DEF_DEF_ "GenMsgSendType" "Cyclic";
BA_DEF_DEF_ "GenMsgCycleTimeFast" 0;
BA_DEF_DEF_ "GenMsgNrOfRepetition" 0;
BA_DEF_DEF_ "GenMsgDelayTime" 0;
BA_DEF_DEF_ "GenMsgStartDelayTime" 0;
BA_DEF_DEF_ "VFrameFormat" "StandardCAN";
BA_DEF_DEF_ "CANFD_BRS" "1";
BA_DEF_DEF_ "GenSigInactiveValue" 0;
BA_ "GenMsgCycleTime" BO_ 256 10;
BA_ "GenMsgStartDelayTime" BO_ 256 5;
BA_ "GenMsgSendType" BO_ 257 1;
BA_ "GenMsgNrOfRepetition" BO_ 257 2;
BA_ "GenMsgCycleTimeFast" BO_ 257 5;
BA_ "GenMsgSendType" BO_ 258 2;
BA_ "GenMsgCycleTime" BO_ 258 100;
BA_ "GenMsgCycleTimeFast" BO_ 258 20;
BA_ "GenMsgSendType" BO_ 259 3;
BA_ "GenMsgCycleTime" BO_ 259 100;
BA_ "GenMsgCycleTime" BO_ 260 10;
BA_ "GenMsgCycleTime" BO_ 2147484160 50;
BA_ "VFrameFormat" BO_ 2147484160 15;
VAL_ 260 Gear 0 "P" 1 "R" 2 "N" 3 "D";
"""


@pytest.fixture
def rich():
    return cantools.database.load_string(RICH_DBC, 'dbc')


def _defaults(database):
    return {name: definition.default_value for name, definition in database.dbc.attribute_definitions.items()}


def _sim(database, session, **kwargs):
    """A simulator of the database on `session` (a BusSession, or a channel name for a session not started)."""
    if isinstance(session, str):
        session = _session(session)
    return RestbusSimulator(session, database.messages, attribute_defaults=_defaults(database), **kwargs)


def test_send_modes_and_timing_come_from_the_database(rich, channel):
    from can_explorer.core.restbus import CYCLIC, IF_ACTIVE, ON_CHANGE

    sim = _sim(rich, channel)
    engine, door, warning, unused = (sim.by_name(name) for name in ('Engine', 'Door', 'Warning', 'Unused'))
    assert (engine.mode, engine.enabled, engine.start_delay) == (CYCLIC, True, pytest.approx(0.005))
    # Spontaneous: sent on change with 2 repetitions at the fast cycle; off by default (no cycle time)
    assert (door.mode, door.enabled, door.repetitions, door.repetition_gap) == (ON_CHANGE, False, 2, 0.005)
    # CyclicIfActive: at the fast cycle while active
    assert (warning.mode, warning.enabled, warning.active_period) == (IF_ACTIVE, True, pytest.approx(0.02))
    # NoMsgSendType: listed, but off even though it has a cycle time
    assert not unused.sent_by_nodes and not unused.enabled and unused.mode == CYCLIC


def test_can_fd_frames_use_the_database_format_and_bit_rate_switch(rich, channel):
    sim = _sim(rich, channel)
    fd = sim.by_name('FdData').static_frame()
    assert fd.is_fd and fd.bitrate_switch and fd.is_extended_id  # CANFD_BRS defaults to "1"
    classic = sim.by_name('Engine').static_frame()
    assert not classic.is_fd and not classic.bitrate_switch
    # Without a CANFD_BRS attribute, the simulator's default decides
    plain = cantools.database.load_string(DBC.replace('BO_ 512 TesterMsg: 8', 'BO_ 512 TesterMsg: 12'), 'dbc')
    assert RestbusSimulator(_session(channel), plain.messages, bitrate_switch=True).get((512, False)).bitrate_switch
    assert not RestbusSimulator(_session(channel), plain.messages).get((512, False)).bitrate_switch


def test_counters_count_and_wrap_at_their_maximum(rich, channel):
    sim = _sim(rich, channel)
    engine = sim.by_name('Engine')
    assert engine.roles['AliveCounter'].role == 'counter'  # Taken for a counter by its name
    message = rich.get_message_by_name('Engine')
    counters = [message.decode(engine.next_frame().data)['AliveCounter'] for _ in range(17)]
    assert counters == list(range(15)) + [0, 1]  # 0..14, the database's range (15 is reserved)


def test_checksum_algorithms_match_their_check_values():
    from can_explorer.core.restbus import checksum

    data = b'123456789'
    assert checksum('crc8_sae_j1850', data) == 0x4B
    assert checksum('crc8h2f', data) == 0xDF
    assert checksum('crc8_smbus', data) == 0xF4
    assert checksum('crc8_maxim', data) == 0xA1
    assert checksum('crc8_cdma2000', data) == 0xDA
    assert checksum('xor', b'\x01\x02\x04') == 0x07 and checksum('xor', b'\x01\x02\x04', 0xF0) == 0xF7
    assert checksum('sum', b'\xff\x02') == 0x01 and checksum('sum', b'\xff\x02', 0x10) == 0x11
    assert checksum('inverted_sum', b'\x01\x02') == 0xFC


def test_checksum_signals_are_computed_over_the_other_bytes(rich, channel):
    from can_explorer.core.restbus import SignalRole, checksum

    sim = _sim(rich, channel)
    key = (256, False)
    assert sim.set_role(key, 'Checksum', SignalRole('checksum', 'xor', 0x5A)) == ''
    sim.set_value(key, 'Rpm', 0x1234)
    for _ in range(3):  # The counter changes, so the checksum does too
        data = sim.get(key).next_frame().data
        assert data[3] == checksum('xor', bytes(data[:3] + data[4:]), 0x5A)
    # Only whole bytes can be checksums
    assert 'whole byte' in sim.set_role(key, 'Rpm', SignalRole('checksum'))
    assert 'unknown checksum' in sim.set_role(key, 'Checksum', SignalRole('checksum', 'md5'))


def test_multiplexed_messages_rotate_through_the_selected_pages(rich, channel):
    sim = _sim(rich, channel)
    key = (260, False)
    entry = sim.get(key)
    assert entry.pages == [0, 1] and entry.dynamic
    message = rich.get_message_by_name('Multiplexed')
    assert [message.decode(entry.next_frame().data)['Page'] for _ in range(4)] == [0, 1, 0, 1]
    sim.set_pages(key, [1])
    assert {message.decode(entry.next_frame().data)['Page'] for _ in range(3)} == {1}
    assert not entry.dynamic  # One page and no counters: the frame no longer changes
    # Choice names are encoded by their value table
    sim.set_value(key, 'Gear', 'D')
    assert str(message.decode(entry.next_frame().data)['Gear']) == 'D'


def test_e2e_protected_pdus_get_a_counter_and_crc(e2e_arxml, channel):
    from can_explorer.core.decoding import DbcDecoder

    decoder = DbcDecoder(e2e_arxml, bus='Powertrain')
    message = next(m for m in decoder.messages() if m.name == 'EngineData')
    sim = RestbusSimulator(_session(channel), [message])
    entry = sim.get((message.frame_id, message.is_extended_frame))
    assert entry.e2e is not None and entry.dynamic and entry.note == ''
    checks = [entry.e2e.check(entry.next_frame().data) for _ in range(3)]
    assert all(check.ok for check in checks) and [check.counter for check in checks] == [0, 1, 2]


def test_if_active_and_bus_load(rich, channel):
    sim = _sim(rich, channel)
    warning = sim.by_name('Warning')
    assert not warning.active  # Lamp is at its inactive value 0
    sim.set_value(warning.key, 'Lamp', 2)
    assert warning.active
    # Engine (10 ms), Warning (if active, 20 ms), Multiplexed (10 ms) and FdData (50 ms) at 500 kbit/s
    load = sim.expected_load(500_000, 2_000_000)
    assert 0.03 < load < 0.06
    assert sim.expected_load(None) is None


def test_configuration_round_trip(rich, channel):
    from can_explorer.core.restbus import ON_CHANGE, SignalRole

    sim = _sim(rich, channel)
    sim.set_enabled((256, False), False)
    sim.set_mode((260, False), ON_CHANGE)
    sim.set_value((260, False), 'Gear', 'R')
    sim.set_role((256, False), 'Checksum', SignalRole('checksum', 'crc8h2f', 7))
    sim.set_role((256, False), 'AliveCounter', SignalRole('value'))
    sim.set_pages((260, False), [0])
    sim.set_hardware(True)
    config = sim.to_dict()
    assert set(config['messages']) == {'Engine', 'Multiplexed'}  # Only what differs from the database

    other = _sim(rich, channel)
    assert other.apply({**config, 'messages': {**config['messages'], 'Gone': {'enabled': True}}}) == [
        'restbus: the database has no message Gone'
    ]
    assert other.to_dict() == config
    engine = other.get((256, False))
    assert not engine.enabled and engine.roles['Checksum'] == SignalRole('checksum', 'crc8h2f', 7)
    assert 'AliveCounter' not in engine.roles and other.hardware


async def test_staggered_start_and_start_delay(rich, channel):
    session = _session(channel)
    session.start()
    sim = _sim(rich, session)
    try:
        sim.start()
        delays = {entry.name: entry._transmit.delay for entry in sim.messages() if entry._transmit is not None}
        # Engine: its 5 ms start delay; the others 1 ms apart (within their period)
        assert delays['Engine'] == pytest.approx(0.005)
        assert len(set(delays.values())) == len(delays)
    finally:
        sim.stop()
        session.stop()


async def test_on_change_messages_are_sent_with_their_repetitions(rich, channel):
    peer = can.Bus(interface='virtual', channel=channel)
    session = _session(channel)
    session.start()
    sim = _sim(rich, session)
    door = sim.by_name('Door')
    sim.set_all(False)
    sim.set_enabled(door.key, True)
    received = []
    try:
        sim.start()
        await asyncio.sleep(0.05)
        _drain(peer, received)
        assert received == []  # Nothing until a value changes
        sim.set_value(door.key, 'DoorOpen', 1)
        await wait_until(lambda: (_drain(peer, received), len(received) >= 3)[1])
        await asyncio.sleep(0.03)
        _drain(peer, received)
        assert [m.arbitration_id for m in received] == [257] * 3  # Once and 2 repetitions
        assert all(m.data[0] & 1 for m in received)
    finally:
        sim.stop()
        session.stop()
        peer.shutdown()


async def test_if_active_messages_are_sent_only_while_active(rich, channel):
    peer = can.Bus(interface='virtual', channel=channel)
    session = _session(channel)
    session.start()
    sim = _sim(rich, session)
    warning = sim.by_name('Warning')
    sim.set_all(False)
    sim.set_enabled(warning.key, True)
    received = []
    try:
        sim.start()
        assert sim.state_of(warning) == 'inactive'
        await asyncio.sleep(0.05)
        _drain(peer, received)
        assert received == []
        sim.set_value(warning.key, 'Lamp', 1)
        assert sim.state_of(warning) == 'sending'
        await wait_until(lambda: _received_id(peer, 258))
        sim.set_value(warning.key, 'Lamp', 0)
        assert sim.state_of(warning) == 'inactive' and not warning.running
    finally:
        sim.stop()
        session.stop()
        peer.shutdown()


async def test_a_transmission_that_fails_shows_as_stopped(rich, channel):
    session = _session(channel)
    session.start()
    sim = _sim(rich, session)
    engine = sim.by_name('Engine')
    sim.set_all(False)
    sim.set_enabled(engine.key, True)

    def broken():
        raise RuntimeError('frame cannot be built')

    engine.next_frame = broken
    try:
        sim.start()
        await wait_until(lambda: sim.state_of(engine) == 'stopped')
        assert sim.problems == {engine.key: 'stopped: frame cannot be built'}
        assert not engine.running
    finally:
        sim.stop()
        session.stop()


async def test_hardware_timing_for_frames_that_never_change(rich, channel):
    session = _session(channel)
    session.start()
    sim = _sim(rich, session)
    sim.set_hardware(True)
    try:
        sim.start()
        fd = sim.by_name('FdData')  # No counter, no checksum, no pages: the interface can repeat it
        await wait_until(lambda: fd.hardware_timed)
        engine = sim.by_name('Engine')  # Its counter changes every frame: timed in software
        assert engine.running and not engine.hardware_timed
    finally:
        sim.stop()
        session.stop()
