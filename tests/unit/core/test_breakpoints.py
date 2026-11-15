"""Breakpoints and single stepping of playbacks: the replay of a recording and a recording transmitted on a bus."""

import asyncio
import uuid
from pathlib import Path

import can
import pytest

from can_explorer.core.breakpoints import Breakpoint, BreakpointSet, parse_data_pattern
from can_explorer.core.decoding import DbcDecoder
from can_explorer.core.playback import LogPlayer
from can_explorer.core.session import BusSession, SessionState
from can_explorer.core.sources import BusConfig, FileSource, LiveSource
from tests.unit.core.test_playback import wait_for

DATA = Path(__file__).resolve().parents[2] / 'data'


def write_log(path, frames):
    """frames: (time, identifier, data) on the recording's clock."""
    with can.Logger(path) as logger:
        for time, identifier, data in frames:
            logger.on_message_received(can.Message(arbitration_id=identifier, data=bytes(data), timestamp=100 + time))
    return path


def _msg(identifier, data=b'', extended=False):
    return can.Message(arbitration_id=identifier, data=bytes(data), is_extended_id=extended)


def test_data_patterns():
    assert parse_data_pattern('02 10 ??') == ((0x02, 0xFF), (0x10, 0xFF), (0, 0))
    assert parse_data_pattern('0x1?') == ((0x10, 0xF0),)
    for bad in ('1', '0G', '12 3'):
        with pytest.raises(ValueError):
            parse_data_pattern(bad)


def test_frame_breakpoints():
    breakpoint = Breakpoint.frame('7DF', '02 1? ??')
    assert breakpoint.label == '7DF data 02 1? ??'
    assert breakpoint.check(_msg(0x7DF, [2, 0x10, 3]), 0, None)
    assert not breakpoint.check(_msg(0x7DF, [2, 0x20, 3]), 0, None)  # Data differs
    assert not breakpoint.check(_msg(0x7DF, [2, 0x10]), 0, None)  # Too short
    assert not breakpoint.check(_msg(0x7E8, [2, 0x10, 3]), 0, None)  # Another identifier
    assert breakpoint.hit_count == 1
    every_third = Breakpoint.frame('100-1FF ext', skip=2)
    hits = [every_third.check(_msg(0x150, extended=True), 0, None) for _ in range(4)]
    assert hits == [False, False, True, True] and every_third.hit_count == 4  # Passes its first two hits
    assert not every_third.check(_msg(0x150), 0, None)  # Not extended
    with pytest.raises(ValueError, match='Give the identifiers'):
        Breakpoint.frame('', '')
    with pytest.raises(ValueError, match='Invalid filter term'):
        Breakpoint.frame('xyz')


def test_time_breakpoints_rearm_after_seeking_back():
    at = Breakpoint.at(1.5)
    assert [at.check(_msg(1), position, None) for position in (1.0, 1.5, 1.6)] == [False, True, False]
    at.rearm(0.5)
    assert at.check(_msg(1), 2.0, None)  # First frame after the time again
    at.rearm(3.0)
    assert not at.check(_msg(1), 3.5, None)  # Sought past it
    with pytest.raises(ValueError):
        Breakpoint.at(-1)


def test_signal_conditions():
    database = DbcDecoder(DATA / 'example.dbc')
    message = database.messages()[0]
    signal = next(s for s in message.signals if s.length >= 8 and not s.choices and not s.multiplexer_ids)

    def frame(value):
        data = message.encode({s.name: 0 for s in message.signals} | {signal.name: value}, scaling=False, strict=False)
        return can.Message(arbitration_id=message.frame_id, is_extended_id=message.is_extended_frame, data=data)

    raw = signal.scale * 10 + signal.offset  # The physical value of raw 10
    above = Breakpoint.condition(f'{message.name}.{signal.name} > {raw}')
    hits = [above.check(frame(value), 0, database) for value in (5, 11, 12, 3, 20)]
    assert hits == [False, True, False, False, True]  # When it becomes true, not while it stays true
    changes = Breakpoint.condition(f'{message.name}.{signal.name} changes')
    assert [changes.check(frame(value), 0, database) for value in (1, 1, 2, 2, 1)] == [False, False, True, False, True]
    assert not above.check(_msg(0x7FF), 0, database)  # Another message
    assert not Breakpoint.condition(f'{message.name}.{signal.name} == 1').check(frame(1), 0, None)  # No database
    assert Breakpoint.condition('A.B = On').value == 'On' and Breakpoint.condition('A.B = On').op == '=='
    for bad in ('EngineSpeed > 3', 'A.B >', 'A.B < On', 'A.B changes 3'):
        with pytest.raises(ValueError):
            Breakpoint.condition(bad)


