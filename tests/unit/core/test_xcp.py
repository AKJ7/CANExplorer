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
