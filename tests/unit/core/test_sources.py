import asyncio
import threading

import can
import pytest

from can_explorer.core.session import BusSession, SessionState
from can_explorer.core.sources import MAX_BATCH, FileSource, ThreadBridge


async def test_thread_bridge_batches_and_preserves_order():
    bridge = ThreadBridge(asyncio.get_running_loop())
    count = 3 * MAX_BATCH + 17

    def produce():
        for i in range(count):
            bridge.put(i)

    thread = threading.Thread(target=produce)
    thread.start()
    received, batches = [], 0
    while len(received) < count:
        batch = await asyncio.wait_for(bridge.get_batch(), 2)
        assert 0 < len(batch) <= MAX_BATCH
        received.extend(batch)
        batches += 1
    thread.join()
    assert received == list(range(count))
    assert batches < count / 10  # Items were actually batched


class EndlessSource:
    """Test source whose producer thread pushes messages as fast as possible until closed."""

    description = 'endless'
    can_send = False
    echoes_tx = False
    bitrates = None
    bus_state = None

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        self._bridge = ThreadBridge(loop)
        self._running = True
        threading.Thread(target=self._produce, daemon=True).start()

    def _produce(self) -> None:
        msg = can.Message(arbitration_id=1, is_extended_id=False)
        while self._running:
            self._bridge.put(msg)

    def close(self) -> None:
        self._running = False

    async def batches(self):
        while self._running:
            yield await self._bridge.get_batch()


async def test_session_does_not_starve_the_event_loop():
    """A producer that never pauses must not keep the session pump from yielding to other tasks."""
    session = BusSession(EndlessSource(), buffer_size=1_000)
    session.start()
    ticks = 0
    for _ in range(20):
        await asyncio.sleep(0)
        ticks += 1
    session.stop()
    assert ticks == 20 and session.trace.total > 0


async def test_large_file_is_replayed_completely_in_order(tmp_path):
    path = tmp_path / 'large.blf'
    with can.Logger(path) as logger:
        for i in range(12_345):
            logger.on_message_received(can.Message(arbitration_id=i % 0x800, is_extended_id=False, timestamp=i * 1e-3))
    session = BusSession(FileSource(path, realtime=False), buffer_size=20_000)
    session.start()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while session.state is not SessionState.FINISHED:
        assert loop.time() < deadline
        await asyncio.sleep(0.01)
    assert [frame.message.arbitration_id for frame in session.trace] == [i % 0x800 for i in range(12_345)]


async def test_closing_a_replay_stops_the_reader(tmp_path):
    path = tmp_path / 'slow.asc'
    with can.Logger(path) as logger:
        for i in range(100):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=float(i)))
    session = BusSession(FileSource(path, realtime=True))
    session.start()
    await asyncio.sleep(0.05)
    session.stop()
    assert session.state is SessionState.CLOSED
    assert session.trace.total == 1


@pytest.mark.parametrize('realtime', [True, False])
async def test_replay_restart(tmp_path, realtime):
    path = tmp_path / 'short.asc'
    with can.Logger(path) as logger:
        for i in range(3):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=i * 0.01))
    session = BusSession(FileSource(path, realtime=realtime))
    for expected in (3, 6):
        session.start()
        while session.state is not SessionState.FINISHED:
            await asyncio.sleep(0.01)
        assert session.trace.total == expected
