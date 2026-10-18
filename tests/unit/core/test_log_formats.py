import asyncio
import time
import uuid

import can
import pytest

from can_explorer.core import log_formats
from can_explorer.core.frames import Frame
from can_explorer.core.log_formats import LOG_FILE_SUFFIXES, file_filters, is_log_file, log_suffix
from can_explorer.core.playback import LogPlayer
from can_explorer.core.session import BusSession, SessionError, SessionState
from can_explorer.core.sources import BusConfig, FileSource, LiveSource


def frames():
    return [
        can.Message(arbitration_id=0x123, is_extended_id=False, data=bytes([1, 2, 3])),
        can.Message(arbitration_id=0x18FEF100, is_extended_id=True, data=bytes(range(8))),
        can.Message(arbitration_id=0x7FF, is_extended_id=False, dlc=0, data=b''),
    ]


async def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Timed out'
        await asyncio.sleep(0.01)


def test_suffixes():
    assert log_suffix('/tmp/Trace.ASC.GZ') == '.asc.gz' and log_suffix('a.blf') == '.blf'
    assert log_suffix('a.blf.gz') == '.blf.gz' and not is_log_file('a.blf.gz')  # python-can cannot compress BLF
    assert not is_log_file('notes.txt') and is_log_file('run.trc.gz')
    assert '.mf4' in LOG_FILE_SUFFIXES  # asammdf is installed for the tests
    filters = file_filters()
    assert filters[0].startswith('CAN log files (*.asc ') and 'ASAM MDF 4 (*.mf4)' in filters
    assert 'Vector ASCII, gzip compressed (*.asc.gz)' in filters


def test_mf4_needs_asammdf(monkeypatch):
    monkeypatch.setattr(log_formats, 'mf4_available', lambda: False)
    assert '.mf4' not in log_formats._suffixes()
    monkeypatch.setattr(log_formats, 'LOG_FILE_SUFFIXES', log_formats._suffixes())
    assert 'asammdf' in log_formats.unsupported_message('run.mf4')


@pytest.mark.parametrize('suffix', LOG_FILE_SUFFIXES)
async def test_record_then_replay(tmp_path, suffix):
    """Every format that can be recorded can be replayed, onto the views and onto a bus."""
    channel = f'formats-{uuid.uuid4().hex}'
    path = tmp_path / f'recording{suffix}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    session.start_recording(path)
    peer = can.Bus(interface='virtual', channel=channel)
    for msg in frames():
        peer.send(msg)
    await wait_for(lambda: session.trace.total == 3)
    session.stop()  # Also ends the recording
    peer.shutdown()

    def key(msg):
        return msg.arbitration_id, msg.is_extended_id, bytes(msg.data)

    replay = BusSession(FileSource(path, speed=0.0))
    replay.start()
    await wait_for(lambda: replay.state is SessionState.FINISHED)
    assert [key(frame.message) for frame in replay.trace] == [key(msg) for msg in frames()]

    exported = tmp_path / f'export{suffix}'
    assert BusSession.export(list(replay.trace)[1:], exported) == 2
    with can.LogReader(exported) as reader:
        assert [key(msg) for msg in reader] == [key(msg) for msg in frames()[1:]]

    bus = BusSession(LiveSource(BusConfig('virtual', f'{channel}-out')))
    bus.start()
    player = LogPlayer(bus, path, speed=0.0)
    player.start()
    await wait_for(lambda: not player.running)
    bus.stop()
    assert player.state == LogPlayer.FINISHED and player.sent == 3


def test_unsupported_formats_are_rejected(tmp_path):
    session = BusSession(LiveSource(BusConfig('virtual', f'formats-{uuid.uuid4().hex}')))
    for name in ('trace.txt', 'trace.blf.gz'):
        with pytest.raises(SessionError, match='Unsupported log format'):
            session.start_recording(tmp_path / name)
        with pytest.raises(SessionError):
            BusSession.export([Frame(frames()[0])], tmp_path / name)
