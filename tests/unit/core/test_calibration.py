import uuid
from pathlib import Path

import pytest

from can_explorer.core.a2l import read_a2l
from can_explorer.core.can_command import CanCommandClient, CanCommandError
from can_explorer.core.ccp import CcpClient, CcpError, CcpSlaveSimulator
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.xcp import XcpClient, XcpError, XcpSlaveSimulator
from tests.unit.waiting import wait_until

DATA = Path(__file__).resolve().parents[2] / 'data'


def _sessions(channel):
    master = BusSession(LiveSource(BusConfig('virtual', channel)))
    slave = BusSession(LiveSource(BusConfig('virtual', channel)))
    master.start()
    slave.start()
    return master, slave


@pytest.fixture
async def xcp():
    channel = f'xcp-{uuid.uuid4().hex}'
    master, slave = _sessions(channel)
    simulator = XcpSlaveSimulator(slave, request_id=0x7E0, response_id=0x7E1)
    transport = CanCommandClient(master, 0x7E0, 0x7E1, timeout=0.3)
    client = XcpClient(transport)
    yield client, simulator
    transport.close()
    simulator.stop()
    master.stop()
    slave.stop()


@pytest.fixture
async def ccp():
    channel = f'ccp-{uuid.uuid4().hex}'
    master, slave = _sessions(channel)
    simulator = CcpSlaveSimulator(slave, request_id=0x7E0, response_id=0x7E1, station_address=0x0039)
    transport = CanCommandClient(master, 0x7E0, 0x7E1, timeout=0.3)
    client = CcpClient(transport, station_address=0x0039)
    yield client, simulator
    transport.close()
    simulator.stop()
    master.stop()
    slave.stop()


async def test_xcp_connect_read_write(xcp):
    client, simulator = xcp
    response = await client.connect()
    assert client.connected and response[0] == 0x05  # Resources: CAL/PAG and DAQ
    simulator.memory[0x1000:0x1002] = (2000).to_bytes(2, 'little')
    assert await client.read(0x1000, 0, 2) == (2000).to_bytes(2, 'little')
    await client.write(0x2000, 0, (1500).to_bytes(2, 'little'))
    assert simulator.memory[0x2000:0x2002] == (1500).to_bytes(2, 'little')
    await client.get_status()
    await client.disconnect()
    assert not client.connected


async def test_xcp_multi_frame_upload_and_errors(xcp):
    client, simulator = xcp
    await client.connect()
    simulator.memory[0x100:0x110] = bytes(range(16))
    assert await client.read(0x100, 0, 16) == bytes(range(16))  # Larger than one CTO: set_mta + upload
    with pytest.raises(XcpError, match='address out of range'):
        await client.read(len(simulator.memory) - 1, 0, 8)


async def test_ccp_connect_read_write(ccp):
    client, simulator = ccp
    await client.connect()
    assert client.connected
    assert (await client.get_version())[:2] == bytes([2, 1])
    simulator.memory[0x1000:0x1002] = (0x1234).to_bytes(2, 'big')
    assert await client.read(0x1000, 0, 2) == (0x1234).to_bytes(2, 'big')
    await client.write(0x2000, 0, (0xABCD).to_bytes(2, 'big'))
    assert simulator.memory[0x2000:0x2002] == (0xABCD).to_bytes(2, 'big')
    await client.disconnect()
    assert not client.connected


async def test_ccp_ignores_other_station_address():
    channel = f'ccp-{uuid.uuid4().hex}'
    master, slave = _sessions(channel)
    simulator = CcpSlaveSimulator(slave, station_address=0x0039)
    transport = CanCommandClient(master, 0x7E0, 0x7E1, timeout=0.2)
    try:
        client = CcpClient(transport, station_address=0x0040)  # Wrong station: the slave stays silent
        with pytest.raises(CanCommandError, match='No response'):
            await client.connect()
        assert not simulator.connected
    finally:
        transport.close()
        simulator.stop()
        master.stop()
        slave.stop()


async def test_calibration_with_a2l(xcp):
    """Read and write A2L variables in physical units over XCP."""
    client, simulator = xcp
    await client.connect()
    variables = {v.name: v for v in read_a2l(DATA / 'example.a2l')}

    speed = variables['EngineSpeed']  # UWORD at 0x1000, factor 0.25
    simulator.memory[speed.address : speed.address + speed.size] = (6000).to_bytes(2, 'little')
    raw = await client.read(speed.address, 0, speed.size)
    assert speed.decode(raw, client.byte_order) == 1500.0  # 6000 * 0.25 rpm

    idle = variables['IdleTarget']  # writable UWORD at 0x2000, factor 0.25
    await client.write(idle.address, 0, idle.encode(850.0, client.byte_order))
    stored = simulator.memory[idle.address : idle.address + idle.size]
    assert idle.decode(stored, client.byte_order) == 850.0


