import asyncio
import uuid

import can
import pytest

from can_explorer.core.session import BusSession, SessionError, SessionState
from can_explorer.core.sources import BusConfig, FileSource, LiveSource


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def channel():
    return f'test-{uuid.uuid4().hex}'


@pytest.fixture
def peer(channel):
    bus = can.Bus(interface='virtual', channel=channel)
    yield bus
    bus.shutdown()


def live_session(channel, **config):
    return BusSession(LiveSource(BusConfig(interface='virtual', channel=channel, **config)))


async def test_receives_messages_in_order(channel, peer):
    session = live_session(channel)
    states = []
    session.add_state_listener(lambda s: states.append(s.state))
    session.start()
    for i in range(200):
        peer.send(can.Message(arbitration_id=i, is_extended_id=False, data=bytes([i % 256])))
    await wait_until(lambda: session.trace.total == 200)
    assert [frame.message.arbitration_id for frame in session.trace] == list(range(200))
    assert len(session.statistics.entries) == 200
    assert session.start_time is not None
    session.stop()
    assert states == [SessionState.OPEN, SessionState.CLOSED]


@pytest.mark.parametrize('receive_own_messages', [False, True])
async def test_transmitted_frames_appear_once(channel, peer, receive_own_messages):
    session = live_session(channel, receive_own_messages=receive_own_messages)
    session.start()
    session.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b'\x01'))
    assert peer.recv(timeout=1).arbitration_id == 0x123
    await wait_until(lambda: session.trace.total >= 1)
    await asyncio.sleep(0.05)
    assert session.trace.total == 1
    assert not next(iter(session.trace)).message.is_rx
    session.stop()


async def test_periodic_transmission(channel, peer):
    session = live_session(channel)
    periodic_changes = []
    session.add_periodic_listener(lambda s: periodic_changes.append(len(s.periodic)))
    session.start()
    session.start_periodic(can.Message(arbitration_id=0x200, is_extended_id=False), period=0.02)
    await asyncio.sleep(0.21)
    session.stop_periodic()
    await asyncio.sleep(0.01)
    sent = session.trace.total
    assert 7 <= sent <= 13
    await asyncio.sleep(0.1)
    assert session.trace.total == sent
    assert session.periodic == [] and periodic_changes == [1, 0]
    session.stop()


async def test_send_requires_open_session(channel):
    session = live_session(channel)
    with pytest.raises(SessionError):
        session.send(can.Message(arbitration_id=1))


async def test_open_failure_sets_error_state():
    session = BusSession(LiveSource(BusConfig(interface='does-not-exist', channel='x')))
    with pytest.raises(SessionError):
        session.start()
    assert session.state is SessionState.ERROR and session.error


@pytest.mark.parametrize('suffix', ['.asc', '.blf', '.csv', '.log', '.trc'])
async def test_record_and_replay_roundtrip(channel, peer, tmp_path, suffix):
    session = live_session(channel)
    session.start()
    path = tmp_path / f'recording{suffix}'
    session.start_recording(path)
    sent = [can.Message(arbitration_id=0x100 + i, is_extended_id=False, data=bytes([i, i])) for i in range(10)]
    for msg in sent:
        peer.send(msg)
    await wait_until(lambda: session.trace.total == 10)
    session.stop()
    assert not session.recording

    replay = BusSession(FileSource(path, realtime=False))
    replay.start()
    await wait_until(lambda: replay.state is SessionState.FINISHED)
    received = [frame.message for frame in replay.trace]
    assert [(m.arbitration_id, bytes(m.data)) for m in received] == [(m.arbitration_id, bytes(m.data)) for m in sent]


async def test_blf_recording_with_numeric_channel_name(tmp_path):
    channel = 'vcan123456789'
    peer = can.Bus(interface='virtual', channel=channel)
    session = live_session(channel)
    errors = []
    session.add_error_listener(errors.append)
    session.start()
    path = tmp_path / 'recording.blf'
    session.start_recording(path)
    peer.send(can.Message(arbitration_id=0x1, is_extended_id=False))
    await wait_until(lambda: session.trace.total == 1)
    assert session.trace and next(iter(session.trace)).message.channel == channel
    session.stop()
    peer.shutdown()
    assert errors == []
    assert [m.arbitration_id for m in can.LogReader(path)] == [0x1]


async def test_failing_recorder_stops_recording_but_not_session(channel, peer, tmp_path):
    session = live_session(channel)
    errors = []
    session.add_error_listener(errors.append)
    session.start()
    session.start_recording(tmp_path / 'recording.asc')

    class BrokenRecorder(can.Listener):
        def on_message_received(self, msg):
            raise OSError('disk full')

        def stop(self):
            pass

    session._recorder = BrokenRecorder()
    peer.send(can.Message(arbitration_id=0x1, is_extended_id=False))
    peer.send(can.Message(arbitration_id=0x2, is_extended_id=False))
    await wait_until(lambda: session.trace.total == 2)
    assert session.is_open and not session.recording
    assert len(errors) == 1 and 'disk full' in errors[0]
    session.stop()


async def test_replay_reproduces_timing(tmp_path):
    path = tmp_path / 'timed.asc'
    with can.Logger(path) as logger:
        for i in range(3):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=1000.0 + 0.1 * i))
    replay = BusSession(FileSource(path, realtime=True))
    loop = asyncio.get_running_loop()
    started = loop.time()
    replay.start()
    await wait_until(lambda: replay.state is SessionState.FINISHED)
    assert loop.time() - started >= 0.18
    assert not replay.source.can_send


async def test_replay_missing_file(tmp_path):
    session = BusSession(FileSource(tmp_path / 'missing.asc'))
    with pytest.raises(SessionError):
        session.start()


async def test_unsupported_recording_format(channel, tmp_path):
    session = live_session(channel)
    with pytest.raises(SessionError):
        session.start_recording(tmp_path / 'recording.txt')
