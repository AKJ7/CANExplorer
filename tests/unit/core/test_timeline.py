import pytest

from can_explorer.core.reverse.timeline import IdTimeline


def test_empty_timeline():
    timeline = IdTimeline([], [])
    assert len(timeline) == 0 and timeline.duration == 0.0 and timeline.length == 0
    assert timeline.index_at(5.0) == -1 and timeline.payload_at(0) == b''


def test_scrubbing_finds_the_frame_at_a_time():
    times = [10.0, 10.1, 10.2, 10.5]
    payloads = [b'\x00', b'\x01', b'\x03', b'\x02']
    timeline = IdTimeline(times, payloads)
    assert (timeline.start, timeline.end, round(timeline.duration, 3)) == (10.0, 10.5, 0.5)
    assert timeline.index_at(9.0) == 0  # Before the first frame: clamped to it
    assert timeline.index_at(10.0) == 0
    assert timeline.index_at(10.15) == 1  # Holds the last value until the next frame
    assert timeline.index_at(10.2) == 2
    assert timeline.index_at(99.0) == 3  # After the last frame: clamped to it
    assert timeline.payload_at(2) == b'\x03' and timeline.time_at(2) == 10.2


def test_bit_values_and_changes():
    # bit 0 toggles, bit 1 set at frame 2, bit 2 set at frame 3
    payloads = [b'\x00', b'\x01', b'\x03', b'\x07', b'\x06']
    times = [float(i) for i in range(5)]
    timeline = IdTimeline(times, payloads)
    assert [timeline.bit_value(i, 0) for i in range(5)] == [0, 1, 1, 1, 0]
    assert timeline.bit_value(2, 1) == 1 and timeline.bit_value(1, 1) == 0
    # Bits that changed into each frame
    assert timeline.changed_bits(0) == []
    assert timeline.changed_bits(1) == [0]
    assert timeline.changed_bits(2) == [1]
    assert timeline.changed_bits(3) == [2]
    assert timeline.changed_bits(4) == [0]  # bit 0 cleared
    # Last change time of a bit at or before an index
    assert timeline.last_change_time(2, 0) == 1.0  # bit 0 last changed entering frame 1
    assert timeline.last_change_time(4, 0) == 4.0
    assert timeline.last_change_time(1, 2) is None  # bit 2 has not changed yet by frame 1
    assert timeline.last_change_time(3, 2) == 3.0


def test_variable_length_and_out_of_range():
    timeline = IdTimeline([0.0, 1.0], [b'\xff', b'\xff\x01'])
    assert timeline.length == 2
    assert timeline.bit_value(1, 8) == 1  # Second byte exists in frame 1
    assert timeline.bit_value(0, 8) == 0  # Frame 0 is one byte: bit 8 reads 0
    assert timeline.bit_value(5, 0) == 0  # Out-of-range index
    assert timeline.last_change_time(0, 99) is None

    with pytest.raises(ValueError):
        IdTimeline([0.0], [b'', b''])