async def test_command_client_requires_an_open_session():
    master, slave = _sessions(f'cc-{uuid.uuid4().hex}')
    slave.stop()
    transport = CanCommandClient(slave, 0x7E0, 0x7E1)
    with pytest.raises(CanCommandError):
        transport.send(bytes([0xFF]))
    transport.close()
    master.stop()


@pytest.fixture
def make_ccp():
    created = []

    def make(compute_key=None, **options):
        master, slave = _sessions(f'ccp-{uuid.uuid4().hex}')
        simulator = CcpSlaveSimulator(slave, 0x7E0, 0x7E1, station_address=0x39, **options)
        transport = CanCommandClient(master, 0x7E0, 0x7E1, timeout=0.2)
        client = CcpClient(transport, station_address=0x39, compute_key=compute_key)
        created.append((master, slave, simulator, transport))
        return client, simulator

    yield make
    for master, slave, simulator, transport in created:
        transport.close()
        simulator.stop()
        master.stop()
        slave.stop()


async def test_ccp_skips_late_responses_and_events(make_ccp):
    client, simulator = make_ccp()
    await client.connect()
    simulator.memory[0x10:0x12] = b'\x12\x34'
    # A stale CRM (an earlier counter) and an event message arrive before the real answer
    simulator._crm((client._counter - 5) & 0xFF, b'\xde\xad')
    simulator._send(bytes([0xFE, 0x20, 0x00]))
    assert await client.read(0x10, 0, 2) == b'\x12\x34'
    assert await wait_until(lambda: client.events)
    assert client.events[-1].name == 'cold start request'


async def test_ccp_busy_timeout_and_dnload_6(make_ccp):
    client, simulator = make_ccp()
    await client.connect()
    simulator.busy_count = 1
    await client.write(0x100, 0, bytes(range(14)))  # DNLOAD_6, DNLOAD_6, DNLOAD 2
    assert simulator.memory[0x100:0x10E] == bytes(range(14))
    assert [c[0] for c in simulator.commands].count(0x23) == 2
    await client.set_mta(0x100)
    simulator.drop_count = 1
    assert await client.upload(4) == bytes(range(4))  # SET_MTA again, then the UPLOAD: not shifted


async def test_ccp_seed_and_key(make_ccp):
    from can_explorer.core.ccp import RESOURCE_CAL, demo_key

    client, simulator = make_ccp(protected=RESOURCE_CAL)
    await client.connect()
    with pytest.raises(CcpError, match='access locked'):
        await client.write(0x100, 0, b'\x01')
    client.compute_key = demo_key
    await client.write(0x100, 0, b'\x01')
    assert simulator.memory[0x100] == 1 and client.privileges & RESOURCE_CAL
    with pytest.raises(CcpError, match='UNLOCK: access locked'):  # A wrong key
        simulator.locked = RESOURCE_CAL
        await client.unlock(RESOURCE_CAL, lambda resource, seed: b'\x00')


async def test_ccp_identification_checksum_pages(make_ccp):
    import zlib

    from can_explorer.core.ccp import REFERENCE_PAGE_ADDRESS

    client, simulator = make_ccp()
    await client.connect()
    identification = await client.exchange_id()
    assert identification['id'] == 'example' and identification['resources'] == 0x03
    simulator.memory[0:16] = bytes(range(16))
    assert await client.build_checksum(0, 16) == zlib.crc32(bytes(range(16))).to_bytes(4, 'big')
    await client.write(0, 0, b'\xff')
    await client.select_cal_page(REFERENCE_PAGE_ADDRESS)
    assert await client.get_active_cal_page() == (0, REFERENCE_PAGE_ADDRESS)
    assert await client.read(0, 0, 1) == b'\x00'
    await client.select_cal_page(0)
    assert await client.read(0, 0, 1) == b'\xff'


async def test_ccp_daq(make_ccp):
    from can_explorer.core.xcp import DaqSignal

    client, simulator = make_ccp(auto_events=False)
    await client.connect()
    simulator.memory[0x1000:0x1002] = (1234).to_bytes(2, 'big')
    simulator.memory[0x1010:0x101B] = bytes(range(11))  # 11 bytes: two ODTs, elements 4+2+1, 4+...
    samples = []
    measurement = await client.start_measurement(
        [DaqSignal('speed', 0x1000, 2), DaqSignal('blob', 0x1010, 11), DaqSignal('slow', 0x1000, 1, event=1)],
        samples.append,
    )
    simulator.trigger_event(0)
    simulator.trigger_event(1)
    assert await wait_until(lambda: len(samples) == 2)
    fast = next(s for s in samples if s.event == 0)
    assert fast.values == {'speed': (1234).to_bytes(2, 'big'), 'blob': bytes(range(11))}
    assert next(s for s in samples if s.event == 1).values == {'slow': b'\x04'}
    await measurement.stop()
    assert not any(d.running for d in simulator.daq_lists)


