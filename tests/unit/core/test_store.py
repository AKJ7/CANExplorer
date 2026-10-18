import can

from can_explorer.core.frames import Frame
from can_explorer.core.store import IdStatistics, TraceBuffer


def frame(can_id, data, timestamp=0.0, rx=True):
    return Frame(can.Message(arbitration_id=can_id, is_extended_id=False, data=data, timestamp=timestamp, is_rx=rx))


def test_trace_buffer_is_bounded_and_drains():
    buffer = TraceBuffer(maxlen=3)
    for i in range(5):
        buffer.append(frame(i, b''))
    assert [f.message.arbitration_id for f in buffer] == [2, 3, 4]
    assert buffer.total == 5
    assert [f.message.arbitration_id for f in buffer.drain()] == [2, 3, 4]
    assert buffer.drain() == []


def test_statistics_track_count_cycle_and_changes():
    statistics = IdStatistics()
    statistics.update(frame(0x10, b'\x01\x02', timestamp=1.0))
    assert statistics.drain_changes() == (True, {(0x10, False, True)})
    statistics.update(frame(0x10, b'\x01\x03\x04', timestamp=1.25))
    added, changed = statistics.drain_changes()
    assert not added and changed == {(0x10, False, True)}
    entry = statistics.entries[(0x10, False, True)]
    assert entry.count == 2
    assert entry.cycle_time == 0.25
    assert entry.changed == (False, True, True)


def test_statistics_separate_directions():
    statistics = IdStatistics()
    statistics.update(frame(0x10, b'', rx=True))
    statistics.update(frame(0x10, b'', rx=False))
    assert len(statistics.entries) == 2
