import asyncio
import time
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


@pytest.mark.parametrize('st_min, separation', [(0xF1, 0.0001), (0x05, 0.005)])
async def test_separation_time_is_kept_precisely(tester_and_ecu, st_min, separation):
    """Consecutive frames are never closer than STmin, and sub-millisecond STmin does not take a timer tick each
    (about 15.6 ms on Windows, which made long transfers time out)."""
    tester_session, ecu_session = tester_and_ecu
    times = []

    def on_message(msg):
        if msg.arbitration_id == 0x7E0 and msg.data and msg.data[0] >> 4 == 2:  # Consecutive frames
            times.append(time.perf_counter())

    tester_session.add_message_listener(on_message)
    payload = bytes(i % 256 for i in range(700 if st_min == 0xF1 else 120))
    with (
        IsoTpClient(tester_session, 0x7E0, 0x7E8) as tester,
        IsoTpClient(ecu_session, 0x7E8, 0x7E0, block_size=0, st_min=st_min) as ecu,
    ):
        started = time.perf_counter()
        received = asyncio.ensure_future(ecu.receive(5))
        await tester.send(payload)
        assert await received == payload
        elapsed = time.perf_counter() - started
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    assert gaps and min(gaps) >= separation * 0.95
    if st_min == 0xF1:
        assert elapsed < 1.0, f'{len(times)} frames took {elapsed:.2f} s'


# Extended and mixed addressing: an address byte starts every frame


def test_parse_isotp_address():
    from can_explorer.core.isotp_client import format_isotp_address, parse_isotp_address

    assert parse_isotp_address('7E0') == (0x7E0, None)
    assert parse_isotp_address('6F1:12') == (0x6F1, 0x12)
    assert parse_isotp_address(' 0x18DA00F1 : f1 ', extended=True) == (0x18DA00F1, 0xF1)
    assert format_isotp_address(0x6F1, 0x12) == '6F1:12' and format_isotp_address(0x7E0, None) == '7E0'
    with pytest.raises(ValueError):
        parse_isotp_address('6F1:1G2')
    with pytest.raises(ValueError):
        parse_isotp_address('6F1:100')
    with pytest.raises(ValueError):
        parse_isotp_address('800')  # 11-bit identifier out of range


@pytest.mark.parametrize('length', [1, 6, 7, 13, 300])
async def test_extended_addressing_transfer(tester_and_ecu, length):
    tester_session, ecu_session = tester_and_ecu
    payload = bytes(i % 256 for i in range(length))
    with (
        IsoTpClient(tester_session, 0x6F1, 0x612, tx_address=0x12, rx_address=0xF1) as tester,
        IsoTpClient(ecu_session, 0x612, 0x6F1, tx_address=0xF1, rx_address=0x12, block_size=3) as ecu,
    ):
        await asyncio.gather(tester.send(payload), received := asyncio.ensure_future(ecu.receive(5)))
        assert received.result() == payload
        await asyncio.gather(ecu.send(payload[::-1]), answer := asyncio.ensure_future(tester.receive(5)))
        assert answer.result() == payload[::-1]
    sent = [frame.message for frame in tester_session.trace if not frame.message.is_rx]
    assert sent and all(msg.data[0] == 0x12 for msg in sent)  # Requests and flow control carry the address byte
    if length <= 6:
        assert sent[0].data[1] == length  # A single frame holds six bytes after the address byte


async def test_extended_addressing_ignores_other_address_bytes(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    with (
        IsoTpClient(tester_session, 0x6F1, 0x612, tx_address=0x40) as tester,  # Frames for node 0x40
        IsoTpClient(ecu_session, 0x612, 0x6F1, rx_address=0x12) as ecu,
    ):
        await tester.send(b'\x10\x03')
        with pytest.raises(IsoTpError):
            await ecu.receive(0.2)


async def test_extended_addressing_can_fd(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    payload = bytes(range(61))  # The largest CAN FD single frame with an address byte (64 - 3)
    with (
        IsoTpClient(tester_session, 0x6F1, 0x612, fd=True, tx_address=0x12) as tester,
        IsoTpClient(ecu_session, 0x612, 0x6F1, fd=True, rx_address=0x12) as ecu,
    ):
        await asyncio.gather(tester.send(payload), received := asyncio.ensure_future(ecu.receive(5)))
    assert received.result() == payload
    sent = [frame.message for frame in tester_session.trace if not frame.message.is_rx]
    assert len(sent) == 1 and bytes(sent[0].data[:3]) == bytes([0x12, 0x00, 61])


async def test_classic_frame_with_zero_length_is_no_fd_escape(tester_and_ecu):
    """The single-frame escape (length 0, length in byte 2) is only valid in CAN FD frames longer than 8 bytes."""
    import can

    tester_session, ecu_session = tester_and_ecu
    with IsoTpClient(ecu_session, 0x7E8, 0x7E0) as ecu:
        tester_session.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, data=bytes([0x00, 2, 0x10, 3])))
        with pytest.raises(IsoTpError):
            await ecu.receive(0.2)


async def test_stalled_reception_is_dropped(tester_and_ecu):
    """N_Cr: consecutive frames that come later than the timeout do not continue an old transfer."""
    import can

    tester_session, ecu_session = tester_and_ecu
    with IsoTpClient(ecu_session, 0x7E8, 0x7E0, timeout=0.05) as ecu:

        def send(data):
            tester_session.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, data=bytes(data)))

        send([0x10, 10, 1, 2, 3, 4, 5, 6])  # First frame of 10 bytes
        await asyncio.sleep(0.2)  # Longer than N_Cr
        send([0x21, 7, 8, 9, 10, 0xCC, 0xCC, 0xCC])
        with pytest.raises(IsoTpError):
            await ecu.receive(0.2)
