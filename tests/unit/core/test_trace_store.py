import can
import pytest

from can_explorer.core.frames import Frame
from can_explorer.core.trace_store import SEGMENT_FRAMES, SPILL_CHUNK, TraceStore


def frame(index, **kwargs):
    defaults = dict(
        arbitration_id=index % 0x800, is_extended_id=False, data=index.to_bytes(4, 'little'), timestamp=index * 1e-3
    )
    return Frame(can.Message(**{**defaults, **kwargs}), f'info {index}')


def test_memory_only_is_a_ring_buffer():
    store = TraceStore(3)
    for i in range(5):
        store.append(frame(i))
    assert not store.uses_disk and len(store) == 3 and store.first == 2 and store.total == 5
    assert [f.message.arbitration_id for f in store] == [2, 3, 4]
    assert store.get(1) is None and store.get(4).info == 'info 4'


def test_frames_go_to_disk_and_come_back_exactly(tmp_path):
    store = TraceStore(1000, 10 * SPILL_CHUNK, tmp_path)
    special = [
        frame(0, arbitration_id=0x18FEF100, is_extended_id=True, is_rx=False),
        frame(1, is_fd=True, bitrate_switch=True, error_state_indicator=True, data=bytes(range(64))),
        frame(2, is_remote_frame=True, dlc=8, data=b''),
        frame(3, is_error_frame=True, data=b''),
    ]
    for item in special:
        store.append(item)
    for i in range(4, 3 * SPILL_CHUNK):
        store.append(frame(i))
    assert store.disk_frames > 0
    for original in special:
        index = special.index(original)
        restored = store.get(index)
        assert restored.info == original.info
        assert restored.message.equals(original.message, timestamp_delta=None, check_channel=False)
        assert restored.message.timestamp == original.message.timestamp
    assert [f.info for f in store.iter_from(store.total - 3)] == [
        f'info {i}' for i in range(store.total - 3, store.total)
    ]
    assert sum(1 for _ in store) == len(store) == store.total


def test_history_limit_deletes_old_files(tmp_path):
    store = TraceStore(SPILL_CHUNK, SEGMENT_FRAMES, tmp_path)
    for i in range(3 * SEGMENT_FRAMES):
        store.append(frame(i))
    assert len(store) <= SEGMENT_FRAMES + SPILL_CHUNK and store.first >= 2 * SEGMENT_FRAMES - SPILL_CHUNK
    assert len(store._segments) <= 2  # Files of dropped frames are closed (and so deleted)
    assert store.get(store.first).info == f'info {store.first}'
    store.clear()
    assert len(store) == 0 and not store._segments


def test_disk_errors_fall_back_to_memory(tmp_path):
    errors = []
    blocked = tmp_path / 'file'
    blocked.write_text('not a directory')
    store = TraceStore(10, 1000 * SPILL_CHUNK, blocked / 'trace', on_error=errors.append)
    for i in range(SPILL_CHUNK + 20):
        store.append(frame(i))
    assert len(errors) == 1 and 'turned off' in errors[0]
    assert not store.uses_disk and len(store) == 10 and store.get(store.total - 1).info == f'info {store.total - 1}'


@pytest.mark.parametrize('memory', [1, 7])
def test_drain_returns_new_frames(memory):
    store = TraceStore(memory)
    store.append(frame(0))
    assert [f.info for f in store.drain()] == ['info 0'] and store.drain() == []
