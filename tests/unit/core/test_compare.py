import can
import pytest

from can_explorer.core.compare import (
    ALIGN_ID,
    ALIGN_SEQUENCE,
    ALIGN_START,
    compare,
    read_recording,
    summary_csv,
)
from can_explorer.core.decoding import open_signal_database


def frame(time, can_id, data, extended=False):
    return can.Message(timestamp=time, arbitration_id=can_id, is_extended_id=extended, data=bytes(data))


def engine(speed_raw):
    return [speed_raw & 0xFF, speed_raw >> 8, 90, 3, 0, 0, 0, 0]  # EngineData: speed (0.25 rpm), 50 °C, gear 3


def recording_a(start=100.0):
    frames = []
    for i in range(50):
        t = start + i * 0.01
        frames.append(frame(t, 0x100, engine(4000 + 40 * i)))
        if i % 2 == 0:
            frames.append(frame(t + 0.001, 0x200, [(i // 2) % 2, 1, 2]))
        if i % 10 == 0:
            frames.append(frame(t + 0.002, 0x300, [7]))
    return read_recording('a.asc', frames)


def recording_b(start=500.0):
    frames = [frame(start - 0.5, 0x7DF, [2, 1, 0])]  # A diagnostic request before the traffic
    for i in range(50):
        t = start + i * 0.01
        speed = 4000 + 40 * i if i < 30 else 6000  # Speed stuck from frame 30 on
        frames.append(frame(t, 0x100, engine(speed)))
        if i % 3 == 0:
            frames.append(frame(t + 0.001, 0x200, [0, 1, 2]))  # Every 30 ms instead of 20 ms
    frames.append(frame(start + 0.6, 0x18FEF100, [0xFF] * 8, extended=True))
    return read_recording('b.asc', frames)


def test_identifiers_by_start(data_dir):
    a, b = recording_a(), recording_b()
    assert (a.frames, round(a.duration, 3), b.traces[(0x100, False)].count) == (80, 0.49, 50)
    comparison = compare(a, b, ALIGN_START)
    by_id = {difference.key: difference for difference in comparison.ids}
    assert comparison.counts() == {'only_a': 1, 'only_b': 2, 'changed': 2, 'same': 0}
    assert by_id[(0x300, False)].status == 'only_a' and by_id[(0x18FEF100, True)].status == 'only_b'
    engine_data = by_id[(0x100, False)]
    # B starts with the diagnostic request, 0.5 s earlier: aligned by the start nothing pairs
    assert engine_data.paired == 0 and 'frames without counterpart' in ', '.join(engine_data.reasons)

    comparison = compare(a, b, ALIGN_ID, (0x100, False))
    by_id = {difference.key: difference for difference in comparison.ids}
    engine_data = by_id[(0x100, False)]
    assert (engine_data.paired, engine_data.differing) == (50, 20)
    assert engine_data.first_difference[0] == pytest.approx(0.30)
    assert engine_data.first_difference[1:] == (bytes(engine(4000 + 40 * 30)), bytes(engine(6000)))
    assert engine_data.reasons == ['20 of 50 frames differ']
    cyclic = by_id[(0x200, False)]
    assert round(cyclic.cycle_a, 3) == 0.02 and round(cyclic.cycle_b, 3) == 0.03
    assert cyclic.reasons[0] == 'cycle time +50%' and 'other bits change' in cyclic.reasons
    assert summary_csv(comparison).splitlines()[1].startswith('100;changed;50;50;10.000;10.000;50;20;0.300;')

    missing = compare(a, b, ALIGN_ID, (0x555, False))
    assert missing.align == ALIGN_START and missing.align_error.startswith('555 is not in recording A and B')


def test_sequence_alignment_and_signals(data_dir):
    database = open_signal_database(data_dir / 'example.dbc')
    comparison = compare(recording_a(), recording_b(), ALIGN_SEQUENCE, database=database)
    engine_data = next(difference for difference in comparison.ids if difference.key == (0x100, False))
    assert engine_data.paired == 50 and engine_data.differing == 20
    speed = next(signal for signal in comparison.signals if signal.key == 'EngineData.EngineSpeed')
    assert speed.unit == 'rpm' and speed.changed and comparison.signals[0] is speed  # Changed signals first
    assert (speed.min_a, speed.max_a, speed.max_b) == (1000, 1490, 1500)
    assert (speed.paired, speed.differing) == (50, 20) and speed.max_difference == 1500 - 1300
    assert speed.first_difference == (pytest.approx(0.30), 1300, 1500)
    temperature = next(signal for signal in comparison.signals if signal.key == 'EngineData.CoolantTemp')
    assert not temperature.changed and temperature.mean_a == temperature.mean_b == 50


def test_log_files(tmp_path):
    for name, start in (('a.asc', 0.0), ('b.blf', 10.0)):
        with can.Logger(str(tmp_path / name)) as logger:
            for i in range(20):
                logger.on_message_received(frame(start + i * 0.1, 0x123, [i]))
    comparison = compare(read_recording(tmp_path / 'a.asc'), read_recording(tmp_path / 'b.blf'))
    assert [(difference.status, difference.paired) for difference in comparison.ids] == [('same', 20)]
    with pytest.raises(InterruptedError):
        read_recording(tmp_path / 'a.asc', stop=lambda: True)
