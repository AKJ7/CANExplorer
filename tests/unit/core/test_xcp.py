import asyncio
import uuid

import pytest

from can_explorer.core.can_command import CanCommandClient, CanCommandTimeout
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.xcp import (
    RESOURCE_CALPAG,
    DaqSignal,
    XcpClient,
    XcpError,
    pack_odts,
)
from can_explorer.core.xcp_protocol import (
    CHECKSUM_ADD_11,
    CHECKSUM_ADD_44,
    CHECKSUM_CRC_16,
    CHECKSUM_CRC_16_CITT,
    CHECKSUM_CRC_32,
    ERROR_CODES,
    ID_FIELD_RELATIVE_BYTE,
    ID_FIELD_RELATIVE_WORD,
    ID_FIELD_RELATIVE_WORD_ALIGNED,
    checksum,
)
from can_explorer.core.xcp_slave import REFERENCE_PAGE, SimulatedEvent, XcpSlaveSimulator, demo_key
from tests.unit.waiting import wait_until


def _sessions(channel, fd=False):
    master = BusSession(LiveSource(BusConfig('virtual', channel, fd=fd)))
    slave = BusSession(LiveSource(BusConfig('virtual', channel, fd=fd)))
    master.start()
    slave.start()
    return master, slave


@pytest.fixture
def make_xcp():
    created = []

    def make(fd=False, compute_key=None, **options):
        channel = f'xcp-{uuid.uuid4().hex}'
        master, slave = _sessions(channel, fd)
        simulator = XcpSlaveSimulator(slave, 0x7E0, 0x7E1, fd=fd, **options)
        transport = CanCommandClient(master, 0x7E0, 0x7E1, fd=fd, timeout=0.2)
        client = XcpClient(transport, compute_key=compute_key)
        created.append((master, slave, simulator, transport))
        return client, simulator

    yield make
    for master, slave, simulator, transport in created:
        transport.close()
        simulator.stop()
        master.stop()
        slave.stop()


def test_error_codes_follow_the_standard():
    assert ERROR_CODES[0x23] == 'write protected'
    assert ERROR_CODES[0x24] == 'access denied'
    assert ERROR_CODES[0x25].startswith('access locked')
    assert ERROR_CODES[0x29] == 'sequence error'


def test_checksum_check_values():
    check = b'123456789'
    assert checksum(CHECKSUM_CRC_16, check) == 0xBB3D
    assert checksum(CHECKSUM_CRC_16_CITT, check) == 0x29B1
    assert checksum(CHECKSUM_CRC_32, check) == 0xCBF43926
    assert checksum(CHECKSUM_ADD_11, bytes([0xFF, 0x02])) == 0x01
    assert checksum(CHECKSUM_ADD_44, bytes([1, 0, 0, 0, 2, 0, 0, 0])) == 3
    assert checksum(CHECKSUM_ADD_44, bytes([0, 0, 0, 1]), 'big') == 1


async def test_connect_reads_the_slave_parameters(make_xcp):
    client, simulator = make_xcp(address_granularity=2, byte_order='big')
    await client.connect()
    assert client.byte_order == 'big' and client.address_granularity == 2
    assert client.max_cto == 8 and client.protection == 0
    assert await client.get_id() == 'example'  # Longer than one response: uploaded from the MTA


@pytest.mark.parametrize('granularity', [1, 2, 4])
async def test_read_write_with_address_granularity(make_xcp, granularity):
    client, simulator = make_xcp(address_granularity=granularity)
    await client.connect()
    address = 0x100  # In elements: byte offset 0x100 * granularity
    simulator.memory[0x100 * granularity : 0x100 * granularity + 16] = bytes(range(16))
    assert await client.read(address, 0, 16) == bytes(range(16))
    assert await client.read(address, 0, 3) == bytes([0, 1, 2])
    await client.write(0x200, 0, bytes([9] * 8))
    assert simulator.memory[0x200 * granularity : 0x200 * granularity + 8] == bytes([9] * 8)
    await client.write(0x300, 0, bytes([7]))  # A partial element is read, modified and written
    assert simulator.memory[0x300 * granularity] == 7


