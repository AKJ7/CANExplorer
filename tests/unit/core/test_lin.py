import asyncio
import uuid

import can
import pytest

from can_explorer.core.busstats import BusStatistics
from can_explorer.core.decoding import FrameDecoder, open_signal_database
from can_explorer.core.frames import Frame
from can_explorer.core.ldf import LdfDecoder
from can_explorer.core.lin import LinDecoder, LinTpReassembler, checksum, frame_duration, protected_id
from can_explorer.core.lin_schedule import LinScheduler
from can_explorer.core.lin_tp import LinTpClient, LinTpError
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import LinSlaveSimulator, UdsServer
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import UdsClient


def lin_message(frame_id, data=None, rx=True):
    if data is None:
        return can.Message(arbitration_id=frame_id, is_extended_id=False, is_remote_frame=True, dlc=8, is_rx=rx)
    return can.Message(arbitration_id=frame_id, is_extended_id=False, data=bytes(data), is_rx=rx)


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def ldf(data_dir):
    return LdfDecoder(data_dir / 'example.ldf')


@pytest.fixture
async def master_and_slaves():
    channel = f'lin-{uuid.uuid4().hex}'
    config = BusConfig('virtual', channel, bus_type='LIN')
    master, slaves = BusSession(LiveSource(config)), BusSession(LiveSource(config))
    master.start()
    slaves.start()
    yield master, slaves
    master.stop()
    slaves.stop()


# Frame model


@pytest.mark.parametrize(
    'frame_id, pid', [(0x00, 0x80), (0x01, 0xC1), (0x02, 0x42), (0x03, 0x03), (0x10, 0x50), (0x3C, 0x3C), (0x3D, 0x7D)]
)
def test_protected_identifier(frame_id, pid):
    assert protected_id(frame_id) == pid


def test_checksums():
    # Example of the LIN 2.x specification: enhanced checksum over PID 0x4A and data 55 93 E5
    assert checksum(bytes([0x55, 0x93, 0xE5]), 0x4A) == 0xE6
    assert checksum(bytes([0x01, 0x02])) == 0xFC
    with pytest.raises(ValueError):
        protected_id(64)


def test_frame_duration_and_bus_load():
    assert frame_duration(lin_message(0x10), 19200) == pytest.approx(34 / 19200)
    assert frame_duration(lin_message(0x10, [1, 2]), 19200) == pytest.approx(30 / 19200)
    assert frame_duration(lin_message(0x11, [1], rx=False), 19200) == pytest.approx(54 / 19200)
    statistics = BusStatistics(None, 19200)
    statistics.sample(0.0)
    for _ in range(10):
        statistics.update(lin_message(0x10))
        statistics.update(lin_message(0x10, [1, 2]))
    assert statistics.sample(1.0).load == pytest.approx(10 * 64 / 19200)


# Passive decoding


def test_decoder_headers_responses_and_diagnostics():
    decoder = LinDecoder()
    assert decoder.describe(lin_message(0x10)) == 'LIN header ID 0x10 (PID 0x50)'
    assert decoder.describe(lin_message(0x10, [1, 2])) == 'LIN response ID 0x10 (PID 0x50)'
    request = decoder.describe(lin_message(0x3C, [0x01, 0x03, 0x22, 0xF1, 0x90, 0xFF, 0xFF, 0xFF]))
    assert request == 'LIN master request NAD 0x01 SF: ReadDataByIdentifier F1 90'
    payload = bytes([0x62, 0xF1, 0x90]) + b'WDB12345'
    assert 'FF (total 11 B)' in decoder.describe(lin_message(0x3D, [0x01, 0x10, 11, *payload[:5]]))
    complete = decoder.describe(lin_message(0x3D, [0x01, 0x21, *payload[5:], 0xFF, 0xFF, 0xFF, 0xFF, 0xFF][:8]))
    assert complete.endswith('Positive response to ReadDataByIdentifier: F1 90 57 44 42 31 32 33 34 35')
    node_configuration = decoder.describe(lin_message(0x3C, [0x01, 0x06, 0xB2, 0x00, 0x34, 0x12, 0x78, 0x56]))
    assert node_configuration.endswith('ReadByIdentifier: 00 34 12 78 56')
    assert decoder.describe(can.Message(arbitration_id=0x100, is_extended_id=False)) is None


