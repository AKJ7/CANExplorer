from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import can

from can_explorer.core.decoding import DbcDecoder


@dataclass(slots=True)
class SignalSeries:
    """Time series of one signal. Old points are dropped in chunks to keep appends cheap."""

    max_points: int
    unit: str = ''
    times: List[float] = field(default_factory=list)
    values: List[float] = field(default_factory=list)

    def append(self, timestamp: float, value: float) -> None:
        self.times.append(timestamp)
        self.values.append(value)
        if len(self.times) > 2 * self.max_points:
            del self.times[: -self.max_points]
            del self.values[: -self.max_points]

    def window(self, start: float, end: float) -> Tuple[List[float], List[float]]:
        """Points with start <= time <= end, plus the point just before the window so lines enter it."""
        first = max(0, bisect_left(self.times, start) - 1)
        last = bisect_left(self.times, end, lo=first)
        last = min(len(self.times), last + 1)
        return self.times[first:last], self.values[first:last]

    @property
    def last(self) -> Optional[Tuple[float, float]]:
        return (self.times[-1], self.values[-1]) if self.times else None

    def clear(self) -> None:
        self.times.clear()
        self.values.clear()


class SignalTracker:
    """Records the values of selected database signals for plotting.

    Only frames carrying a tracked signal are decoded, so tracking does not slow down the other traffic.
    """

    def __init__(self, max_points: int = 50_000):
        self.max_points = max_points
        self.series: Dict[str, SignalSeries] = {}
        self._by_message: Dict[str, Set[str]] = {}
        self._changed = False

    @staticmethod
    def key(message: str, signal: str) -> str:
        return f'{message}.{signal}'

    @property
    def tracked(self) -> List[str]:
        return list(self.series)

    def track(self, message: str, signal: str, unit: str = '') -> str:
        key = self.key(message, signal)
        if key not in self.series:
            self.series[key] = SignalSeries(self.max_points, unit)
            self._by_message.setdefault(message, set()).add(signal)
        return key

    def untrack(self, message: str, signal: str) -> None:
        key = self.key(message, signal)
        if self.series.pop(key, None) is not None:
            signals = self._by_message[message]
            signals.discard(signal)
            if not signals:
                del self._by_message[message]

    def untrack_all(self) -> None:
        self.series.clear()
        self._by_message.clear()

    def update(self, msg: can.Message, database: DbcDecoder) -> None:
        if not self._by_message:
            return
        definition = database.message_definition(msg)
        if definition is None:
            return
        signals = self._by_message.get(definition.name)
        if not signals:
            return
        try:
            values = definition.decode(bytes(msg.data), decode_choices=False, allow_truncated=True)
        except Exception:
            return
        for signal in signals:
            value = values.get(signal)
            if isinstance(value, (int, float)):
                self.series[self.key(definition.name, signal)].append(msg.timestamp, float(value))
                self._changed = True

    def drain_changed(self) -> bool:
        changed, self._changed = self._changed, False
        return changed

    def clear(self) -> None:
        for series in self.series.values():
            series.clear()
        self._changed = True
