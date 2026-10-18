import can
import pytest

from can_explorer.core.decoding import DbcDecoder
from can_explorer.core.signals import SignalSeries, SignalTracker


def engine(speed_raw, timestamp):
    data = bytes([speed_raw & 0xFF, speed_raw >> 8, 90, 1, 0, 0, 0, 0])
    return can.Message(arbitration_id=0x100, is_extended_id=False, data=data, timestamp=timestamp)


def test_tracks_only_selected_signals(data_dir):
    database = DbcDecoder(data_dir / 'example.dbc')
    tracker = SignalTracker()
    key = tracker.track('EngineData', 'EngineSpeed', 'rpm')
    for i in range(5):
        tracker.update(engine(4 * i, float(i)), database)
    tracker.update(can.Message(arbitration_id=0x555, is_extended_id=False, data=bytes(8)), database)
    assert tracker.tracked == [key] == ['EngineData.EngineSpeed']
    assert tracker.series[key].values == [0, 1, 2, 3, 4]
    assert tracker.series[key].unit == 'rpm'
    assert tracker.drain_changed() and not tracker.drain_changed()
    tracker.untrack('EngineData', 'EngineSpeed')
    assert tracker.tracked == []


def test_enumerated_signals_are_plotted_numerically(data_dir):
    database = DbcDecoder(data_dir / 'example.dbc')
    tracker = SignalTracker()
    key = tracker.track('EngineData', 'GearPosition')
    tracker.update(engine(0, 1.0), database)
    assert tracker.series[key].values == [1.0]


def test_series_window_and_trimming():
    series = SignalSeries(max_points=10)
    for i in range(25):
        series.append(float(i), float(i))
    assert len(series.times) <= 20 and series.times[-1] == 24
    times, values = series.window(20.5, 24)
    assert times == [20.0, 21.0, 22.0, 23.0, 24.0]
    assert series.last == (24.0, 24.0)


@pytest.mark.parametrize('start, end, expected', [(0, 1, [0.0, 1.0]), (100, 200, [24.0])])
def test_series_window_edges(start, end, expected):
    series = SignalSeries(max_points=100)
    for i in range(25):
        series.append(float(i), 0.0)
    assert series.window(start, end)[0] == expected
