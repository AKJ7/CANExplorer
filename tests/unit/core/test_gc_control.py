import gc
import uuid

import pytest

from can_explorer.core.gc_control import SUSPENDED_THRESHOLD, CaptureGc, capture_gc
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource


class FakeTrace:
    def __init__(self):
        self.total = 0


class FakeSession:
    def __init__(self):
        self.trace = FakeTrace()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def controller():
    clock = Clock()
    collected = []
    gc_control = CaptureGc(clock=clock, collect=lambda: collected.append(clock.now) or 0)
    gc_control.clock, gc_control.collected = clock, collected
    yield gc_control
    gc_control.release()


async def test_suspends_full_collections_while_a_session_is_open_and_restores_them(controller):
    before = gc.get_threshold()
    first, second = FakeSession(), FakeSession()
    controller.session_started(first)
    controller.session_started(second)
    assert gc.get_threshold() == (before[0], before[1], SUSPENDED_THRESHOLD)  # Young collections unchanged
    controller.session_stopped(first)
    assert controller.active  # Another session still captures
    controller.session_stopped(second)
    assert gc.get_threshold() == before and not controller.active


async def test_collects_when_the_bus_is_quiet_and_not_too_often(controller):
    session = FakeSession()
    controller.session_started(session)
    controller.frames_per_second(1.0)  # Baseline of the frame counters

    # Busy (10 000 frames/s): no collection before the maximum interval
    for _ in range(120):
        controller.clock.now += 1
        session.trace.total += 10_000
        assert not controller.check(1.0)
    # Quiet: collects once the minimum interval has passed since the last one ...
    controller.clock.now += 1
    session.trace.total += 100
    assert controller.check(1.0)
    # ... and not again until the minimum interval has passed once more
    for _ in range(int(controller.min_interval) - 1):
        controller.clock.now += 1
        assert not controller.check(1.0)
    controller.clock.now += 1
    assert controller.check(1.0)
    assert len(controller.collected) == 2


async def test_collects_anyway_after_the_maximum_interval_on_a_busy_bus(controller):
    session = FakeSession()
    controller.session_started(session)
    controller.frames_per_second(1.0)
    controller.clock.now += controller.max_interval - 1
    session.trace.total += 50_000
    assert not controller.check(1.0)
    controller.clock.now += 1
    session.trace.total += 50_000
    assert controller.check(1.0)  # Overdue: bounds the memory reference cycles elsewhere could hold


async def test_rate_is_summed_over_sessions_and_a_cleared_trace_counts_nothing(controller):
    a, b = FakeSession(), FakeSession()
    controller.session_started(a)
    controller.session_started(b)
    controller.frames_per_second(1.0)
    a.trace.total, b.trace.total = 300, 400
    assert controller.frames_per_second(1.0) == 700
    a.trace.total = 0  # Cleared
    assert controller.frames_per_second(2.0) == 0


async def test_a_live_session_suspends_full_collections_until_it_stops():
    """BusSession.start and stop drive the shared controller."""
    before = gc.get_threshold()
    session = BusSession(LiveSource(BusConfig(interface='virtual', channel=f'gc-{uuid.uuid4().hex}')))
    session.start()
    try:
        assert capture_gc.active and gc.get_threshold()[2] == SUSPENDED_THRESHOLD
    finally:
        session.stop()
    assert not capture_gc.active and gc.get_threshold() == before
