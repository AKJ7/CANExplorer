import asyncio

import pytest

from can_explorer.core.doip import (
    DIAGNOSTIC_MESSAGE,
    GENERIC_NACK,
    ROUTING_ACTIVATED,
    VEHICLE_ANNOUNCEMENT,
    DoipClient,
    DoipEntity,
    DoipEntitySimulator,
    DoipError,
    EntityStatus,
    decode_message,
    discover,
    encode_message,
    entity_status,
    parse_eid,
    power_mode,
    read_message,
)
from can_explorer.core.flashing import FlashImage, FlashJob, FlashOptions, Segment
from can_explorer.core.simulation import UdsServer, xor_key
from can_explorer.core.uds import UdsClient, UdsNegativeResponse


@pytest.fixture
async def simulator():
    servers = {0x1001: UdsServer(slow_routines=(0x0F0F,)), 0x1002: UdsServer(dids={0xF190: b'SECONDECU00000000'})}
    simulator = DoipEntitySimulator(targets=servers)
    await simulator.start()
    yield simulator
    await simulator.stop()


def test_messages_round_trip():
    message = encode_message(DIAGNOSTIC_MESSAGE, bytes.fromhex('0E00 1001 22F190'))
    assert message[:8] == bytes.fromhex('02FD 8001 00000007')
    assert decode_message(message) == (0x02, DIAGNOSTIC_MESSAGE, bytes.fromhex('0E00100122F190'))
    with pytest.raises(DoipError, match='pattern'):
        decode_message(bytes.fromhex('02FC 8001 00000000'))
    with pytest.raises(DoipError, match='shorter'):
        decode_message(bytes.fromhex('02FD 8001 00000004 0E00'))

    entity = DoipEntity('10.0.0.2', 'WDB1234567890ABCD', 0x1000, parse_eid('00:1a:2b:3c:4d:5e'), bytes(6), 0x10, 0x00)
    decoded = DoipEntity.from_payload('10.0.0.2', entity.payload())
    assert decoded == entity
    assert (decoded.eid_text, decoded.further_action_text) == (
        '00:1A:2B:3C:4D:5E',
        'routing activation required for central security',
    )
    assert EntityStatus.from_payload(bytes.fromhex('00 04 01 00000FFF')).text == (
        'gateway, 1 of 4 TCP sockets open, diagnostic messages up to 4095 bytes'
    )
    with pytest.raises(ValueError):
        parse_eid('00:11:22')


async def test_discovery_status_and_power_mode(simulator):
    entities = await discover(simulator.host, simulator.port, timeout=0.3)
    assert [(e.vin, e.logical_address, e.eid_text, e.host) for e in entities] == [
        ('WDB1234567890ABCD', 0x1000, '02:00:5E:00:00:01', '127.0.0.1')
    ]
    assert await discover(simulator.host, simulator.port, timeout=0.3, vin='WDB1234567890ABCD')
    assert await discover(simulator.host, simulator.port, timeout=0.3, vin='OTHERVIN000000000') == []
    assert await discover(simulator.host, simulator.port, timeout=0.3, eid=bytes.fromhex('02005E000001'))
    status = await entity_status(simulator.host, simulator.port)
    assert (status.node_type, status.max_sockets, status.open_sockets) == (0x00, 2, 0)
    assert await power_mode(simulator.host, simulator.port) == 0x01


async def test_uds_over_doip(simulator):
    async with DoipClient(simulator.host, simulator.port) as client:
        assert client.activation.code == ROUTING_ACTIVATED and client.entity_address == 0x1000
        with client.transport(0x1001) as first, client.transport(0x1002) as second:
            uds = UdsClient(first)
            assert await uds.read_data_by_identifier(0xF190) == b'WDB1234567890ABCD'
            assert await UdsClient(second).read_data_by_identifier(0xF190) == b'SECONDECU00000000'
            await uds.diagnostic_session_control(0x03)
            assert await uds.security_access(1, xor_key) is True
            assert await uds.routine_control(0x01, 0x0F0F) == b'\x00'  # Answered after 'response pending'
            with pytest.raises(UdsNegativeResponse, match='requestOutOfRange'):
                await uds.read_data_by_identifier(0x1234)
            await uds.tester_present()  # Suppressed response: only acknowledged
        assert simulator.diagnostic_messages[0] == (0x0E00, 0x1001, bytes.fromhex('22F190'))
        assert await simulator.alive_check() == 1 and client.alive_checks == 1
        with client.transport(0x2000) as unknown:
            with pytest.raises(DoipError, match='unknown target address'):
                await unknown.send(bytes.fromhex('3E00'))
        assert (await entity_status(simulator.host, simulator.port)).open_sockets == 1


