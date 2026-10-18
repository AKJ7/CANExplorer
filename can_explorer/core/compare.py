"""Comparing two recordings: identifiers, cycle times, payloads and signal values.

Frames of the two recordings are paired per identifier, either by time (relative to the start of each recording,
or to the first frame of a chosen identifier) or by sequence (the n-th frame of an identifier in one recording
with the n-th in the other).
"""

import bisect
import itertools
import math
import statistics
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import can

Key = Tuple[int, bool]  # Identifier, extended
ALIGN_START, ALIGN_ID, ALIGN_SEQUENCE = 'start', 'id', 'sequence'
CYCLE_TOLERANCE = 0.1  # Cycle times differing by more than 10 % are reported
MAX_PAIR_DISTANCE = 0.5  # Time alignment: frames further apart than half a cycle (or this) are not paired


def key_text(key: Key) -> str:
    return f'{key[0]:08X}' if key[1] else f'{key[0]:03X}'


@dataclass
class IdTrace:
    """The frames of one identifier in a recording."""

    times: array = field(default_factory=lambda: array('d'))
    payloads: List[bytes] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.times)

    def cycle(self) -> Optional[float]:
        """Median time between frames, robust against gaps."""
        if len(self.times) < 2:
            return None
        return statistics.median(b - a for a, b in itertools.pairwise(self.times))

    def jitter(self) -> Optional[float]:
        if len(self.times) < 3:
            return None
        return statistics.pstdev(b - a for a, b in itertools.pairwise(self.times))

    def lengths(self) -> List[int]:
        return sorted({len(payload) for payload in self.payloads})

    def changing_bits(self) -> int:
        """Bits that change at least once, as an integer over the first 64 bytes (byte 0 most significant)."""
        if not self.payloads:
            return 0
        width = max(len(payload) for payload in self.payloads)
        first = int.from_bytes(self.payloads[0].ljust(width, b'\x00'), 'big')
        mask = 0
        for payload in self.payloads[1:]:
            mask |= first ^ int.from_bytes(payload.ljust(width, b'\x00'), 'big')
        return mask


@dataclass
class Recording:
    path: Path
    traces: Dict[Key, IdTrace]
    start: float
    end: float
    frames: int

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def read_recording(
    path, messages: Optional[Iterable[can.Message]] = None, stop: Callable[[], bool] = None
) -> Recording:
    """Read a log file (or the given messages) into per identifier traces; error and remote frames are skipped."""
    traces: Dict[Key, IdTrace] = {}
    start = end = None
    frames = 0
    reader = None
    if messages is None:
        reader = can.LogReader(str(path))
        messages = reader
    try:
        for count, msg in enumerate(messages):
            if stop is not None and count % 10000 == 0 and stop():
                raise InterruptedError('Cancelled')
            if msg.is_error_frame or msg.is_remote_frame:
                continue
            key = (msg.arbitration_id, bool(msg.is_extended_id))
            trace = traces.get(key)
            if trace is None:
                trace = traces[key] = IdTrace()
            trace.times.append(msg.timestamp)
            trace.payloads.append(bytes(msg.data))
            start = msg.timestamp if start is None else min(start, msg.timestamp)
            end = msg.timestamp if end is None else max(end, msg.timestamp)
            frames += 1
    finally:
        if reader is not None:
            reader.stop()
    return Recording(Path(path), traces, start or 0.0, end or 0.0, frames)


@dataclass
class IdDifference:
    key: Key
    status: str  # 'only_a', 'only_b', 'changed', 'same'
    count_a: int = 0
    count_b: int = 0
    cycle_a: Optional[float] = None
    cycle_b: Optional[float] = None
    lengths_a: List[int] = field(default_factory=list)
    lengths_b: List[int] = field(default_factory=list)
    paired: int = 0
    differing: int = 0  # Paired frames with different payloads
    first_difference: Optional[Tuple[float, bytes, bytes]] = None  # Time (relative to A's alignment), A, B
    changing_bits_a: int = 0
    changing_bits_b: int = 0
    reasons: List[str] = field(default_factory=list)

    @property
    def cycle_change(self) -> Optional[float]:
        """Relative change of the cycle time, e.g. 0.2 for 20 % slower."""
        if not self.cycle_a or not self.cycle_b:
            return None
        return self.cycle_b / self.cycle_a - 1