async def test_block_modes(make_xcp):
    client, simulator = make_xcp(slave_block_mode=True, master_block_mode=True)
    await client.connect()
    assert client.slave_block_mode and client.master_block_mode and client.max_bs == 8
    simulator.memory[0x400:0x440] = bytes(range(64))
    count = len(simulator.commands)
    assert await client.read(0x400, 0, 64) == bytes(range(64))
    uploads = [c for c in simulator.commands[count:] if c[0] == 0xF5]
    assert len(uploads) == 1  # One UPLOAD, answered by a slave block
    await client.write(0x500, 0, bytes(range(40)))
    assert simulator.memory[0x500:0x528] == bytes(range(40))
    assert any(c[0] == 0xEF for c in simulator.commands)  # DOWNLOAD_NEXT


async def test_can_fd(make_xcp):
    client, simulator = make_xcp(fd=True)
    await client.connect()
    assert client.max_cto == 64
    simulator.memory[0:60] = bytes(range(60))
    count = len(simulator.commands)
    assert await client.read(0, 0, 60) == bytes(range(60))
    assert len(simulator.commands) - count == 1  # One SHORT_UPLOAD of 60 bytes


async def test_busy_is_repeated_and_timeouts_resynchronise(make_xcp):
    client, simulator = make_xcp()
    await client.connect()
    simulator.memory[0x10:0x18] = bytes(range(8))
    simulator.busy_count = 2
    assert await client.read(0x10, 0, 2) == bytes([0, 1])
    await client.set_mta(0x10)
    simulator.drop_count = 1  # The response to the next UPLOAD is lost
    assert await client.upload(4) == bytes(range(4))  # SYNCH, SET_MTA and UPLOAD again: no shifted data
    assert any(c[0] == 0xFC for c in simulator.commands)
    simulator.drop_count = 10
    with pytest.raises(CanCommandTimeout):
        await client.get_status()


async def test_seed_and_key(make_xcp):
    client, simulator = make_xcp(protected=RESOURCE_CALPAG, seed=bytes(range(1, 11)))  # A seed over two packets
    await client.connect()
    assert client.protection == RESOURCE_CALPAG
    with pytest.raises(XcpError, match='access locked'):
        await client.write(0x100, 0, b'\x01')
    client.compute_key = demo_key
    await client.write(0x100, 0, b'\x01')  # Unlocked automatically, then repeated
    assert simulator.memory[0x100] == 1 and client.protection == 0


async def test_wrong_key(make_xcp):
    client, simulator = make_xcp(protected=RESOURCE_CALPAG)
    await client.connect()
    with pytest.raises(XcpError, match='access locked'):
        await client.unlock(RESOURCE_CALPAG, lambda resource, seed: b'\x00\x00\x00\x00')
    assert not simulator.connected


async def test_checksum_and_pages(make_xcp):
    client, simulator = make_xcp()
    await client.connect()
    simulator.memory[0x2000:0x2010] = bytes(range(16))
    result = await client.verify_checksum(0x2000, bytes(range(16)))
    assert result['type'] == 'CRC_32' and result['match'] is True
    assert (await client.verify_checksum(0x2000, bytes(16)))['match'] is False

    assert await client.get_cal_page() == 0
    await client.write(0x2000, 0, b'\xaa')
    await client.set_cal_page(REFERENCE_PAGE)
    assert await client.read(0x2000, 0, 1) == b'\x00'  # The reference page still has the original value
    with pytest.raises(XcpError, match='write protected'):
        await client.write(0x2000, 0, b'\xbb')
    await client.set_cal_page(0)
    await client.copy_cal_page(REFERENCE_PAGE, 0)  # Back to the reference values
    assert await client.read(0x2000, 0, 1) == b'\x00'
    assert (await client.get_pag_processor_info())['segments'] == 1


