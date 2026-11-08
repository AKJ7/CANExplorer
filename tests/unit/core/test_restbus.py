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
