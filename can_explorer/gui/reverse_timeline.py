"""Temporal "flow" playback for the Reverse engineering Bits tab: a scrubber and play/pause over the selected
identifier's frames, so the bit grid shows the payload at any point in time and the bits flowing and flipping.

The controller holds an IdTimeline (built from a trace snapshot) and a current frame index; while it is active the
ReverseController's bit grid, bytes and field value read the payload at that index instead of the latest frame.

The current frame index is the source of truth: scrubbing and stepping move it directly, so they are frame-exact
even when several frames share a timestamp (a coarse clock, or a capture made in a tight loop, where time no
longer tells frames apart). Playback instead advances a time cursor so it runs at the recorded pace — a frame
holds until the next one — mapping that time back to a frame index as it goes. The slider reads the index as a
fraction (0 at the first frame, 1 at the last).
"""

import time
from typing import List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.reverse.timeline import IdTimeline

TICK_MS = 40  # ~25 updates per second during playback
SPEEDS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)


class ReverseTimeline(QObject):
    """Scrubbing and playback of one identifier's frames."""

    changed = pyqtSignal()  # active, playing or speed
    moved = pyqtSignal()  # position (the grid must update)

    def __init__(self, reverse, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._reverse = reverse
        self._timeline: Optional[IdTimeline] = None
        self._index = 0  # The current frame, source of truth
        self._speed = 1.0
        self._playing = False
        self._play_time = 0.0  # Seconds from the start of the recording, advanced during playback
        self._last_tick = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    # State

    @pyqtProperty(bool, notify=changed)
    def active(self) -> bool:
        return self._timeline is not None and len(self._timeline) > 0

    def enable(self, timeline: IdTimeline) -> None:
        self._timeline = timeline
        self._index = 0
        self._play_time = 0.0
        self._playing = False
        self._timer.stop()
        self.changed.emit()
        self.moved.emit()

    @pyqtSlot()
    def disable(self) -> None:
        if self._timeline is None and not self._playing:
            return
        self._timeline = None
        self._index = 0
        self._play_time = 0.0
        self._playing = False
        self._timer.stop()
        self.changed.emit()
        self.moved.emit()

    @property
    def timeline(self) -> Optional[IdTimeline]:
        return self._timeline

    def payload(self) -> bytes:
        return self._timeline.payload_at(self.index) if self.active else b''

    # Position

    def _clamped_index(self) -> int:
        return max(0, min(len(self._timeline) - 1, self._index))

    @pyqtProperty(int, notify=moved)
    def index(self) -> int:
        return self._clamped_index() if self.active else -1

    @pyqtProperty(float, notify=moved)
    def position(self) -> float:
        """The current frame as a fraction: 0 at the first frame, 1 at the last."""
        count = len(self._timeline) if self.active else 0
        return self._clamped_index() / (count - 1) if count > 1 else 0.0

    @pyqtSlot(float)
    def set_position(self, fraction: float) -> None:
        """Scrub to a frame by fraction (0 the first, 1 the last) — by frame index, so every frame is reachable."""
        if not self.active:
            return
        count = len(self._timeline)
        self._index = round(max(0.0, min(1.0, fraction)) * (count - 1)) if count > 1 else 0
        self.moved.emit()

    @pyqtProperty(int, notify=moved)
    def frame(self) -> int:
        """The current frame number, 1-based, for display."""
        return self.index + 1 if self.active else 0

    @pyqtProperty(int, notify=changed)
    def frames(self) -> int:
        return len(self._timeline) if self.active else 0

    @pyqtProperty(str, notify=moved)
    def time_text(self) -> str:
        if not self.active:
            return ''
        offset = self._reverse.session.start_time
        base = offset if offset is not None else self._timeline.start
        return f'{self._timeline.time_at(self.index) - base:.3f} s'

    @pyqtSlot(int)
    def step(self, delta: int) -> None:
        """Move by `delta` frames (e.g. -1 / +1)."""
        if not self.active:
            return
        self.pause()
        self._index = self._clamped_index() + delta
        self._index = self._clamped_index()
        self.moved.emit()

    # Playback

    @pyqtProperty(bool, notify=changed)
    def playing(self) -> bool:
        return self._playing

    @pyqtSlot()
    def play(self) -> None:
        if not self.active or self._playing:
            return
        if self._clamped_index() >= len(self._timeline) - 1:  # At the end: start over
            self._index = 0
        self._play_time = self._timeline.time_at(self._clamped_index()) - self._timeline.start
        self._playing = True
        self._last_tick = time.monotonic()
        self._timer.start()
        self.changed.emit()

    @pyqtSlot()
    def pause(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self._timer.stop()
        self.changed.emit()

    @pyqtSlot()
    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    @pyqtProperty('QVariantList', constant=True)
    def speeds(self) -> List[float]:
        return list(SPEEDS)

    @pyqtProperty(float, notify=changed)
    def speed(self) -> float:
        return self._speed

    @pyqtSlot(float)
    def set_speed(self, speed: float) -> None:
        self._speed = max(0.05, speed)
        self.changed.emit()

    def _tick(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_tick
        self._last_tick = now
        duration = self._timeline.duration
        if duration <= 0:  # Nothing to advance through in time: jump to the end
            self._index = len(self._timeline) - 1
            self.pause()
        else:
            self._play_time += elapsed * self._speed
            if self._play_time >= duration:
                self._index = len(self._timeline) - 1
                self.pause()
            else:
                self._index = self._timeline.index_at(self._timeline.start + self._play_time)
        self.moved.emit()
