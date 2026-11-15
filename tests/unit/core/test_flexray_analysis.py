"""Checking FlexRay traffic against the schedule, the bus health and the channel comparison; signal series."""

import dataclasses
from pathlib import Path

import pytest

from can_explorer.core.flexray import FlexRayEvent, FlexRayFrame
from can_explorer.core.flexray_analysis import FlexRayAnalyzer, SignalRecorder, analyse
from can_explorer.core.flexray_database import ClusterSimulator, load_database

DATA = Path(__file__).resolve().parents[2] / 'data'


@pytest.fixture
def database():
    return load_database(DATA / 'flexray_pdus.arxml')


@pytest.fixture
def traffic(database):
    return ClusterSimulator(database, start=0.0).frames_until(0.050)  # 10 cycles


def _issues(report):
    return {(issue['slot'], issue['channel'], issue['kind']): issue for issue in report['issues']}


def test_the_simulated_schedule_is_clean(database, traffic):
    report = analyse(traffic, database)
    assert report['issues'] == []
    a = report['channels']['A']
    assert (a['frames'], a['cycles'], a['cycle_jumps'], a['null'], a['with_errors']) == (24, 10, 0, 0, 0)
    assert (a['sync_min'], a['sync_max'], a['few_sync_cycles']) == (1, 1, 10)  # One sync node only
    assert a['cycle_time'] == pytest.approx(0.005)
    assert report['timing']['checked'] > 0 and report['timing']['max_deviation'] < 1e-9
    assert report['channel_comparison'] == {'compared': 10, 'mismatches': 0}  # Slot 12 on both channels
    assert report['dynamic_frames'] == 10  # Slot 60, above the 50 static slots


def test_schedule_problems_are_found(database, traffic):
    frames = [f for f in traffic if not (f.slot == 40 and f.cycle == 5)]  # Lost
    frames = [
        dataclasses.replace(f, payload=b'\xff' * 8) if (f.slot == 12 and f.channel == 'B' and f.cycle == 3) else f
        for f in frames
    ]  # The B copy differs
    frames = [
        dataclasses.replace(f, timestamp=f.timestamp + 0.0004)
        if (f.slot == 12 and f.channel == 'A' and f.cycle == 6)
        else f
        for f in frames
    ]  # 400 µs late
    frames = [
        dataclasses.replace(f, payload=b'\x00' * 6) if (f.slot == 40 and f.cycle == 9) else f for f in frames
    ]  # Wrong length
    frames.append(FlexRayFrame(0.020 + 32 * 50e-6, 'A', 33, 4, b'\x01\x02' * 4))  # Not described
    frames.append(FlexRayFrame(0.025 + 49 * 50e-6, 'A', 50, 5, b'\x01\x02' * 4))  # FrStatus is only in cycle 3
    frames.sort(key=lambda f: f.timestamp)
    issues = _issues(analyse(frames, database))
    assert issues[(40, 'A', 'missing')]['cycle'] == 5
    assert issues[(12, 'A+B', 'channel mismatch')]['cycle'] == 3
    assert issues[(12, 'A', 'timing')]['detail'] == '+400 µs from its slot'
    assert (33, 'A', 'not described') in issues and (50, 'A', 'not described') in issues
    assert issues[(40, 'A', 'wrong length')]['detail'] == '6 bytes, 8 expected'
    assert issues[(40, 'A', 'static payload length')]['detail'] == '6 bytes, the cluster sends 8'


def test_cycle_jumps_dynamic_order_errors_and_events():
    analyzer = FlexRayAnalyzer(load_database(DATA / 'flexray_pdus.arxml'))
    frames = [
        FlexRayFrame(0.000, 'A', 12, 0, bytes(8), sync=True),
        FlexRayFrame(0.001, 'A', 62, 0, bytes(2)),
        FlexRayFrame(0.002, 'A', 61, 0, bytes(2)),  # Dynamic slots must ascend
        FlexRayFrame(0.020, 'A', 12, 4, bytes(8), sync=True, errors=['frame CRC']),  # Cycles 1–3 are missing
        FlexRayFrame(0.025, 'A', 12, 5, bytes(8), null_frame=True),
    ]
    analyzer.add(frames)
    analyzer.flush()
    analyzer.add_events([FlexRayEvent(0.03, 'symbol', 'CAS', 'A'), FlexRayEvent(0.04, 'symbol', 'CAS', 'A')])
    report = analyzer.report()
    a = report['channels']['A']
    assert (a['cycle_jumps'], a['skipped_cycles'], a['null'], a['with_errors'], a['errors']) == (
        1,
        3,
        1,
        1,
        {'frame CRC': 1},
    )
    assert a['sync_min'] == 0  # A frame with errors is no valid sync frame; the null frame carries no sync bit
    assert _issues(report)[(61, 'A', 'dynamic slot order')]['detail'] == 'after slot 62'
    assert {'kind': 'symbol', 'text': 'CAS', 'count': 2} in report['events']


def test_without_a_description_only_health_and_channels(traffic):
    report = analyse(traffic)
    assert report['issues'] == [] and report['channels']['A']['frames'] == 24
    assert report['channel_comparison']['compared'] == 10 and report['timing']['checked'] == 0


def test_signal_recorder(database, traffic):
    recorder = SignalRecorder()
    key = recorder.track('FrEngine', 'EngineSpeed', 'rpm')
    recorder.track('FrEngine', 'GearPosition')
    assert key == 'FrEngine.EngineSpeed' and recorder.tracked == ['FrEngine.EngineSpeed', 'FrEngine.GearPosition']
    recorder.update(traffic, database)
    assert recorder.drain_changed() and not recorder.drain_changed()
    assert len(recorder.series[key].times) == 20  # Both channels, 10 cycles
    gears = recorder.series['FrEngine.GearPosition'].values
    assert gears and all(isinstance(value, float) for value in gears)  # Value texts plotted by their number
    recorder.untrack_key(key)
    assert recorder.tracked == ['FrEngine.GearPosition']
    recorder.untrack_all()
    assert recorder.tracked == []
