from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

from can_explorer.core.frames import Frame
from can_explorer.core.trace_store import TraceStore

TraceBuffer = TraceStore  # The former memory-only trace buffer; TraceStore without a history limit behaves the same

IdKey = Tuple[int, bool, bool]  # (arbitration_id, is_extended_id, is_rx)


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