def test_pack_odts_splits_ranges():
    odts = pack_odts([('a', 0x100, 0, 4), ('b', 0x200, 0, 8)], 3, 7, 0xFF)
    assert [[(e.key, e.offset, e.size) for e in odt] for odt in odts] == [
        [('a', 0, 3)],
        [('a', 3, 1), ('b', 0, 6)],
        [('b', 6, 2)],
    ]
    assert odts[1][1].address == 0x200 and odts[2][0].address == 0x206


@pytest.mark.parametrize(
    'identification',
    [0, ID_FIELD_RELATIVE_BYTE, ID_FIELD_RELATIVE_WORD, ID_FIELD_RELATIVE_WORD_ALIGNED],
)
async def test_dynamic_daq(make_xcp, identification):
    client, simulator = make_xcp(identification_field=identification, auto_events=False)
    await client.connect()
    events = await client.daq_events()
    assert [(e['name'], e['cycle']) for e in events] == [('10ms', 0.01), ('100ms', 0.1)]
    simulator.memory[0x1000:0x1002] = (6000).to_bytes(2, 'little')
    simulator.memory[0x1010:0x1018] = bytes(range(8))
    simulator.memory[0x1020] = 42
    samples = []
    measurement = await client.start_measurement(
        [
            DaqSignal('speed', 0x1000, 2, event=0),
            DaqSignal('blob', 0x1010, 8, event=0),
            DaqSignal('slow', 0x1020, 1, event=1),
        ],
        samples.append,
    )
    simulator.trigger_event(0)
    simulator.trigger_event(1)
    simulator.memory[0x1000:0x1002] = (6004).to_bytes(2, 'little')
    simulator.trigger_event(0)
    assert await wait_until(lambda: len(samples) == 3)
    fast = [s for s in samples if s.event == 0]
    assert fast[0].values == {'speed': (6000).to_bytes(2, 'little'), 'blob': bytes(range(8))}
    assert fast[1].values['speed'] == (6004).to_bytes(2, 'little')
    assert fast[0].ecu_time is not None and fast[1].ecu_time >= fast[0].ecu_time
    assert next(s for s in samples if s.event == 1).values == {'slow': bytes([42])}
    await measurement.stop()
    simulator.trigger_event(0)
    await asyncio.sleep(0.05)
    assert len(samples) == 3


async def test_daq_runs_on_cyclic_events(make_xcp):
    client, simulator = make_xcp(events=[SimulatedEvent(0, 'fast', 0.005)])
    await client.connect()
    samples = []
    measurement = await client.start_measurement([DaqSignal('x', 0x10, 4)], samples.append)
    assert await wait_until(lambda: len(samples) >= 3)
    await measurement.stop()


async def test_daq_needs_unlock(make_xcp):
    from can_explorer.core.xcp import RESOURCE_DAQ

    client, simulator = make_xcp(protected=RESOURCE_DAQ, compute_key=demo_key, auto_events=False)
    await client.connect()
    samples = []
    await client.start_measurement([DaqSignal('x', 0x10, 1)], samples.append)
    simulator.trigger_event(0)
    assert await wait_until(lambda: samples)


@pytest.mark.parametrize('protocol', ['udp', 'tcp'])
async def test_xcp_on_ethernet(protocol):
    from can_explorer.core.xcp_ethernet import XcpEthernetSlave, XcpEthernetTransport, frame, unframe

    assert list(unframe(frame(1, b'\xff') + frame(2, b'\xfe\x20'))) == [b'\xff', b'\xfe\x20']
    server = XcpEthernetSlave(protocol=protocol, auto_events=False)
    port = await server.start()
    transport = XcpEthernetTransport('127.0.0.1', port, protocol, timeout=0.5)
    await transport.open()
    try:
        client = XcpClient(transport)
        await client.connect()
        assert client.max_cto == 255
        server.memory[0x100:0x200] = bytes(range(256))
        assert await client.read(0x100, 0, 256) == bytes(range(256))
        await client.write(0x300, 0, bytes(range(200)))
        assert server.memory[0x300 : 0x300 + 200] == bytes(range(200))
        samples = []
        await client.start_measurement([DaqSignal('a', 0x100, 100), DaqSignal('b', 0x180, 4)], samples.append)
        server.slave.trigger_event(0)
        assert await wait_until(lambda: samples)
        assert samples[0].values == {'a': bytes(range(100)), 'b': bytes(range(0x80, 0x84))}
        await client.disconnect()
    finally:
        transport.close()
        await server.stop()


