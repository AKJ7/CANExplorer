"""Checking FlexRay traffic against its schedule, and the health of the bus: what a cluster description says should
happen, against what the frames show.

Per channel, the frames of one communication cycle are collected; when the cycle counter moves on, the cycle is
checked:

- Schedule (with a cluster description): a frame the description expects in that slot, channel and cycle that did
  not come is *missing* (a null frame counts as sent); a frame the description does not have is *not described*;
  a payload whose length is not the frame's is *wrong length*; a static frame whose length is not the cluster's
  static payload length is reported too.
- Timing (with the cluster's macrotick and static slot length): the start of the cycle is estimated from its first
  static frame (its time minus its slot's offset), and every static frame is expected within one static slot of its
  own slot's offset from there; the cycle length is compared with the description's. The precision is the
  timestamps': a log with 10 µs resolution shows deviations of that order.
- Dynamic segment (with the number of static slots): frames above the static slots must come in ascending slot
  order within a cycle.
- Health: frames, null frames, frames with errors (by error), sync and startup frames per cycle (a cluster needs at
  least two sync frames per cycle to keep its clock synchronised), cycle counter jumps (cycles without any frame),
  and the symbols and controller events of the log.
- Channels A and B: a frame sent on both channels in the same cycle with different payloads or header flags is a
  *channel mismatch* (the redundant copies of a frame are identical).
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from can_explorer.core.flexray import CYCLES, FlexRayEvent, FlexRayFrame

MIN_SYNC_FRAMES = 2


@dataclass
class _Cycle:
    number: int
    frames: List[FlexRayFrame] = field(default_factory=list)


@dataclass
class _ChannelHealth:
    frames: int = 0
    null: int = 0
    with_errors: int = 0
    errors: Counter = field(default_factory=Counter)
    cycles: int = 0
    cycle_jumps: int = 0
    skipped_cycles: int = 0
    sync_per_cycle: List[int] = field(default_factory=list)  # Of the last cycles
    startup_per_cycle: List[int] = field(default_factory=list)
    few_sync_cycles: int = 0
    cycle_times: List[float] = field(default_factory=list)


class FlexRayAnalyzer:
    """Feed it frames (in time order) with add(); report() returns what was found. `database` is a FlexRayDatabase
    or None (then only health and the channel comparison are checked)."""

    KEPT = 256  # Cycles whose sync counts and lengths are kept for the statistics

    def __init__(self, database: Any = None):
        self.database = database
        self.reset()

    def reset(self) -> None:
        self._current: Dict[str, _Cycle] = {}
        self._health: Dict[str, _ChannelHealth] = defaultdict(_ChannelHealth)
        self._issues: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
        self._last_start: Dict[str, float] = {}
        self._last_cycle: Dict[str, int] = {}
        self._timing_checked = 0
        self._max_deviation = 0.0
        self._pending_ab: Dict[Tuple[int, int], FlexRayFrame] = {}
        self._ab_compared = 0
        self._ab_mismatches = 0
        self._events: Counter = Counter()
        self._dynamic_frames = 0

    def set_database(self, database: Any) -> None:
        self.database = database
        self.reset()

    # Feeding

    def add(self, frames: Iterable[FlexRayFrame]) -> None:
        for frame in frames:
            current = self._current.get(frame.channel)
            if current is None or frame.cycle != current.number:
                if current is not None:
                    self._finish(frame.channel, current, frame.cycle)
                current = self._current[frame.channel] = _Cycle(frame.cycle)
            current.frames.append(frame)
            self._count(frame)
            self._compare_channels(frame)

    def add_events(self, events: Iterable[FlexRayEvent]) -> None:
        for event in events:
            self._events[(event.kind, event.text.split(':')[0])] += 1

    def flush(self) -> None:
        """Check the cycles still being collected (at the end of a log)."""
        for channel, current in list(self._current.items()):
            self._finish(channel, current, None)
        self._current.clear()

    # Checks

    def _issue(self, slot: int, channel: str, kind: str, cycle: int, name: str = '', detail: str = '') -> None:
        entry = self._issues.setdefault(
            (slot, channel, kind), {'slot': slot, 'channel': channel, 'kind': kind, 'name': name, 'count': 0}
        )
        entry['count'] += 1
        entry['cycle'] = cycle
        if detail:
            entry['detail'] = detail

    def _count(self, frame: FlexRayFrame) -> None:
        health = self._health[frame.channel]
        health.frames += 1
        health.null += frame.null_frame
        if frame.errors:
            health.with_errors += 1
            health.errors.update(frame.errors)
        database = self.database
        if database is None:
            return
        definition = database.definition(frame)
        if definition is None:
            self._issue(frame.slot, frame.channel, 'not described', frame.cycle)
            return
        if frame.null_frame:
            return
        expected = definition.length + definition.length % 2
        if len(frame.payload) != expected:
            self._issue(
                frame.slot, frame.channel, 'wrong length', frame.cycle, definition.name,
                f'{len(frame.payload)} bytes, {expected} expected',
            )  # fmt: skip
        parameters = database.parameters
        if parameters.payload_length_static and parameters.is_dynamic(frame.slot) is False:
            static = parameters.payload_length_static * 2
            if len(frame.payload) != static:
                self._issue(
                    frame.slot, frame.channel, 'static payload length', frame.cycle, definition.name,
                    f'{len(frame.payload)} bytes, the cluster sends {static}',
                )  # fmt: skip

    def _compare_channels(self, frame: FlexRayFrame) -> None:
        key = (frame.slot, frame.cycle)
        other = self._pending_ab.get(key)
        window = self._window()
        if other is not None and other.channel != frame.channel and abs(frame.timestamp - other.timestamp) <= window:
            del self._pending_ab[key]
            self._ab_compared += 1
            if (other.payload, other.null_frame, other.sync, other.startup) != (
                frame.payload,
                frame.null_frame,
                frame.sync,
                frame.startup,
            ):
                self._ab_mismatches += 1
                name = self._name(frame)
                self._issue(frame.slot, 'A+B', 'channel mismatch', frame.cycle, name, 'A and B differ')
            return
        self._pending_ab[key] = frame
        if len(self._pending_ab) > 4096:  # Frames on one channel only: forget the oldest
            for old in list(self._pending_ab)[:2048]:
                del self._pending_ab[old]

    def _window(self) -> float:
        cycle = self.database.parameters.cycle_time if self.database is not None else None
        return (cycle or 0.005) / 2

    def _name(self, frame: FlexRayFrame) -> str:
        if self.database is None:
            return ''
        definition = self.database.definition(frame)
        return definition.name if definition is not None else ''

    def _finish(self, channel: str, cycle: _Cycle, next_cycle: Optional[int]) -> None:
        health = self._health[channel]
        health.cycles += 1
        if next_cycle is not None and next_cycle != (cycle.number + 1) % CYCLES:
            health.cycle_jumps += 1
            health.skipped_cycles += (next_cycle - cycle.number - 1) % CYCLES
        sync = len({frame.slot for frame in cycle.frames if frame.sync and not frame.errors})
        startup = len({frame.slot for frame in cycle.frames if frame.startup and not frame.errors})
        health.sync_per_cycle = (health.sync_per_cycle + [sync])[-self.KEPT :]
        health.startup_per_cycle = (health.startup_per_cycle + [startup])[-self.KEPT :]
        health.few_sync_cycles += sync < MIN_SYNC_FRAMES
        database = self.database
        if database is None:
            return
        seen = {frame.slot for frame in cycle.frames}
        for definition in database.definitions:
            if channel in definition.channels and definition.in_cycle(cycle.number) and definition.slot not in seen:
                self._issue(definition.slot, channel, 'missing', cycle.number, definition.name)
        parameters = database.parameters
        if parameters.static_slots is not None:
            dynamic = [frame.slot for frame in cycle.frames if frame.slot > parameters.static_slots]
            self._dynamic_frames += len(dynamic)
            for previous, slot in zip(dynamic, dynamic[1:], strict=False):
                if slot <= previous:
                    self._issue(slot, channel, 'dynamic slot order', cycle.number, '', f'after slot {previous}')
        self._check_timing(channel, cycle)

    def _check_timing(self, channel: str, cycle: _Cycle) -> None:
        parameters = self.database.parameters
        slot_time = parameters.static_slot_time
        if slot_time is None:
            return
        static = [
            (frame, offset)
            for frame in cycle.frames
            if (offset := parameters.static_slot_offset(frame.slot)) is not None
        ]
        if not static:
            return
        # The cycle start: each static frame's time minus its slot's offset; the median of those that agree with
        # the start predicted from the previous cycle (when there is one), so a stray frame does not move it
        starts = sorted(frame.timestamp - offset for frame, offset in static)
        previous = self._last_start.get(channel)
        predicted = previous + parameters.cycle_time if previous is not None and parameters.cycle_time else None
        if predicted is not None and cycle.number == (self._last_cycle.get(channel, -2) + 1) % CYCLES:
            agreeing = [start for start in starts if abs(start - predicted) <= slot_time]
            start = agreeing[len(agreeing) // 2] if agreeing else predicted
            checkable = True
        else:
            start = starts[len(starts) // 2]
            checkable = len(static) > 1  # One frame alone cannot be off from itself
        for frame, offset in static if checkable else ():
            deviation = frame.timestamp - offset - start
            self._timing_checked += 1
            self._max_deviation = max(self._max_deviation, abs(deviation))
            if abs(deviation) > slot_time:
                self._issue(
                    frame.slot, channel, 'timing', cycle.number, self._name(frame),
                    f'{deviation * 1e6:+.0f} µs from its slot',
                )  # fmt: skip
        if previous is not None and parameters.cycle_time:
            length = start - previous
            if 0 < length < 2 * parameters.cycle_time:  # Consecutive cycles only
                health = self._health[channel]
                health.cycle_times = (health.cycle_times + [length])[-self.KEPT :]
        self._last_start[channel] = start
        self._last_cycle[channel] = cycle.number

    # Results

    def report(self) -> Dict[str, Any]:
        channels = {}
        for channel, health in sorted(self._health.items()):
            sync = health.sync_per_cycle
            times = health.cycle_times
            channels[channel] = {
                'frames': health.frames,
                'null': health.null,
                'with_errors': health.with_errors,
                'errors': dict(health.errors.most_common()),
                'cycles': health.cycles,
                'cycle_jumps': health.cycle_jumps,
                'skipped_cycles': health.skipped_cycles,
                'sync_min': min(sync) if sync else None,
                'sync_max': max(sync) if sync else None,
                'startup_max': max(health.startup_per_cycle) if health.startup_per_cycle else None,
                'few_sync_cycles': health.few_sync_cycles,
                'cycle_time': sum(times) / len(times) if times else None,
                'cycle_time_min': min(times) if times else None,
                'cycle_time_max': max(times) if times else None,
            }
        issues = sorted(self._issues.values(), key=lambda entry: (entry['slot'], entry['channel'], entry['kind']))
        return {
            'channels': channels,
            'issues': issues,
            'timing': {'checked': self._timing_checked, 'max_deviation': self._max_deviation},
            'channel_comparison': {'compared': self._ab_compared, 'mismatches': self._ab_mismatches},
            'dynamic_frames': self._dynamic_frames,
            'events': [{'kind': kind, 'text': text, 'count': count} for (kind, text), count in self._events.items()],
        }


def analyse(frames: Iterable[FlexRayFrame], database: Any = None, events: Iterable[FlexRayEvent] = ()) -> Dict:
    """The report of a whole log (see FlexRayAnalyzer)."""
    analyzer = FlexRayAnalyzer(database)
    analyzer.add(frames)
    analyzer.flush()
    analyzer.add_events(events)
    return analyzer.report()


class SignalRecorder:
    """Time series of chosen FlexRay signals, keyed "Frame.Signal", for plotting (the series and `tracked` the
    signal plots read). Only updated PDUs give values."""

    def __init__(self, max_points: int = 200_000):
        from can_explorer.core.signals import SignalSeries

        self._series_type = SignalSeries
        self.max_points = max_points
        self.series: Dict[str, Any] = {}
        self._by_frame: Dict[str, Dict[str, str]] = {}  # frame name → {signal: key}
        self._changed = False

    @staticmethod
    def key(frame: str, signal: str) -> str:
        return f'{frame}.{signal}'

    @property
    def tracked(self) -> List[str]:
        return list(self.series)

    def track(self, frame: str, signal: str, unit: str = '') -> str:
        key = self.key(frame, signal)
        if key not in self.series:
            self.series[key] = self._series_type(self.max_points, unit)
            self._by_frame.setdefault(frame, {})[signal] = key
        return key

    def untrack_key(self, key: str) -> None:
        if self.series.pop(key, None) is None:
            return
        for frame, signals in list(self._by_frame.items()):
            for signal, existing in list(signals.items()):
                if existing == key:
                    del signals[signal]
            if not signals:
                del self._by_frame[frame]

    def untrack_all(self) -> None:
        self.series.clear()
        self._by_frame.clear()

    def clear(self) -> None:
        for series in self.series.values():
            series.clear()

    def update(self, frames: Iterable[FlexRayFrame], database: Any) -> None:
        if not self._by_frame or database is None:
            return
        for frame in frames:
            definition = database.definition(frame)
            if definition is None or definition.name not in self._by_frame:
                continue
            wanted = self._by_frame[definition.name]
            _, values = database.decode(frame, numeric=True)
            for signal, key in wanted.items():
                value = values.get(signal)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.series[key].append(frame.timestamp, float(value))
                    self._changed = True

    def drain_changed(self) -> bool:
        changed, self._changed = self._changed, False
        return changed