@pytest.fixture
async def example():
    from can_explorer.core.a2l import load_a2l
    from can_explorer.core.xcp_slave import example_memory

    master, slave = _sessions(f'cal-{uuid.uuid4().hex}')
    simulator = XcpSlaveSimulator(slave, memory=example_memory())
    transport = CanCommandClient(master, 0x7E0, 0x7E1, timeout=0.3)
    client = XcpClient(transport)
    await client.connect()
    yield client, simulator, load_a2l(DATA / 'example.a2l')
    transport.close()
    simulator.stop()
    master.stop()
    slave.stop()


async def test_read_and_write_every_kind(example):
    from can_explorer.core.calibration import check_epk, read_value, write_value

    client, simulator, document = example
    value = await read_value(client, document.variable('FuelMap'), document)
    assert value.axes == [[1000.0, 2000.0, 4000.0, 6000.0], [20.0, 50.0, 80.0]]
    assert value.value[1] == [20.0, 30.0, 40.0, 50.0]  # Row y = 50 %
    rows = [row[:] for row in value.value]
    rows[2][3] = 77.5
    await write_value(client, document.variable('FuelMap'), rows, document)
    assert simulator.memory[0x220A + 11] == 155  # ROW_DIR: y outer, x inner; 77.5 / 0.5

    curve = await read_value(client, document.variable('IgnitionCurve'), document)
    assert curve.value == [5.0, 10.0, 15.0, 20.0, 25.0] and curve.axes[0][-1] == 6000.0
    await write_value(client, document.variable('IgnitionCurve'), [6, 11, 16, 21, 26], document,
                      axes=[[1000, 2000, 3000, 5000, 7000]])  # fmt: skip
    curve = await read_value(client, document.variable('IgnitionCurve'), document)
    assert curve.value == [6.0, 11.0, 16.0, 21.0, 26.0] and curve.axes[0][3:] == [5000.0, 7000.0]

    with pytest.raises(Exception, match='above its maximum'):
        await write_value(client, document.variable('IgnitionCurve'), [6, 11, 16, 21, 99], document)
    with pytest.raises(Exception, match='5 values'):
        await write_value(client, document.variable('IgnitionCurve'), [1, 2], document)

    await write_value(client, document.variable('FanMode'), 'On', document)
    assert (await read_value(client, document.variable('FanMode'), document)).value == 'On'
    await write_value(client, document.variable('VariantCode'), 'US-9', document)
    assert simulator.memory[0x2500:0x2508] == b'US-9\0\0\0\0'
    await write_value(client, document.variable('GearRatios'), [4, 3, 2, 1, 0.5, 0.25], document)
    assert (await read_value(client, document.variable('GearRatios'), document)).value[-1] == 0.25
    await write_value(client, document.variable('LoadAxis'), [10, 40, 90], document)
    assert simulator.memory[0x2301:0x2304] == bytes([10, 40, 90])
    with pytest.raises(Exception, match='read-only'):
        await write_value(client, document.variable('EngineSpeed'), 1, document)
    assert (await check_epk(client, document))['match'] is True
    simulator.memory[0x3000] = ord('Y')
    assert (await check_epk(client, document))['match'] is False


async def test_read_many_merges_neighbours(example):
    from can_explorer.core.calibration import read_many

    client, simulator, document = example
    measurements = [v for v in document.variables if v.kind == 'MEASUREMENT']
    count = len(simulator.commands)
    raw = await read_many(client, measurements)
    assert raw['EngineSpeed'] == (6000).to_bytes(2, 'little') and raw['WheelSpeeds'][:2] == (5000).to_bytes(2, 'little')
    assert raw['BrakePressed'] == raw['ClutchPressed'] == b'\x01'
    assert len(simulator.commands) - count == 5  # One merged 24-byte range: SET_MTA + 4 UPLOADs, not 7 reads


async def test_datasets_round_trip(example, tmp_path):
    from can_explorer.core.calibration import (
        compare_datasets,
        load_dataset,
        read_dataset,
        save_dataset,
        write_dataset,
    )

    client, simulator, document = example
    dataset, errors = await read_dataset(client, document)
    assert not errors and set(dataset) >= {'IdleTarget', 'FuelMap', 'IgnitionCurve', 'FanMode', 'VariantCode'}
    for suffix in ('json', 'dcm'):
        path = tmp_path / f'set.{suffix}'
        save_dataset(path, dataset, document)
        assert compare_datasets(dataset, load_dataset(path)) == []
    changed = load_dataset(tmp_path / 'set.dcm')
    changed['IdleTarget']['value'] = 900.0
    changed['FanMode']['value'] = 'Off'
    assert {d['name'] for d in compare_datasets(dataset, changed)} == {'IdleTarget', 'FanMode'}
    assert await write_dataset(client, document, changed) == {}
    now, _ = await read_dataset(client, document)
    assert compare_datasets(changed, now) == []