# Static DAQ


def _list_numbers(commands, code):
    return [int.from_bytes(c[2:4], 'little') for c in commands if c[0] == code]


async def test_static_daq_uses_the_slaves_lists(make_xcp):
    from can_explorer.core.xcp_protocol import ALLOC_DAQ, CLEAR_DAQ_LIST, FREE_DAQ, READ_DAQ, SET_DAQ_PTR
    from can_explorer.core.xcp_slave import DEFAULT_STATIC_LISTS

    client, simulator = make_xcp(static_daq=DEFAULT_STATIC_LISTS, auto_events=False)
    await client.connect()
    simulator.memory[0x1000:0x1002] = (6000).to_bytes(2, 'little')
    simulator.memory[0x1010:0x1018] = bytes(range(8))
    simulator.memory[0x1020] = 42
    samples = []
    measurement = await client.start_measurement(
        [
            DaqSignal('speed', 0x1000, 2, event=0),
            DaqSignal('blob', 0x1010, 8, event=0),
            DaqSignal('slow', 0x1020, 1, event=1),
        ],
        samples.append,
    )
    assert measurement.static
    commands = simulator.commands
    assert not any(c[0] in (FREE_DAQ, ALLOC_DAQ) for c in commands)
    # List 0 is predefined: it is read (READ_DAQ), never cleared or written, and carries 'speed' on event 0
    assert _list_numbers(commands, CLEAR_DAQ_LIST) == [1, 2]
    after_list_0 = [commands[i + 1][0] for i, c in enumerate(commands[:-1]) if c[0] == SET_DAQ_PTR and c[2] == 0]
    assert after_list_0 and set(after_list_0) == {READ_DAQ}
    predefined = next(d for d in measurement.lists if d.predefined)
    assert predefined.number == 0 and set(predefined.views) == {'speed'}
    simulator.trigger_event(0)
    simulator.trigger_event(1)
    assert await wait_until(lambda: len(samples) == 3)  # Event 0 has two lists: predefined 0 and configured 1
    event_0 = {k: v for s in samples if s.event == 0 for k, v in s.values.items()}
    assert event_0 == {'speed': (6000).to_bytes(2, 'little'), 'blob': bytes(range(8))}
    assert next(s for s in samples if s.event == 1).values == {'slow': bytes([42])}
    await measurement.stop()


async def test_static_daq_respects_fixed_events_and_sizes(make_xcp):
    from can_explorer.core.xcp_protocol import CLEAR_DAQ_LIST
    from can_explorer.core.xcp_slave import StaticDaqList

    lists = [StaticDaqList(2, 4, fixed_event=1), StaticDaqList(1, 1), StaticDaqList(3, 2)]
    client, simulator = make_xcp(static_daq=lists, auto_events=False)
    await client.connect()
    samples = []
    # Event 0 cannot use list 0 (fixed to event 1), and list 1 (one ODT, one entry) is too small for two signals
    measurement = await client.start_measurement(
        [DaqSignal('a', 0x10, 2, event=0), DaqSignal('b', 0x20, 2, event=0)], samples.append
    )
    assert _list_numbers(simulator.commands, CLEAR_DAQ_LIST) == [2]
    simulator.trigger_event(0)
    assert await wait_until(lambda: samples)
    await measurement.stop()


