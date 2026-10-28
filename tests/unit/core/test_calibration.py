import uuid
from pathlib import Path

import pytest

from can_explorer.core.a2l import read_a2l
from can_explorer.core.can_command import CanCommandClient, CanCommandError
from can_explorer.core.ccp import CcpClient, CcpSlaveSimulator
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.xcp import XcpClient, XcpError, XcpSlaveSimulator

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
    assert client.connected and response[0] == 0x00  # resource
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