def test_breakpoint_set():
    breakpoints = BreakpointSet()
    breakpoints.add(Breakpoint.frame('101'))
    breakpoints.add(Breakpoint.frame('101'))  # Both count the hit; the first one is reported
    assert breakpoints.check(_msg(0x100), 0.1) is None
    hit = breakpoints.check(_msg(0x101, b'\x01'), 0.2)
    assert hit.breakpoint is breakpoints.items[0] and [row['hits'] for row in breakpoints.rows()] == [1, 1]
    assert hit.text == 'Breakpoint 101 — 0.200000 s: 101 01'
    step = breakpoints.check(_msg(0x100), 0.3, stepping=True)
    assert step.breakpoint is None and step.text.startswith('Step')
    breakpoints.seeked(5.0)
    assert breakpoints.check(_msg(0x101), 4.0) is None  # Skipped over by seeking
    breakpoints.set_enabled(0, False)
    breakpoints.set_enabled(1, False)
    assert not breakpoints.active and breakpoints.check(_msg(0x101), 6.0) is None
    breakpoints.remove(0)
    assert len(breakpoints.items) == 1
    breakpoints.restart()
    assert breakpoints.items[0].hit_count == 0 and breakpoints.last_hit is None


FRAMES = [(i * 0.01, 0x100 + i % 4, [i]) for i in range(40)]  # 0.39 s


@pytest.mark.parametrize('speed', [1.0, 0.0])
async def test_replay_pauses_at_breakpoints_and_steps(tmp_path, speed):
    source = FileSource(write_log(tmp_path / 'log.blf', FRAMES), speed=speed)
    source.breakpoints.add(Breakpoint.frame('103', '07'))  # The 8th frame
    session = BusSession(source)
    session.start()
    await wait_for(lambda: source.clock.paused)
    await asyncio.sleep(0.05)
    assert session.trace.total == 8 and source.breakpoints.last_hit.msg.data == b'\x07'
    assert source.clock.position == pytest.approx(0.07)
    source.clock.step()
    await wait_for(lambda: session.trace.total == 9)
    await asyncio.sleep(0.05)
    assert session.trace.total == 9 and source.clock.paused  # Exactly one frame
    assert source.breakpoints.last_hit.breakpoint is None and source.breakpoints.last_hit.msg.data == b'\x08'
    source.breakpoints.add(Breakpoint.at(0.2))
    source.clock.resume()
    await wait_for(lambda: session.trace.total == 21 and source.clock.paused)
    assert source.breakpoints.last_hit.msg.data == bytes([20])
    source.clock.resume()
    await wait_for(lambda: session.state is SessionState.FINISHED)
    assert session.trace.total == 40
    session.stop()


async def test_seeking_while_stopped_at_a_breakpoint(tmp_path):
    source = FileSource(write_log(tmp_path / 'log.asc', FRAMES), speed=1.0)
    source.breakpoints.add(Breakpoint.frame('101'))
    session = BusSession(source)
    session.start()
    await wait_for(lambda: source.clock.paused)
    assert session.trace.total == 2
    source.breakpoints.set_enabled(0, False)
    source.seek(0.3)  # Forward while held: the frames in between come at once, no breakpoint hits them
    source.clock.resume()
    await wait_for(lambda: session.state is SessionState.FINISHED)
    assert session.trace.total == 40
    session.stop()


async def test_bus_replay_pauses_at_breakpoints(tmp_path):
    channel = f'bp-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    peer = can.Bus(interface='virtual', channel=channel)
    breakpoints = BreakpointSet()
    breakpoints.add(Breakpoint.frame('102'))
    player = LogPlayer(session, write_log(tmp_path / 'log.blf', FRAMES[:10]), speed=0.0, breakpoints=breakpoints)
    player.start()
    await wait_for(lambda: player.clock.paused)
    await asyncio.sleep(0.05)
    assert player.sent == 3 and player.state == LogPlayer.PAUSED  # Paused right after sending the hit
    player.clock.step()
    await wait_for(lambda: player.sent == 4)
    await asyncio.sleep(0.05)
    assert player.sent == 4
    player.clock.resume()
    await wait_for(lambda: player.clock.paused and player.sent == 7)  # 0x102 again (frame 6)
    breakpoints.clear()
    player.clock.resume()
    await wait_for(lambda: player.state == LogPlayer.FINISHED)
    received = []
    while (msg := peer.recv(timeout=0)) is not None:
        received.append(msg.data[0])
    assert received == list(range(10))
    peer.shutdown()
    session.stop()