async def test_static_daq_without_a_free_list(make_xcp):
    from can_explorer.core.xcp_slave import DEFAULT_STATIC_LISTS

    client, _simulator = make_xcp(static_daq=DEFAULT_STATIC_LISTS, auto_events=False)
    await client.connect()
    signals = [DaqSignal(f's{event}', 0x10 * event, 1, event=event) for event in range(4)]
    with pytest.raises(XcpError, match='No free static DAQ list can take DAQ on event 3'):
        await client.start_measurement(signals, lambda sample: None)


# STIM


def _stim_received(measurement, slave_list) -> bool:
    """Whether the slave holds, for every ODT, the data the master last sent (CAN pads the DTOs)."""
    daq = next(d for d in measurement.lists if d.stim)
    for odt, entries in enumerate(daq.odts):
        expected = b''.join(bytes(daq.values[e.key][e.offset : e.offset + e.size]) for e in entries)
        if slave_list.stim_data.get(odt, b'')[: len(expected)] != expected:
            return False
    return True


@pytest.mark.parametrize('identification', [0, ID_FIELD_RELATIVE_BYTE, ID_FIELD_RELATIVE_WORD_ALIGNED])
async def test_stim_writes_the_slaves_memory_at_the_event(make_xcp, identification):
    client, simulator = make_xcp(identification_field=identification, auto_events=False)
    await client.connect()
    simulator.memory[0x40:0x48] = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    stimulation = await client.start_stimulation([DaqSignal('a', 0x40, 4), DaqSignal('b', 0x44, 4)])
    stim_list = next(d for d in simulator.slave.daq_lists if d.running)
    assert await wait_until(lambda: stim_list.stim_data)  # The current values, sent at the start
    stimulation.stimulate({'b': bytes([0xB0, 0xB1, 0xB2, 0xB3])})
    assert await wait_until(lambda: _stim_received(stimulation, stim_list))
    assert simulator.memory[0x44:0x48] == bytes([5, 6, 7, 8])  # Written at the event, not before
    simulator.trigger_event(0)
    # 'a' keeps the value it had: the master read it before starting
    assert simulator.memory[0x40:0x48] == bytes([1, 2, 3, 4, 0xB0, 0xB1, 0xB2, 0xB3])
    with pytest.raises(XcpError, match='takes 4 bytes'):
        stimulation.stimulate({'a': b'\x00'})
    with pytest.raises(XcpError, match='not stimulated'):
        stimulation.stimulate({'c': b'\x00'})
    await stimulation.stop()


async def test_stim_and_daq_together_on_static_lists(make_xcp):
    from can_explorer.core.xcp_slave import DEFAULT_STATIC_LISTS

    client, simulator = make_xcp(static_daq=DEFAULT_STATIC_LISTS, auto_events=False)
    await client.connect()
    samples = []
    measurement = await client.start_measurement(
        [DaqSignal('out', 0x60, 2, event=0)], samples.append, stimuli=[DaqSignal('in', 0x60, 2, event=0)]
    )
    stim_list = simulator.slave.daq_lists[3]  # The only list that can do STIM
    assert stim_list.running and stim_list.mode & 0x02
    measurement.stimulate({'in': bytes([0x34, 0x12])})
    assert await wait_until(lambda: stim_list.stim_data.get(0, b'')[:2] == bytes([0x34, 0x12]))  # CAN pads
    simulator.trigger_event(0)  # STIM is written first, then DAQ samples it
    assert await wait_until(lambda: samples)
    assert samples[0].values == {'out': bytes([0x34, 0x12])}
    await measurement.stop()


async def test_stim_is_repeated_each_period(make_xcp):
    client, simulator = make_xcp(auto_events=False)
    await client.connect()
    stimulation = await client.start_stimulation([DaqSignal('a', 0x40, 2)], period=0.01)
    assert await wait_until(lambda: stimulation.stimulations >= 3)
    await stimulation.stop()
    sent = stimulation.stimulations
    await asyncio.sleep(0.05)
    assert stimulation.stimulations == sent


