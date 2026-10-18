import asyncio
import time
import uuid

import can
import pytest

from can_explorer.core.filters import MessageFilter
from can_explorer.core.playback import LogPlayer, PlaybackClock, scan_log, speed_label
from can_explorer.core.session import BusSession, SessionState
from can_explorer.core.sources import BusConfig, FileSource, LiveSource


def write_log(path, count, interval, rx=True):
    with can.Logger(path) as logger:
        for i in range(count):
            logger.on_message_received(
                can.Message(arbitration_id=0x100 + i, is_rx=rx, data=bytes([i]), timestamp=100.0 + i * interval)
            )
    return path


async def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Timed out'
        await asyncio.sleep(0.005)


def test_speed_label():
    assert [speed_label(speed) for speed in (0.5, 1.0, 10.0, 0.0)] == ['0.5×', '1×', '10×', 'Max']


async def test_clock_speed_and_pause():
    clock = PlaybackClock(speed=2.0)
    clock.start()
    await asyncio.sleep(0.1)
    assert 0.15 < clock.position < 0.35
    clock.pause()
    paused_at = clock.position
    await asyncio.sleep(0.05)
    assert clock.position == paused_at and not clock.is_due(paused_at + 1)
    clock.resume()
    clock.set_speed(0.0)  # As fast as possible: follows the frames
    await clock.wait_until(50.0)
    assert clock.position == 50.0 and clock.is_due(1000.0)


async def test_clock_wait_follows_pause_and_speed_changes():
    clock = PlaybackClock(speed=1.0)
    clock.start()
    waiting = asyncio.ensure_future(clock.wait_until(10.0))
    await asyncio.sleep(0.02)
    clock.pause()
    await asyncio.sleep(0.02)
    assert not waiting.done()
    clock.resume()
    clock.set_speed(1000.0)  # 10 s of recording in 10 ms
    await asyncio.wait_for(waiting, 1.0)


def test_scan_log(tmp_path):
    assert scan_log(write_log(tmp_path / 'a.asc', 11, 0.1)) == (11, pytest.approx(1.0))


async def test_file_source_pause_and_speed(tmp_path):
    path = write_log(tmp_path / 'slow.asc', 20, 0.05)  # 0.95 s
    source = FileSource(path, speed=1.0)
    session = BusSession(source)
    session.start()
    await wait_for(lambda: session.trace.total >= 2)
    source.clock.pause()
    await asyncio.sleep(0.05)
    count = session.trace.total
    await asyncio.sleep(0.15)
    assert session.trace.total == count  # Nothing played while paused
    source.clock.resume()
    source.clock.set_speed(10.0)
    started = time.monotonic()
    await wait_for(lambda: session.state is SessionState.FINISHED)
    assert time.monotonic() - started < 0.5 and session.trace.total == 20
    await wait_for(lambda: source.duration is not None)
    assert source.duration == pytest.approx(0.95)
    session.stop()


async def test_log_player_sends_with_timing_and_filter(tmp_path):
    channel = f'player-{uuid.uuid4().hex}'
    path = write_log(tmp_path / 'log.blf', 6, 0.04)
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    peer = can.Bus(interface='virtual', channel=channel)
    finished = []
    player = LogPlayer(
        session, path, speed=1.0, message_filter=MessageFilter.parse('!101'), on_finished=finished.append
    )
    started = time.monotonic()
    player.start()
    await wait_for(lambda: finished)
    elapsed = time.monotonic() - started
    received = []
    while (msg := peer.recv(timeout=0)) is not None:
        received.append(msg)
    peer.shutdown()
    session.stop()
    assert player.state == LogPlayer.FINISHED and player.sent == 5
    assert [msg.arbitration_id for msg in received] == [0x100, 0x102, 0x103, 0x104, 0x105]
    assert 0.17 < elapsed < 1.0  # 0.2 s of recording
    assert player.duration == pytest.approx(0.2)


async def test_log_player_loops_until_stopped(tmp_path):
    channel = f'player-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    player = LogPlayer(session, write_log(tmp_path / 'log.asc', 3, 0.001), speed=0.0, loop=True)
    player.start()
    await wait_for(lambda: player.iteration >= 3)
    player.clock.pause()
    assert player.state == LogPlayer.PAUSED
    player.stop()
    await wait_for(lambda: not player.running)
    assert player.state == LogPlayer.STOPPED and player.sent >= 9
    session.stop()


async def test_log_player_reports_errors(tmp_path):
    channel = f'player-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    player = LogPlayer(session, write_log(tmp_path / 'log.asc', 50, 0.01), speed=1.0)
    player.start()
    await wait_for(lambda: player.sent > 0)
    session.stop()  # Sending fails from now on
    await wait_for(lambda: not player.running)
    assert player.state == LogPlayer.ERROR and 'not open' in player.error


async def test_file_source_seeking(tmp_path):
    path = write_log(tmp_path / 'long.asc', 100, 0.1)  # 9.9 s
    source = FileSource(path, speed=1.0)
    session = BusSession(source)
    session.start()
    await wait_for(lambda: session.trace.total >= 1)

    # Forward: the frames in between are delivered at once
    assert source.seek(9.0) is False
    await wait_for(lambda: session.trace.total >= 91, timeout=1.0)
    assert source.clock.position == pytest.approx(9.0, abs=0.1)

    # Backward: the file is read again; frames up to the position at once, the rest in time
    session.clear()
    assert source.seek(2.0) is True
    await wait_for(lambda: session.trace.total >= 21, timeout=1.0)
    await asyncio.sleep(0.25)
    assert 21 <= session.trace.total <= 25
    ids = [frame.message.arbitration_id for frame in session.trace]
    assert ids[:21] == [0x100 + i for i in range(21)]  # In order, from the beginning, without duplicates

    # While paused, seeking delivers the frames up to the position and stays paused
    source.clock.pause()
    count = session.trace.total
    source.seek(5.0)
    await wait_for(lambda: session.trace.total >= 51, timeout=1.0)
    await asyncio.sleep(0.2)
    assert session.trace.total == 51 > count and source.clock.paused
    session.stop()


async def test_log_player_seeking_skips_frames(tmp_path):
    channel = f'player-{uuid.uuid4().hex}'
    path = write_log(tmp_path / 'log.asc', 50, 0.1)  # 4.9 s
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    peer = can.Bus(interface='virtual', channel=channel)
    player = LogPlayer(session, path, speed=1.0)
    player.start()
    await wait_for(lambda: player.sent >= 2)
    # Slow runners may send a further frame before the test gets to seek: count what was sent at each jump
    before_forward = player.sent
    player.seek(4.0)  # The frames up to 0x127 are skipped, not sent
    await wait_for(lambda: player.sent >= before_forward + 2)
    before_back = player.sent
    player.seek(0.0)  # Back to the beginning: sent again
    await wait_for(lambda: player.sent >= before_back + 2)
    player.stop()
    await wait_for(lambda: not player.running)
    received = []
    while (msg := peer.recv(timeout=0)) is not None:
        received.append(msg.arbitration_id - 0x100)
    peer.shutdown()
    session.stop()
    assert received[:before_forward] == list(range(before_forward))
    assert all(frame >= 40 for frame in received[before_forward:before_back])  # After the forward jump
    assert received[before_back] == 0  # Started again after jumping back
    assert not any(before_forward <= frame < 40 for frame in received[:before_back])  # Skipped by the jump
