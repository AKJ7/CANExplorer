import asyncio
import uuid

import pytest

from can_explorer.core.isotp_client import IsoTpClient, IsoTpError, parse_st_min
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import describe_request, describe_response, uds_request


@pytest.fixture
async def tester_and_ecu():
    channel = f'isotp-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu.start()
    yield tester, ecu
    tester.stop()
    ecu.stop()


def test_parse_st_min():
    assert parse_st_min(0) == 0
    assert parse_st_min(0x7F) == 0.127
    assert parse_st_min(0xF1) == pytest.approx(0.0001)
    assert parse_st_min(0xFA) == 0.127


@pytest.mark.parametrize('length', [1, 7, 8, 62, 100, 4095, 5000])
async def test_transfer_both_directions(tester_and_ecu, length):
    tester_session, ecu_session = tester_and_ecu
    payload = bytes(i % 256 for i in range(length))
    with (
        IsoTpClient(tester_session, 0x7E0, 0x7E8) as tester,
        IsoTpClient(ecu_session, 0x7E8, 0x7E0, block_size=4, st_min=0xF1) as ecu,
    ):
        await asyncio.gather(tester.send(payload), ecu_received := asyncio.ensure_future(ecu.receive(5)))
        assert ecu_received.result() == payload
        await asyncio.gather(ecu.send(payload[::-1]), tester_received := asyncio.ensure_future(tester.receive(5)))
        assert tester_received.result() == payload[::-1]


@pytest.mark.parametrize('length', [10, 62, 63, 500])
async def test_can_fd_transfer(tester_and_ecu, length):
    tester_session, ecu_session = tester_and_ecu
    payload = bytes(i % 256 for i in range(length))
    with (
        IsoTpClient(tester_session, 0x7E0, 0x7E8, fd=True) as tester,
        IsoTpClient(ecu_session, 0x7E8, 0x7E0, fd=True) as ecu,
    ):
        await asyncio.gather(tester.send(payload), received := asyncio.ensure_future(ecu.receive(5)))
    assert received.result() == payload
    frames = [frame.message for frame in tester_session.trace if not frame.message.is_rx]
    assert all(msg.is_fd and len(msg.data) in (8, 12, 16, 20, 24, 32, 48, 64) for msg in frames)


async def test_missing_flow_control_times_out(tester_and_ecu):
    tester_session, _ = tester_and_ecu
    with IsoTpClient(tester_session, 0x7E0, 0x7E8, timeout=0.1) as tester:
        with pytest.raises(IsoTpError, match='flow control'):
            await tester.send(bytes(20))


async def test_uds_request_with_response_pending(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    vin = b'WDB1234567890ABCD'

    async def ecu_task(ecu: IsoTpClient):
        request = await ecu.receive(2)
        assert request == bytes([0x22, 0xF1, 0x90])
        await ecu.send(bytes([0x7F, 0x22, 0x78]))
        await asyncio.sleep(0.05)
        await ecu.send(bytes([0x62, 0xF1, 0x90]) + vin)

    with IsoTpClient(tester_session, 0x7E0, 0x7E8) as tester, IsoTpClient(ecu_session, 0x7E8, 0x7E0) as ecu:
        task = asyncio.ensure_future(ecu_task(ecu))
        result = await uds_request(tester, bytes([0x22, 0xF1, 0x90]))
        await task
    assert result.positive and result.error is None
    assert result.response == bytes([0x62, 0xF1, 0x90]) + vin
    assert result.description.startswith('Positive response to ReadDataByIdentifier: F1 90 57 44 42')


async def test_uds_request_without_ecu(tester_and_ecu):
    tester_session, _ = tester_and_ecu
    with IsoTpClient(tester_session, 0x7E0, 0x7E8, timeout=0.1) as tester:
        result = await uds_request(tester, bytes([0x3E, 0x00]))
    assert not result.positive and result.error == 'Timeout waiting for a response'


def test_uds_descriptions():
    assert describe_request(bytes([0x10, 0x03])) == 'DiagnosticSessionControl 03'
    assert describe_response(bytes([0x7F, 0x27, 0x35])) == 'Negative response to SecurityAccess: invalidKey'
    assert describe_response(bytes([0x50, 0x03])) == 'Positive response to DiagnosticSessionControl: 03'
    assert describe_response(b'') == 'empty response'