@dataclass
class SignalDifference:
    message: str
    signal: str
    unit: str
    min_a: Optional[float] = None
    max_a: Optional[float] = None
    mean_a: Optional[float] = None
    min_b: Optional[float] = None
    max_b: Optional[float] = None
    mean_b: Optional[float] = None
    paired: int = 0
    differing: int = 0
    max_difference: float = 0.0
    first_difference: Optional[Tuple[float, object, object]] = None

    @property
    def key(self) -> str:
        return f'{self.message}.{self.signal}'

    @property
    def changed(self) -> bool:
        return self.differing > 0 or (self.min_a is None) != (self.min_b is None)


@dataclass
class Comparison:
    a: Recording
    b: Recording
    align: str
    offset_a: float
    offset_b: float
    ids: List[IdDifference]
    signals: List[SignalDifference]
    align_error: str = ''

    def counts(self) -> Dict[str, int]:
        result = {'only_a': 0, 'only_b': 0, 'changed': 0, 'same': 0}
        for difference in self.ids:
            result[difference.status] += 1
        return result


def _offset(recording: Recording, align: str, align_key: Optional[Key]) -> Optional[float]:
    if align == ALIGN_ID and align_key is not None:
        trace = recording.traces.get(align_key)
        return trace.times[0] if trace is not None and trace.count else None
    return recording.start


def _pairs(a: IdTrace, b: IdTrace, align: str, offset_a: float, offset_b: float) -> List[Tuple[int, int]]:
    """Indices of paired frames of an identifier in both recordings."""
    if align == ALIGN_SEQUENCE:
        return [(i, i) for i in range(min(a.count, b.count))]
    times_b = [time - offset_b for time in b.times]
    cycle = a.cycle() or b.cycle()
    limit = min(MAX_PAIR_DISTANCE, cycle / 2) if cycle else MAX_PAIR_DISTANCE
    pairs = []
    used = -1
    for i, time in enumerate(a.times):
        time -= offset_a
        j = bisect.bisect_left(times_b, time)
        candidates = [k for k in (j - 1, j) if used < k < len(times_b)]
        if not candidates:
            continue
        k = min(candidates, key=lambda k: abs(times_b[k] - time))
        if abs(times_b[k] - time) <= limit:
            pairs.append((i, k))
            used = k
    return pairs


def _numeric(value) -> Optional[float]:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def compare(
    a: Recording,
    b: Recording,
    align: str = ALIGN_START,
    align_key: Optional[Key] = None,
    database=None,
    cycle_tolerance: float = CYCLE_TOLERANCE,
) -> Comparison:
    """Compare two recordings; database (DbcDecoder or LdfDecoder) adds signal values."""
    align_error = ''
    offset_a, offset_b = _offset(a, align, align_key), _offset(b, align, align_key)
    if offset_a is None or offset_b is None:
        missing = ' and '.join(name for name, offset in (('A', offset_a), ('B', offset_b)) if offset is None)
        align_error = f'{key_text(align_key)} is not in recording {missing}; aligned by the start instead'
        align, offset_a, offset_b = ALIGN_START, a.start, b.start
    ids: List[IdDifference] = []
    pairs_by_key: Dict[Key, List[Tuple[int, int]]] = {}
    for key in sorted(set(a.traces) | set(b.traces), key=lambda key: (key[1], key[0])):
        trace_a, trace_b = a.traces.get(key), b.traces.get(key)
        if trace_a is None or trace_b is None:
            trace = trace_a or trace_b
            ids.append(
                IdDifference(
                    key,
                    'only_a' if trace_b is None else 'only_b',
                    count_a=trace_a.count if trace_a else 0,
                    count_b=trace_b.count if trace_b else 0,
                    cycle_a=trace_a.cycle() if trace_a else None,
                    cycle_b=trace_b.cycle() if trace_b else None,
                    lengths_a=trace_a.lengths() if trace_a else [],
                    lengths_b=trace_b.lengths() if trace_b else [],
                    reasons=['only in A' if trace_b is None else 'only in B'],
                    changing_bits_a=trace.changing_bits() if trace_a else 0,
                    changing_bits_b=trace.changing_bits() if trace_b else 0,
                )
            )
            continue
        difference = IdDifference(
            key,
            'same',
            trace_a.count,
            trace_b.count,
            trace_a.cycle(),
            trace_b.cycle(),
            trace_a.lengths(),
            trace_b.lengths(),
            changing_bits_a=trace_a.changing_bits(),
            changing_bits_b=trace_b.changing_bits(),
        )
        pairs = pairs_by_key[key] = _pairs(trace_a, trace_b, align, offset_a, offset_b)
        difference.paired = len(pairs)
        for i, j in pairs:
            if trace_a.payloads[i] != trace_b.payloads[j]:
                difference.differing += 1
                if difference.first_difference is None:
                    difference.first_difference = (
                        trace_a.times[i] - offset_a,
                        trace_a.payloads[i],
                        trace_b.payloads[j],
                    )
        change = difference.cycle_change
        if change is not None and abs(change) > cycle_tolerance:
            difference.reasons.append(f'cycle time {change:+.0%}')
        if difference.lengths_a != difference.lengths_b:
            difference.reasons.append('length')
        if difference.differing:
            difference.reasons.append(f'{difference.differing} of {difference.paired} frames differ')
        if difference.changing_bits_a != difference.changing_bits_b:
            difference.reasons.append('other bits change')
        unpaired = max(trace_a.count, trace_b.count) - len(pairs)
        if align != ALIGN_SEQUENCE and unpaired > max(2, 0.05 * max(trace_a.count, trace_b.count)):
            difference.reasons.append(f'{unpaired} frames without counterpart')
        if difference.reasons:
            difference.status = 'changed'
        ids.append(difference)
    signals = _compare_signals(a, b, database, pairs_by_key, offset_a) if database is not None else []
    return Comparison(a, b, align, offset_a, offset_b, ids, signals, align_error)


