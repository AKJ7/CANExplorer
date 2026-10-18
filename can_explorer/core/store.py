from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Set, Tuple

from can_explorer.core.frames import Frame

IdKey = Tuple[int, bool, bool]  # (arbitration_id, is_extended_id, is_rx)


class TraceBuffer:
    """Ring buffer of the most recent frames, plus the frames added since the last drain()."""

    def __init__(self, maxlen: int = 100_000):
        self._frames: deque[Frame] = deque(maxlen=maxlen)
        self._pending: List[Frame] = []
        self.total = 0

    @property
    def maxlen(self) -> int:
        return self._frames.maxlen

    def append(self, frame: Frame) -> None:
        self._frames.append(frame)
        self._pending.append(frame)
        self.total += 1
        if len(self._pending) > 2 * self.maxlen:
            del self._pending[: -self.maxlen]

    def drain(self) -> List[Frame]:
        pending, self._pending = self._pending[-self.maxlen :], []
        return pending

    def clear(self) -> None:
        self._frames.clear()
        self._pending.clear()
        self.total = 0

    def __iter__(self) -> Iterator[Frame]:
        return iter(self._frames)

    def __len__(self) -> int:
        return len(self._frames)


@dataclass(slots=True)
class IdEntry:
    last: Frame
    previous_data: Optional[bytearray] = None
    count: int = 1
    cycle_time: Optional[float] = None

    @property
    def changed(self) -> Tuple[bool, ...]:
        """Which bytes differ from the previous frame. Computed on demand, only for displayed rows."""
        data, previous = self.last.message.data, self.previous_data
        if previous is None:
            return (True,) * len(data)
        return tuple(i >= len(previous) or previous[i] != byte for i, byte in enumerate(data))


class IdStatistics:
    """Per-identifier overview: last frame, count, cycle time and which bytes changed."""

    def __init__(self):
        self.entries: Dict[IdKey, IdEntry] = {}
        self._changed: Set[IdKey] = set()
        self._added = False

    @staticmethod
    def key(frame: Frame) -> IdKey:
        msg = frame.message
        return msg.arbitration_id, msg.is_extended_id, msg.is_rx

    def update(self, frame: Frame) -> None:
        msg = frame.message
        key = (msg.arbitration_id, msg.is_extended_id, msg.is_rx)
        entry = self.entries.get(key)
        if entry is None:
            self.entries[key] = IdEntry(last=frame)
            self._added = True
        else:
            previous = entry.last.message
            entry.cycle_time = msg.timestamp - previous.timestamp
            entry.previous_data = previous.data
            entry.count += 1
            entry.last = frame
        self._changed.add(key)

    def drain_changes(self) -> Tuple[bool, Set[IdKey]]:
        """Return (whether new identifiers appeared, identifiers updated) since the last call."""
        result = self._added, self._changed
        self._added, self._changed = False, set()
        return result

    def clear(self) -> None:
        self.entries.clear()
        self._changed.clear()
        self._added = True