async def test_flashing_over_doip(simulator):
    image = FlashImage([Segment(0x8000, bytes(range(256)) * 20)])
    async with DoipClient(simulator.host, simulator.port) as client:
        with client.transport(0x1001) as transport:
            job = FlashJob(image, FlashOptions(security_level=1, compute_key=xor_key))
            await job.run(UdsClient(transport))
    assert simulator.targets[0x1001].memory == {0x8000: bytes(range(256)) * 20}


async def test_routing_activation_refused(simulator):
    with pytest.raises(DoipError, match='unknown source address'):
        await DoipClient(simulator.host, simulator.port, source_address=0x0001).connect()
    with pytest.raises(DoipError, match='unsupported routing activation type'):
        await DoipClient(simulator.host, simulator.port, activation_type=0xE0).connect()
    async with DoipClient(simulator.host, simulator.port):
        with pytest.raises(DoipError, match='already active on another socket'):
            await DoipClient(simulator.host, simulator.port).connect()
    with pytest.raises(DoipError, match='Could not connect|No connection'):
        await DoipClient('127.0.0.1', 1).connect()


async def test_entity_closing_the_connection(simulator):
    client = DoipClient(simulator.host, simulator.port)
    reasons = []
    client.add_close_listener(reasons.append)
    await client.connect()
    transport = client.transport(0x1001)
    receiving = asyncio.ensure_future(transport.receive(5.0))
    await asyncio.sleep(0.05)
    await simulator.stop()
    with pytest.raises(DoipError, match='closed the connection'):
        await asyncio.wait_for(receiving, 1.0)  # Woken up at once, not after the timeout
    await client.wait_closed()
    assert not client.is_open and reasons == ['The DoIP entity closed the connection']
    result = await UdsClient(transport).request(bytes.fromhex('22F190'))
    assert result.error == 'Not connected'
    transport.close()
    await client.close()


async def test_client_answers_alive_checks_and_rejects_garbage():
    """Against a minimal entity written here: alive checks are answered, a malformed header ends the connection."""
    answers = []
    finished = asyncio.Event()

    async def entity(reader, writer):
        _, _, request = await read_message(reader)
        writer.write(encode_message(0x0006, request[:2] + bytes.fromhex('1000 10 00000000')))
        writer.write(encode_message(0x0007))
        await writer.drain()
        answers.append(await read_message(reader))
        writer.write(b'\x00\x00\x00\x00\x00\x00\x00\x00')  # Not a DoIP header
        await writer.drain()
        answers.append(await read_message(reader))
        writer.close()
        finished.set()

    server = await asyncio.start_server(entity, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    client = DoipClient('127.0.0.1', port, source_address=0x0E80)
    await client.connect()
    await asyncio.wait_for(client.wait_closed(), 2.0)
    await asyncio.wait_for(finished.wait(), 2.0)
    assert answers == [(0x02, 0x0008, bytes.fromhex('0E80')), (0x02, GENERIC_NACK, b'\x00')]
    assert 'Incorrect DoIP header pattern' in client.close_reason
    await client.close()
    server.close()
    await server.wait_closed()


async def test_simulator_rejects_unknown_udp_requests(simulator):
    loop = asyncio.get_running_loop()
    received = loop.create_future()

    class Protocol(asyncio.DatagramProtocol):
        def datagram_received(self, data, address):
            received.set_result(decode_message(data))

    transport, _ = await loop.create_datagram_endpoint(Protocol, remote_addr=(simulator.host, simulator.port))
    transport.sendto(encode_message(0x1234))
    assert await asyncio.wait_for(received, 1.0) == (0x02, GENERIC_NACK, b'\x01')
    transport.close()
    assert VEHICLE_ANNOUNCEMENT == 0x0004
