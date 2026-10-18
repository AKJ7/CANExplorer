import asyncio
import uuid

import can
import pytest

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.recording import Recorder, Trigger, TriggeredRecorder, numbered_path
from can_explorer.core.session import BusSession, SessionError
from can_explorer.core.sources import BusConfig, LiveSource


def message(can_id, timestamp, data=b'\x00'):
    return can.Message(arbitration_id=can_id, is_extended_id=False, timestamp=timestamp, data=data)


def read(path):
    with can.LogReader(path) as reader:
        return [(msg.arbitration_id, round(msg.timestamp, 3)) for msg in reader]


def test_numbered_paths(tmp_path):
    assert numbered_path(tmp_path / 'trace.asc.gz', 3).name == 'trace_003.asc.gz'
    assert numbered_path(tmp_path / 'run.blf', 12).name == 'run_012.blf'


def test_split_by_duration(tmp_path):
    recorder = Recorder(tmp_path / 'trace.asc', split_seconds=1.0)
    for i in range(25):
        recorder.on_message_received(message(i, 100 + i * 0.1))
    recorder.stop()
    assert [path.name for path in recorder.files] == ['trace_001.asc', 'trace_002.asc', 'trace_003.asc']
    assert [len(read(path)) for path in recorder.files] == [10, 10, 5]


def test_split_by_size(tmp_path):
    recorder = Recorder(tmp_path / 'trace.log', split_bytes=20_000)
    for i in range(3000):
        recorder.on_message_received(message(i % 0x800, i * 0.001, bytes(8)))
    recorder.stop()
    assert len(recorder.files) >= 2
    assert sum(len(read(path)) for path in recorder.files) == 3000


def test_triggered_with_pre_and_post_time(tmp_path):
    database = open_signal_database('examples/simulation/vehicle.dbc')
    definition = database.messages()[0]  # EngineData
    events = []
    trigger = Trigger(start='EngineData.EngineSpeed > 6000', pre=0.5, post=0.3)
    recorder = TriggeredRecorder(tmp_path / 'event.asc', trigger, lambda: database, on_event=events.append)
    for i in range(40):
        speed = 7000 if i == 20 else 3000
        data = definition.encode({'EngineSpeed': speed, 'CoolantTemp': 80, 'GearPosition': 'First'})
        recorder.on_message_received(
            can.Message(arbitration_id=definition.frame_id, is_extended_id=False, timestamp=10 + i * 0.1, data=data)
        )
    recorder.stop()
    assert recorder.state == 'done' and recorder.events == 1 and 'Trigger' in events[0]
    times = [timestamp for _, timestamp in read(tmp_path / 'event.asc')]  # ASC stores times from the first frame
    assert len(times) == 9 and times[-1] - times[0] == pytest.approx(0.8)  # 11.5 to 12.3: 0.5 s before, 0.3 s after


def test_triggered_start_stop_and_repeat(tmp_path):
    trigger = Trigger(start='100 data=01', stop='100 data=02', pre=0.0, post=0.0, repeat=True)
    recorder = TriggeredRecorder(tmp_path / 'run.asc', trigger, lambda: None)
    sequence = [0, 1, 0, 0, 2, 0, 1, 2, 0]  # Two start/stop pairs
    for i, value in enumerate(sequence):
        recorder.on_message_received(message(0x100, i, bytes([value])))
    recorder.on_message_received(message(0x200, 20))
    recorder.stop()
    assert recorder.state == 'armed' and recorder.events == 2
    assert [path.name for path in recorder.files] == ['run_001.asc', 'run_002.asc']
    assert [len(read(path)) for path in recorder.files] == [4, 2]


async def test_session_recording_options(tmp_path):
    channel = f'record-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    with pytest.raises(SessionError, match='Invalid trigger'):
        session.start_recording(tmp_path / 'a.asc', trigger=Trigger(start='EngineData.EngineSpeed >'))
    with pytest.raises(SessionError, match='Could not record'):
        session.start_recording(tmp_path / 'missing' / 'a.asc', trigger=Trigger(start='100'))
    events = []
    session.start_recording(tmp_path / 'b.asc', trigger=Trigger(start='123', pre=1, post=0), on_event=events.append)
    peer = can.Bus(interface='virtual', channel=channel)
    for can_id in (0x100, 0x101, 0x123, 0x102):
        peer.send(message(can_id, 0))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    peer.shutdown()
    assert session.recorder.state == 'done' and events
    session.stop()
    assert [can_id for can_id, _ in read(tmp_path / 'b.asc')] == [0x100, 0x101, 0x123]  # 1 s before, 0 s after
