import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import can

from can_explorer.core.decoding import DbcDecoder
from can_explorer.core.reverse.fields import BitField


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

    @property
    def first(self) -> Optional[Tuple[float, float]]:
        return (self.times[0], self.values[0]) if self.times else None

    def value_at(self, time: float) -> Optional[float]:
        """The value at `time`: of the last point at or before it (signals hold their value between frames)."""
        index = bisect_right(self.times, time) - 1
        return self.values[index] if index >= 0 else None

    def statistics(self, start: float, end: float) -> Optional[Tuple[float, float, float, int]]:
        """(minimum, maximum, mean, count) of the finite values with start <= time <= end."""
        first = bisect_left(self.times, start)
        last = bisect_right(self.times, end)
        values = [value for value in self.values[first:last] if math.isfinite(value)]
        if not values:
            return None
        return min(values), max(values), sum(values) / len(values), len(values)

    def clear(self) -> None:
        self.times.clear()
        self.values.clear()


class SignalTracker:
    """Records the values of selected database signals, and of bit fields without a database, for plotting.

    Only frames carrying a tracked signal are decoded, so tracking does not slow down the other traffic.
    """

    def __init__(self, max_points: int = 200_000):
        self.max_points = max_points
        self.series: Dict[str, SignalSeries] = {}
        self.fields: Dict[str, BitField] = {}  # By key, e.g. '1A0[16|8@1+]'
        self.multiplexers: Dict[str, Tuple[BitField, Optional[int]]] = {}  # Of multiplexed fields, by key
        self._by_message: Dict[str, Set[str]] = {}
        self._fields_by_id: Dict[Tuple[int, bool], List[Tuple[str, BitField, Optional[BitField], Optional[int]]]] = {}
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

    @staticmethod
    def field_key(field: BitField, multiplexer: Optional[BitField] = None, mux_value: Optional[int] = None) -> str:
        """E.g. '1A0[16|8@1+]', or '600[8|16@1+] if 600[0|2@1+]=1' for a multiplexed field."""
        return field.key if multiplexer is None else f'{field.key} if {multiplexer.key}={mux_value}'

    def track_field(
        self,
        field: BitField,
        unit: str = '',
        multiplexer: Optional[BitField] = None,
        mux_value: Optional[int] = None,
    ) -> str:
        """Track a bit field, of the frames with multiplexer == mux_value when given; one tracked already at the same
        bits is replaced (e.g. with another factor)."""
        key = self.field_key(field, multiplexer, mux_value)
        self.untrack_key(key)
        self.series[key] = SignalSeries(self.max_points, unit)
        self.fields[key] = field
        if multiplexer is not None:
            self.multiplexers[key] = (multiplexer, mux_value)
        self._fields_by_id.setdefault((field.frame_id, field.extended), []).append((key, field, multiplexer, mux_value))
        return key

    def backfill(self, key: str, messages) -> None:
        """Add the values of a tracked field in earlier frames, e.g. of the trace, in time order."""
        field, series = self.fields.get(key), self.series.get(key)
        if field is None or series is None:
            return
        multiplexer, mux_value = self.multiplexers.get(key, (None, None))
        for msg in messages:
            if msg.arbitration_id == field.frame_id and msg.is_extended_id == field.extended:
                if multiplexer is not None and multiplexer.raw(msg.data) != mux_value:
                    continue
                value = field.value(msg.data)
                if value is not None:
                    series.append(msg.timestamp, value)
        self._changed = True

    def untrack_key(self, key: str) -> None:
        """Stop tracking a field or signal by its key."""
        field = self.fields.pop(key, None)
        if field is not None:
            self.series.pop(key, None)
            self.multiplexers.pop(key, None)
            id_key = (field.frame_id, field.extended)
            remaining = [entry for entry in self._fields_by_id[id_key] if entry[0] != key]
            if remaining:
                self._fields_by_id[id_key] = remaining
            else:
                del self._fields_by_id[id_key]
            return
        message, _, signal = key.partition('.')
        if signal:
            self.untrack(message, signal)

    def untrack_all(self, fields: bool = True) -> None:
        """Stop tracking the database signals, and the bit fields unless `fields` is false."""
        if fields:
            self.series.clear()
            self.fields.clear()
            self.multiplexers.clear()
            self._fields_by_id.clear()
        else:
            for key in [key for key in self.series if key not in self.fields]:
                del self.series[key]
        self._by_message.clear()

    def update(self, msg: can.Message, database: Optional[DbcDecoder]) -> None:
        if self._fields_by_id:
            fields = self._fields_by_id.get((msg.arbitration_id, msg.is_extended_id))
            if fields and not msg.is_remote_frame and not msg.is_error_frame:
                for key, field, multiplexer, mux_value in fields:
                    if multiplexer is not None and multiplexer.raw(msg.data) != mux_value:
                        continue
                    value = field.value(msg.data)
                    if value is not None:
                        self.series[key].append(msg.timestamp, value)
                        self._changed = True
        if not self._by_message or database is None:
            return
        definition = database.message_definition(msg)
        if definition is None:
            return
        # Container frames carry several PDUs, each tracked under its own name
        names = [part.name for part in getattr(definition, 'contained_messages', None) or [definition]]
        if not any(name in self._by_message for name in names):
            return
        try:
            if len(names) > 1 or getattr(definition, 'is_container', False):
                parts = database.decode_message(definition, msg.data, decode_choices=False)
            else:
                parts = [
                    (definition.name, definition.decode(bytes(msg.data), decode_choices=False, allow_truncated=True))
                ]
        except Exception:
            return
        for name, values in parts:
            for signal in self._by_message.get(name, ()):
                value = values.get(signal)
                if isinstance(value, (int, float)):
                    self.series[self.key(name, signal)].append(msg.timestamp, float(value))
                    self._changed = True

    def drain_changed(self) -> bool:
        changed, self._changed = self._changed, False
        return changed

    def clear(self) -> None:
        for series in self.series.values():
            series.clear()
        self._changed = True