async def test_stim_needs_the_resource(make_xcp):
    client, _simulator = make_xcp(stim=False)
    await client.connect()
    with pytest.raises(XcpError, match='does not offer STIM'):
        await client.start_stimulation([DaqSignal('a', 0x40, 2)])


# Predefined DAQ lists


def _predefined_slave(make_xcp, read_daq=True):
    from can_explorer.core.xcp_slave import StaticDaqList

    lists = (StaticDaqList(2, 3, fixed_event=1, entries=((0x100, 2), (0x104, 2))),)
    return make_xcp(predefined_daq=lists, read_daq=read_daq, auto_events=False)


@pytest.mark.parametrize('source', ['read_daq', 'a2l'])
async def test_predefined_lists_carry_what_they_can(make_xcp, source):
    from can_explorer.core.xcp_protocol import ALLOC_DAQ

    client, simulator = _predefined_slave(make_xcp, read_daq=source == 'read_daq')
    await client.connect()
    simulator.memory[0x100:0x106] = bytes([1, 2, 3, 4, 5, 6])
    content = {0: [[(0x100, 0, 2, 0xFF), (0x104, 0, 2, 0xFF)]]} if source == 'a2l' else None
    samples = []
    measurement = await client.start_measurement(
        [
            DaqSignal('a', 0x100, 2, event=1),  # Entry 0
            DaqSignal('b', 0x105, 1, event=1),  # The second byte of entry 1
            DaqSignal('c', 0x102, 2, event=1),  # Not carried: a list configured for it
            DaqSignal('d', 0x100, 2, event=0),  # Carried, but list 0 is fixed to event 1
        ],
        samples.append,
        predefined=content,
    )
    predefined = next(d for d in measurement.lists if d.predefined)
    assert predefined.number == 0 and set(predefined.views) == {'a', 'b'}
    allocated = [c for c in simulator.commands if c[0] == ALLOC_DAQ]
    assert int.from_bytes(allocated[0][2:4], 'little') == 2  # c on event 1, d on event 0
    simulator.trigger_event(1)
    simulator.trigger_event(0)
    assert await wait_until(lambda: len(samples) == 3)
    values = {k: v for s in samples for k, v in s.values.items()}
    assert values == {'a': bytes([1, 2]), 'b': bytes([6]), 'c': bytes([3, 4]), 'd': bytes([1, 2])}
    await measurement.stop()


async def test_predefined_lists_unknown_without_read_daq_or_a2l(make_xcp):
    client, simulator = _predefined_slave(make_xcp, read_daq=False)
    await client.connect()
    samples = []
    measurement = await client.start_measurement([DaqSignal('a', 0x100, 2, event=1)], samples.append)
    assert not any(d.predefined for d in measurement.lists)
    assert [d.number for d in measurement.lists] == [1]  # After the predefined list
    simulator.trigger_event(1)
    assert await wait_until(lambda: samples)
    await measurement.stop()


# STIM timeout


async def test_stim_timeout_sends_the_data_again(make_xcp):
    client, simulator = make_xcp(stim_timeout=True, auto_events=False)
    await client.connect()
    timeouts = []
    stimulation = await client.start_measurement(
        [], None, stimuli=[DaqSignal('a', 0x40, 2)], on_stim_timeout=timeouts.append
    )
    stim_list = next(d for d in simulator.slave.daq_lists if d.running)
    assert await wait_until(lambda: stim_list.stim_fresh)  # The data sent at the start
    simulator.trigger_event(0)  # Uses it: no timeout
    assert stimulation.stim_timeouts == 0
    simulator.trigger_event(0)  # Nothing new since: EV_STIM_TIMEOUT
    assert await wait_until(lambda: stimulation.stim_timeouts == 1 and stimulation.stimulations == 2)
    assert timeouts[0].name == 'STIM timeout'
    assert await wait_until(lambda: stim_list.stim_fresh)  # The resent data arrived
    await stimulation.stop()
    assert stimulation._on_event not in client.event_listeners
