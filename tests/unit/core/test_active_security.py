"""Tests for the active security test module: replay and forgery tests against mock send/receive functions."""

import asyncio

import pytest

from can_explorer.core.active_security_test import (
    dos_resilience_test,
    forgery_test,
    functional_bypass_test,
    replay_test,
    reset_bypass_test,
    session_race_test,
)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def run(coro):
    """Run an async function synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestReplayTest:
    def test_accepted_replay(self):
        """When the observer returns True, the frame is considered accepted."""
        sent = []

        def send(frame_id, payload):
            sent.append((frame_id, payload))

        def observe():
            return True

        result = run(replay_test(send, 0x100, b'\x01\x02\x03', count=3, interval=0.0, observe=observe))
        assert result.replayed is True
        assert result.frame_id == 0x100
        assert len(sent) == 1  # Stopped after first acceptance
        assert any('accepted' in f.lower() for f in result.findings)

    def test_rejected_replay(self):
        """When the observer always returns False, the frame is not accepted."""
        sent = []

        def send(frame_id, payload):
            sent.append((frame_id, payload))

        def observe():
            return False

        result = run(replay_test(send, 0x200, b'\xaa\xbb', count=5, interval=0.0, observe=observe))
        assert result.replayed is False
        assert result.frame_id == 0x200
        assert len(sent) == 5
        assert any('not accepted' in f.lower() for f in result.findings)

    def test_no_observer(self):
        """Without an observer, the test just replays and reports."""
        sent = []

        def send(frame_id, payload):
            sent.append((frame_id, payload))

        result = run(replay_test(send, 0x300, b'\xff', count=3, interval=0.0, observe=None))
        assert result.replayed is False
        assert len(sent) == 3
        assert any('no observer' in f.lower() for f in result.findings)

    def test_async_send(self):
        """The send callable can be async."""
        sent = []

        async def send(frame_id, payload):
            sent.append((frame_id, payload))

        def observe():
            return False

        run(replay_test(send, 0x100, b'\x01', count=2, interval=0.0, observe=observe))
        assert len(sent) == 2


class TestForgeryTest:
    def test_all_rejected(self):
        """When the observer always returns False, no forgeries are accepted."""
        sent = []

        def send(frame_id, payload):
            sent.append((frame_id, payload))

        def observe():
            return False

        result = run(
            forgery_test(
                send,
                0x100,
                b'\x01\x02\x03\x04\x05\x06',
                mac_range=(4, 5),
                attempts=10,
                observe=observe,
                interval=0.0,
            )
        )
        assert result.accepted == 0
        assert result.attempts == 10
        assert result.frame_id == 0x100
        assert len(sent) == 10
        assert any('no forged frames accepted' in f.lower() for f in result.findings)

    def test_some_accepted(self):
        """When the observer sometimes returns True, the count is tracked."""
        call_count = [0]

        def send(frame_id, payload):
            pass

        def observe():
            call_count[0] += 1
            return call_count[0] % 3 == 0  # Every third attempt

        result = run(
            forgery_test(
                send,
                0x200,
                b'\x01\x02\x03\x04',
                mac_range=(2, 3),
                attempts=9,
                observe=observe,
                interval=0.0,
            )
        )
        assert result.accepted == 3  # 3, 6, 9
        assert any('forged frame(s) accepted' in f.lower() for f in result.findings)

    def test_mac_bytes_are_randomised(self):
        """The MAC bytes in the sent frames should differ from the authentic payload."""
        sent = []

        def send(frame_id, payload):
            sent.append(payload)

        def observe():
            return False

        authentic = b'\x01\x02\x03\x04\x05\x06\x07\x08'
        run(
            forgery_test(
                send,
                0x100,
                authentic,
                mac_range=(6, 7),
                attempts=20,
                observe=observe,
                interval=0.0,
            )
        )
        # The non-MAC bytes should be preserved
        for payload in sent:
            assert payload[:6] == authentic[:6]
        # At least some MAC bytes should differ (very unlikely all 20 match)
        different_macs = sum(1 for p in sent if p[6:8] != authentic[6:8])
        assert different_macs > 0

    def test_no_observer(self):
        """Without an observer, the test just sends and reports."""

        def send(frame_id, payload):
            pass

        result = run(
            forgery_test(
                send,
                0x300,
                b'\x01\x02\x03\x04',
                mac_range=(2, 3),
                attempts=5,
                observe=None,
                interval=0.0,
            )
        )
        assert result.accepted == 0
        assert any('no observer' in f.lower() for f in result.findings)


# ---------------------------------------------------------------------------
# functional_bypass_test (roadmap 3.2)
# ---------------------------------------------------------------------------


class TestFunctionalBypassTest:
    def test_bypass_detected(self):
        """Functional addressing returns positive while physical returns negative."""
        sent = []

        def send(frame_id, payload):
            sent.append((frame_id, payload))

        async def observe(timeout=0.5):
            if sent and sent[-1][0] == 0x7DF:
                return b'\x62\xf1\x90\x41'
            return b'\x7f\x22\x33'

        results = run(functional_bypass_test(send, 0x7E0, 0x7DF, [b'\x22\xf1\x90'], observe=observe, interval=0.0))
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_no_bypass(self):
        """Both physical and functional return same response."""
        sent = []

        def send(frame_id, payload):
            sent.append((frame_id, payload))

        async def observe(timeout=0.5):
            return b'\x7f\x22\x33'

        results = run(functional_bypass_test(send, 0x7E0, 0x7DF, [b'\x22\xf1\x90'], observe=observe, interval=0.0))
        bypass_found = any(r.bypass_found for r in results)
        assert bypass_found is False

    def test_no_observer(self):
        """Without observer, still runs and reports."""

        def send(frame_id, payload):
            pass

        results = run(functional_bypass_test(send, 0x7E0, 0x7DF, [b'\x22\xf1\x90'], interval=0.0))
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# session_race_test (roadmap 3.3)
# ---------------------------------------------------------------------------


class TestSessionRaceTest:
    def test_no_race_found(self):
        """No race condition when observer returns negative."""

        def send(frame_id, payload):
            pass

        async def observe(timeout=0.5):
            return b'\x7f\x22\x33'

        result = run(session_race_test(send, 0x7E0, b'\x22\xf1\x90', observe=observe, attempts=3))
        assert result.service_accepted is False
        assert result.race_window_ms == 0.0

    def test_no_observer(self):
        def send(frame_id, payload):
            pass

        result = run(session_race_test(send, 0x7E0, b'\x22\xf1\x90', attempts=2))
        assert isinstance(result.findings, list)


# ---------------------------------------------------------------------------
# reset_bypass_test (roadmap 3.4)
# ---------------------------------------------------------------------------


class TestResetBypassTest:
    def test_no_bypass(self):
        """No bypass when observer returns negative."""

        def send(frame_id, payload):
            pass

        async def observe(timeout=0.5):
            return b'\x7f\x22\x33'

        result = run(
            reset_bypass_test(
                send,
                0x7E0,
                [b'\x22\xf1\x90'],
                observe=observe,
                attempts=1,
                max_window_ms=100.0,
                probe_interval_ms=10.0,
            )
        )
        assert result.bypass_found is False
        assert isinstance(result.findings, list)

    def test_no_observer(self):
        def send(frame_id, payload):
            pass

        result = run(reset_bypass_test(send, 0x7E0, [b'\x22\xf1\x90'], attempts=1, max_window_ms=50.0))
        assert isinstance(result.findings, list)


# ---------------------------------------------------------------------------
# dos_resilience_test (roadmap 3.8)
# ---------------------------------------------------------------------------


class TestDosResilienceTest:
    def test_basic_test(self):
        """Basic DoS resilience test with mock observer."""

        def send(frame_id, payload):
            pass

        async def observe(timeout=0.5):
            return b'\x7e\x00'

        result = run(
            dos_resilience_test(
                send,
                0x7E0,
                b'\x3e\x00',
                observe=observe,
                rates_hz=[10.0, 50.0],
                duration_per_rate=0.05,
            )
        )
        assert result.frames_sent > 0
        assert isinstance(result.findings, list)
        assert isinstance(result.max_sustained_rate_hz, float)

    def test_no_observer(self):
        def send(frame_id, payload):
            pass

        result = run(
            dos_resilience_test(
                send,
                0x7E0,
                b'\x3e\x00',
                rates_hz=[10.0],
                duration_per_rate=0.05,
            )
        )
        assert isinstance(result.findings, list)
