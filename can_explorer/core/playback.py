"""Timed playback of log files: the clock shared by replays, and transmitting a log file on a bus."""

import asyncio
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

import can

from can_explorer.core.filters import MessageFilter
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

SPEEDS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 0.0)  # 0: as fast as possible
DUE_TOLERANCE = 0.001  # Frames due within this time (s) are played together


def speed_label(speed: float) -> str:
    return 'Max' if speed <= 0 else f'{speed:g}×'


class PlaybackClock:
    """Position in recording time (seconds since the first frame) of a playback.

    It advances with the wall clock multiplied by `speed` and stands still while paused. With speed 0 (as fast as
    possible) it follows the frames instead: `wait_until` returns at once and moves the position on.
    """

    def __init__(self, speed: float = 1.0):
        self._speed = max(0.0, speed)
        self._paused = False
        self._offset = 0.0  # Position at _since
        self._since: Optional[float] = None  # None until started
        self._changed: Optional[asyncio.Event] = None

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def started(self) -> bool:
        return self._since is not None

    @property
    def position(self) -> float:
        if self._since is None or self._paused or self._speed <= 0:
            return self._offset
        return self._offset + (time.monotonic() - self._since) * self._speed

    def start(self, position: float = 0.0) -> None:
        self._offset, self._since = position, time.monotonic()
        self._notify()

    def set_speed(self, speed: float) -> None:
        self._offset, self._since = self.position, time.monotonic()
        self._speed = max(0.0, speed)
        self._notify()

    def pause(self) -> None:
        if not self._paused:
            self._offset = self.position
            self._paused = True
            self._notify()

    def resume(self) -> None:
        if self._paused:
            self._since = time.monotonic()
            self._paused = False
            self._notify()

    def is_due(self, position: float) -> bool:
        """Whether a frame at this recording time can be played now (as fast as possible, it moves the position on)."""
        if self._paused:
            return False
        if self._speed <= 0:
            self._offset = max(self._offset, position)
            return True
        return (position - self.position) / self._speed <= DUE_TOLERANCE

    async def wait_until(self, position: float) -> None:
        """Wait until the playback reaches `position`; follows pauses and speed changes while waiting."""
        while True:
            if self._paused:
                await self._wait_for_change(None)
                continue
            if self._speed <= 0:
                self._offset = max(self._offset, position)
                return
            delay = (position - self.position) / self._speed
            if delay <= DUE_TOLERANCE:
                return
            await self._wait_for_change(delay)

    async def _wait_for_change(self, timeout: Optional[float]) -> None:
        self._changed = event = asyncio.Event()
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def _notify(self) -> None:
        if self._changed is not None:
            self._changed.set()


def scan_log(path: str | Path) -> Tuple[int, float]:
    """Number of frames and duration (s) of a log file. Reads the whole file; run it in a worker thread."""
    count, first, last = 0, None, None
    with can.LogReader(path) as reader:
        for msg in reader:
            count += 1
            if first is None:
                first = msg.timestamp
            last = msg.timestamp
    return count, (last - first) if count else 0.0


class LogPlayer:
    """Transmits the frames of a log file on a session with their recorded timing (scaled by the speed).

    Error frames are skipped; `message_filter` (display filter syntax, e.g. "rx !7DF") selects the frames to send.
    """

    PLAYING, PAUSED, FINISHED, STOPPED, ERROR = 'playing', 'paused', 'finished', 'stopped', 'error'

    def __init__(
        self,
        session,
        path: str | Path,
        speed: float = 1.0,
        loop: bool = False,
        message_filter: Optional[MessageFilter] = None,
        on_finished: Optional[Callable[['LogPlayer'], None]] = None,
    ):
        self.session = session
        self.path = Path(path)
        self.loop = loop
        self.clock = PlaybackClock(speed)
        self.filter = message_filter or MessageFilter()
        self.sent = 0
        self.iteration = 0  # Completed passes when looping
        self.duration: Optional[float] = None
        self.error = ''
        self._state = self.STOPPED
        self._task: Optional[asyncio.Task] = None
        self._on_finished = on_finished

    @property
    def state(self) -> str:
        if self._state == self.PLAYING and self.clock.paused:
            return self.PAUSED
        return self._state

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(f'Log file not found: {self.path}')
        self._state = self.PLAYING
        self._task = asyncio.ensure_future(self._run())
        asyncio.ensure_future(self._scan())

    def stop(self) -> None:
        if self.running:
            self._task.cancel()
        if self._state in (self.PLAYING,):
            self._state = self.STOPPED

    async def _scan(self) -> None:
        try:
            _, self.duration = await asyncio.get_running_loop().run_in_executor(None, scan_log, self.path)
        except Exception as e:  # The playback reports read errors itself
            logger.debug(f'Could not scan {self.path}: {e}')

    async def _run(self) -> None:
        from can_explorer.core.sources import FileSource  # Imported here: sources imports this module

        try:
            while True:
                await self._play_once(FileSource(self.path, speed=0))
                if not self.loop:
                    break
                self.iteration += 1
            self._state = self.FINISHED
        except asyncio.CancelledError:
            self._state = self.STOPPED
        except Exception as e:
            self._state, self.error = self.ERROR, str(e)
            logger.warning(f'Replay of {self.path.name} stopped: {e}')
        if self._on_finished is not None:
            self._on_finished(self)

    async def _play_once(self, source) -> None:
        source.open(asyncio.get_running_loop())
        try:
            first = None
            async for batch in source.batches():
                for msg in batch:
                    if msg.is_error_frame or not self.filter.matches(msg):
                        continue
                    if first is None:
                        first = msg.timestamp
                        self.clock.start()
                    await self.clock.wait_until(msg.timestamp - first)
                    self.session.send(self._copy(msg))
                    self.sent += 1
                await asyncio.sleep(0)  # Let the GUI run between batches sent as fast as possible
        finally:
            source.close()

    @staticmethod
    def _copy(msg: can.Message) -> can.Message:
        return can.Message(
            arbitration_id=msg.arbitration_id,
            is_extended_id=msg.is_extended_id,
            is_remote_frame=msg.is_remote_frame,
            is_fd=msg.is_fd,
            bitrate_switch=msg.bitrate_switch,
            error_state_indicator=msg.error_state_indicator,
            dlc=msg.dlc,
            data=msg.data,
        )
