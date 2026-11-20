"""Keeps Python's full garbage collections from interrupting live capture.

A full (generation 2) collection walks every object the process holds, including the up to 100 000 frames of each
session's trace, and stops every thread while it does: 35 to 150 ms, measured with tools/benchmark_socketcan.py.
The kernel buffers only a few hundred frames for a socket, so at 5 000 frames/s and more a full collection made the
receive thread miss frames. The frames themselves are freed by reference counting as the trace drops them; a busy
session leaves no reference cycles for the full collection to find (measured: none in 30 s at 10 000 frames/s).

So while a session is open, automatic full collections are suspended (young collections run as usual), and this
module runs the full collection itself when the buses are quiet: below `QUIET_FPS` frames/s in total, at most every
`MIN_INTERVAL` seconds. If they are never quiet, it collects anyway after `MAX_INTERVAL`, to bound the memory that
reference cycles from elsewhere in the application could hold. The previous settings return when the last session
closes.
"""

import asyncio
import gc
import time
import weakref
from typing import Callable, Optional

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

QUIET_FPS = 500  # Frames/s over all sessions below which a full collection cannot cost frames
CHECK_INTERVAL = 1.0  # Seconds between looks at the traffic
MIN_INTERVAL = 60.0  # Seconds between full collections at least
MAX_INTERVAL = 30 * 60.0  # Seconds after which a full collection runs even on a busy bus
SUSPENDED_THRESHOLD = 1_000_000_000  # Generation 2 threshold that keeps automatic full collections from running


class CaptureGc:
    """Defers full garbage collections to quiet moments while sessions capture. One per process (`capture_gc`)."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        collect: Callable[[], int] = gc.collect,
        quiet_fps: float = QUIET_FPS,
        check_interval: float = CHECK_INTERVAL,
        min_interval: float = MIN_INTERVAL,
        max_interval: float = MAX_INTERVAL,
    ):
        self._clock = clock
        self._collect = collect
        self.quiet_fps = quiet_fps
        self.check_interval = check_interval
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._sessions: 'weakref.WeakSet' = weakref.WeakSet()
        self._saved_threshold: Optional[tuple] = None
        self._task: Optional[asyncio.Task] = None
        self._last_totals: dict = {}  # id(session) -> trace total at the last check
        self._last_collection = 0.0
        self.collections = 0  # Full collections run by this controller

    @property
    def active(self) -> bool:
        return self._saved_threshold is not None

    def session_started(self, session) -> None:
        """Called by BusSession.start, on the event loop."""
        self._sessions.add(session)
        if self._saved_threshold is None:
            self._saved_threshold = gc.get_threshold()
            young, middle, _ = self._saved_threshold
            gc.set_threshold(young, middle, SUSPENDED_THRESHOLD)
            self._last_collection = self._clock()
        loop = asyncio.get_running_loop()
        # A task of a loop that has gone (e.g. a test's) is replaced by one on the current loop
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._task = loop.create_task(self._run(), name='Capture GC')

    def session_stopped(self, session) -> None:
        """Called when a session stops; the last one restores the automatic full collections."""
        self._sessions.discard(session)
        self._last_totals.pop(id(session), None)
        if len(self._sessions) == 0:
            self.release()

    def release(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._saved_threshold is not None:
            gc.set_threshold(*self._saved_threshold)
            self._saved_threshold = None
        self._last_totals.clear()

    def frames_per_second(self, elapsed: float) -> float:
        """Frames received by all sessions since the last call, per second of `elapsed`."""
        received = 0
        totals = {}
        for session in list(self._sessions):
            total = session.trace.total
            totals[id(session)] = total
            received += max(0, total - self._last_totals.get(id(session), total))  # A cleared trace counts 0
        self._last_totals = totals
        return received / elapsed if elapsed > 0 else 0.0

    def check(self, elapsed: float) -> bool:
        """Run a full collection if it is due; True if one ran."""
        rate = self.frames_per_second(elapsed)
        since = self._clock() - self._last_collection
        quiet = rate < self.quiet_fps
        if not ((quiet and since >= self.min_interval) or since >= self.max_interval):
            return False
        started = time.perf_counter()
        found = self._collect()
        self._last_collection = self._clock()
        self.collections += 1
        pause_ms = (time.perf_counter() - started) * 1000
        log = logger.debug if quiet else logger.info
        log(
            f'Full garbage collection ({"quiet bus" if quiet else f"{rate:.0f} frames/s, overdue"}): '
            f'{found} unreachable objects, {pause_ms:.0f} ms'
        )
        return True

    async def _run(self) -> None:
        last = self._clock()
        while True:
            await asyncio.sleep(self.check_interval)
            now = self._clock()
            self.check(now - last)
            last = now


capture_gc = CaptureGc()
