"""Throughput benchmark of the receive pipeline and the table models.

Usage: QT_QPA_PLATFORM=offscreen uv run python tools/benchmark.py
"""

import asyncio
import random
import statistics
import threading
import time
from pathlib import Path

import can
from PyQt6.QtWidgets import QApplication

from can_explorer.core.decoding import DbcDecoder, FrameDecoder
from can_explorer.core.filters import MessageFilter
from can_explorer.core.protocols import create_protocol_decoder
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.gui.models import OverviewModel, TraceModel

DBC = Path(__file__).resolve().parent.parent / 'tests' / 'data' / 'example.dbc'
MESSAGES = 200_000


def make_messages(count: int, ids: int = 500) -> list:
    rng = random.Random(1)
    messages = []
    for i in range(count):
        can_id = 0x100 if i % 5 == 0 else (0x18FEF100 | rng.randrange(256) if i % 5 == 1 else rng.randrange(ids))
        messages.append(
            can.Message(
                arbitration_id=can_id,
                is_extended_id=can_id > 0x7FF,
                data=bytes(rng.randrange(256) for _ in range(8)),
                timestamp=1000.0 + i * 1e-4,
            )
        )
    return messages


class InjectedSource(LiveSource):
    """A LiveSource whose 'notifier thread' is a thread pushing pre-built messages as fast as possible."""

    def __init__(self, messages):
        super().__init__(BusConfig(interface='virtual', channel='benchmark'))
        self._messages = messages

    def open(self, loop):
        super().open(loop)
        listener = self._notifier.listeners[0]
        self._notifier.stop()
        self._notifier = None
        threading.Thread(target=lambda: [listener.on_message_received(m) for m in self._messages], daemon=True).start()


async def pipeline(messages, decoder: FrameDecoder) -> float:
    session = BusSession(InjectedSource(messages), decoder, buffer_size=100_000)
    started = time.perf_counter()
    session.start()
    while session.trace.total < len(messages):
        await asyncio.sleep(0.001)
    elapsed = time.perf_counter() - started
    session.stop()
    return len(messages) / elapsed


def best_of(function, repeat=3):
    return min(timeit_once(function) for _ in range(repeat))


def timeit_once(function):
    started = time.perf_counter()
    function()
    return time.perf_counter() - started


def main():
    app = QApplication([])  # noqa: F841
    messages = make_messages(MESSAGES)
    print(f'Pipeline ({MESSAGES} messages, frames/s):')
    for name, decoder in [
        ('raw', FrameDecoder()),
        ('J1939', FrameDecoder(create_protocol_decoder('J1939'))),
        ('J1939 + DBC', FrameDecoder(create_protocol_decoder('J1939'), DbcDecoder(DBC))),
    ]:
        rates = [asyncio.run(pipeline(messages, decoder)) for _ in range(3)]
        print(f'  {name:<12} {statistics.median(rates):>12,.0f}')

    session = BusSession(InjectedSource(messages), FrameDecoder(), buffer_size=100_000)
    for msg in messages[:100_000]:
        session._dispatch(msg)
    frames = session.trace.drain()

    print('Trace model (seconds):')
    trace = session.trace  # Still holds the 100k frames in memory after the drain
    model = TraceModel(trace, lambda: 1000.0)  # The model follows the store
    model.rebuild()
    print(f'  rebuild 100k, no filter         {best_of(model.rebuild):.4f}')
    message_filter = MessageFilter.parse('100-1FF ext')
    print(f'  filter 100k (100-1FF ext)       {best_of(lambda: model.set_filter(message_filter)):.4f}')
    model.set_filter(MessageFilter())  # Back to no filter for the incremental refresh below
    new_frames = messages[100_000:103_000]  # ~33 ms of a saturated 1 Mbit/s bus... x3

    def grow_and_refresh():
        for msg in new_frames:
            session._dispatch(msg)
        model.refresh()

    print(f'  refresh after 3k new frames     {timeit_once(grow_and_refresh):.4f}')

    overview = OverviewModel(session.statistics)
    overview.rebuild()
    print(f'Overview model ({overview.rowCount()} ids, seconds):')
    for msg in messages[:500]:
        session._dispatch(msg)
    changes = session.statistics.drain_changes()
    print(f'  refresh after 500 updates       {best_of(lambda: overview.refresh(False, changes[1])):.4f}')
    describe = FrameDecoder(None, DbcDecoder(DBC)).full_description
    visible = frames[:50]
    print(f'  describe 50 visible rows (DBC)  {timeit_once(lambda: [describe(f) for f in visible]):.4f}')
    print(f'  rebuild                         {best_of(overview.rebuild):.4f}')


if __name__ == '__main__':
    main()
