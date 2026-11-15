"""Waiting for asynchronous results in tests by a deadline, never by a fixed time or a fixed number of sleeps.

CI runners differ: Windows rounds sleeps up to its ~15.6 ms timer resolution and runs the virtual buses' threads
slower, macOS runners finish Qt transitions later. A wait that is long enough on a Linux desktop flakes there. So
a test waits until the condition holds (a deadline only ends a wait that would otherwise hang) and then asserts it.
"Nothing happens" checks are the exception: they need a fixed time, and slowness cannot make them fail.
"""

import asyncio
from typing import Callable, Optional

TIMEOUT = 5.0  # Seconds; only reached when the condition never holds


async def wait_until(
    predicate: Callable[[], object], timeout: float = TIMEOUT, pump: Optional[Callable[[], object]] = None
) -> bool:
    """Wait until `predicate()` is true (then True) or `timeout` seconds pass (then False). `pump` runs before each
    check, e.g. `qapp.processEvents` so QML bindings follow."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if pump is not None:
            pump()
        if predicate():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