def _compare_signals(a, b, database, pairs_by_key, offset_a) -> List[SignalDifference]:
    result: Dict[str, SignalDifference] = {}
    units = {info.key: info.unit for info in database.signals()}

    def decode(key: Key, payload: bytes) -> Dict[str, object]:
        msg = can.Message(arbitration_id=key[0], is_extended_id=key[1], data=payload)
        try:
            return database.decode(msg, decode_choices=False) or {}
        except Exception:
            return {}

    def name(key: Key) -> Optional[str]:
        msg = can.Message(arbitration_id=key[0], is_extended_id=key[1])
        definition = database.message_definition(msg)
        return definition.name if definition is not None else None

    def entry(message: str, signal: str) -> SignalDifference:
        full = f'{message}.{signal}'
        if full not in result:
            result[full] = SignalDifference(message, signal, units.get(full, ''))
        return result[full]

    for recording, side in ((a, 'a'), (b, 'b')):
        for key, trace in recording.traces.items():
            message = name(key)
            if message is None:
                continue
            values: Dict[str, List[float]] = {}
            for payload in trace.payloads:
                for signal, value in decode(key, payload).items():
                    number = _numeric(value)
                    if number is not None and math.isfinite(number):
                        values.setdefault(signal, []).append(number)
            for signal, numbers in values.items():
                difference = entry(message, signal)
                setattr(difference, f'min_{side}', min(numbers))
                setattr(difference, f'max_{side}', max(numbers))
                setattr(difference, f'mean_{side}', sum(numbers) / len(numbers))

    for key, pairs in pairs_by_key.items():
        message = name(key)
        if message is None:
            continue
        trace_a, trace_b = a.traces[key], b.traces[key]
        for i, j in pairs:
            values_a, values_b = decode(key, trace_a.payloads[i]), decode(key, trace_b.payloads[j])
            for signal in values_a.keys() & values_b.keys():
                difference = entry(message, signal)
                difference.paired += 1
                value_a, value_b = values_a[signal], values_b[signal]
                if value_a == value_b:
                    continue
                difference.differing += 1
                number_a, number_b = _numeric(value_a), _numeric(value_b)
                if number_a is not None and number_b is not None:
                    difference.max_difference = max(difference.max_difference, abs(number_b - number_a))
                if difference.first_difference is None:
                    difference.first_difference = (trace_a.times[i] - offset_a, value_a, value_b)
    return sorted(result.values(), key=lambda difference: (not difference.changed, difference.key))


def summary_csv(comparison: Comparison) -> str:
    """The identifier comparison as CSV."""

    def number(value: Optional[float], scale: float = 1.0) -> str:
        return '' if value is None else f'{value * scale:.3f}'

    lines = ['id;status;count_a;count_b;cycle_a_ms;cycle_b_ms;paired;differing;first_difference_s;data_a;data_b;notes']
    for difference in comparison.ids:
        first = difference.first_difference
        lines.append(
            ';'.join(
                [
                    key_text(difference.key),
                    difference.status,
                    str(difference.count_a),
                    str(difference.count_b),
                    number(difference.cycle_a, 1000),
                    number(difference.cycle_b, 1000),
                    str(difference.paired),
                    str(difference.differing),
                    number(first[0]) if first else '',
                    first[1].hex(' ').upper() if first else '',
                    first[2].hex(' ').upper() if first else '',
                    ', '.join(difference.reasons),
                ]
            )
        )
    return '\n'.join(lines) + '\n'