def test_transport_reassembler_sequence_error():
    reassembler = LinTpReassembler()
    reassembler.feed(0x3D, bytes([0x01, 0x10, 20, 1, 2, 3, 4, 5]))
    description, payload = reassembler.feed(0x3D, bytes([0x01, 0x22, 0, 0, 0, 0, 0, 0]))
    assert 'aborted' in description and payload is None


# LDF


def test_ldf_decoding(ldf, data_dir):
    msg = lin_message(0x10, [10, 0x2C, 0x01])
    assert ldf.describe(msg) == 'SeatStatus: SeatPosition=5.000 mm, SeatTemperature=-10.000 degC'
    assert ldf.decode(msg) == {'SeatPosition': 5.0, 'SeatTemperature': -10.0}
    assert ldf.message_definition(lin_message(0x10)) is None  # Headers carry no data
    heating = ldf.message_definition(lin_message(0x11, [2]))
    assert heating.decode(b'\x02') == {'SeatHeating': 'High'}
    assert heating.decode(b'\x02', decode_choices=False) == {'SeatHeating': 2}
    assert [(s.message, s.signal, s.unit) for s in ldf.signals()][-2:] == [
        ('SeatStatus', 'SeatPosition', 'mm'),
        ('SeatStatus', 'SeatTemperature', 'degC'),
    ]
    assert isinstance(open_signal_database(data_dir / 'example.ldf'), LdfDecoder)
    decoder = FrameDecoder(LinDecoder(), ldf)
    frame = Frame(msg, decoder.describe(msg))
    assert decoder.full_description(frame).endswith('| SeatStatus: SeatPosition=5.000 mm, SeatTemperature=-10.000 degC')


# Master schedule and diagnostics over LIN


async def test_schedule_table(master_and_slaves, ldf):
    master, slaves = master_and_slaves
    simulator = LinSlaveSimulator(slaves, {0x10: bytes([20, 0x2C, 0x01]), 0x20: bytes([0x30])})
    assert LinScheduler.tables(ldf) == ['NormalTable', 'DiagnosticTable']
    scheduler = LinScheduler(master, ldf, 'NormalTable')
    scheduler.set_signal('SeatHeating', 'High')
    with pytest.raises(KeyError):
        scheduler.set_signal('SeatPosition', 1)  # Published by a slave
    with pytest.raises(ValueError):
        scheduler.set_signal('SeatHeating', 'Boiling')
    scheduler.start()
    await wait_until(lambda: scheduler.cycles >= 3)
    scheduler.stop()
    await asyncio.sleep(0.05)
    frames = [
        (f.message.arbitration_id, f.message.is_rx, f.message.is_remote_frame, bytes(f.message.data))
        for f in master.trace
    ]
    assert (0x10, False, True, b'') in frames  # Header for the slave frame
    assert (0x10, True, False, bytes([20, 0x2C, 0x01])) in frames  # Slave response
    assert (0x11, False, False, b'\x02') in frames  # Master frame with SeatHeating = High
    assert (0x21, False, False, b'\x00') in frames
    assert simulator.headers_seen >= 6
    simulator.close()


async def test_uds_over_lin_transport(master_and_slaves):
    master, slaves = master_and_slaves
    server = UdsServer()
    simulator = LinSlaveSimulator(slaves, nad=0x01, server=server)
    with LinTpClient(master, 0x01) as transport:
        client = UdsClient(transport)
        assert await client.read_data_by_identifier(0xF190) == b'WDB1234567890ABCD'  # Multi-frame response
        await client.diagnostic_session_control(0x03)
        long_request = bytes([0x31, 0x01, 0x02, 0x03]) + bytes(range(10))  # Multi-frame request
        assert await client.routine_control(0x01, 0x0203, bytes(range(10))) == b'\x00'
        assert server.requests[-1] == long_request
    with LinTpClient(master, 0x05, timeout=0.1) as other:
        with pytest.raises(LinTpError, match='Timeout'):
            await other.request(bytes([0x3E, 0x00]))
    simulator.close()
