"""Finding the field that matches a known quantity, a reference: a signal of a database (the same value on another
bus or in another message), a column of a CSV file (e.g. a GPS track) or OBD-II values polled while recording.

The candidate fields of every identifier are sampled at the times of the reference (holding their last value) and
ranked by the correlation with it. A linear fit gives the factor and offset converting the raw value into the
reference's unit, and the best time lag (within ±max_lag) is searched for the best candidates.
"""

import csv
import math
import re
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import can
import numpy as np

from can_explorer.core.reverse.analysis import IdAnalysis, IdFrames, IdKey
from can_explorer.core.reverse.fields import BitField, _motorola_index

MAX_SAMPLES = 2000  # The reference is thinned out to this many samples
STALE = 2.0  # Seconds; a field whose last frame is older than this at a sample time is not sampled there
MIN_SAMPLES = 10
REFINED = 30  # Candidates whose time lag is searched


@dataclass
class Reference:
    name: str
    unit: str = ''
    times: List[float] = dataclass_field(default_factory=list)
    values: List[float] = dataclass_field(default_factory=list)
    exclude: Set[IdKey] = dataclass_field(default_factory=set)  # Identifiers the reference itself comes from

    def __len__(self) -> int:
        return len(self.times)

    def thinned(self, count: int = MAX_SAMPLES) -> 'Reference':
        if len(self.times) <= count:
            return self
        step = len(self.times) / count
        indices = [int(index * step) for index in range(count)]
        return Reference(
            self.name,
            self.unit,
            [self.times[index] for index in indices],
            [self.values[index] for index in indices],
            self.exclude,
        )


def reference_from_signal(database, messages: Iterable[can.Message], message_name: str, signal_name: str) -> Reference:
    """The values of a database signal in the frames (a cantools database or a decoder holding one)."""
    database = getattr(database, 'database', database)
    message = database.get_message_by_name(message_name)
    signal = message.get_signal_by_name(signal_name)
    reference = Reference(f'{message_name}.{signal_name}', signal.unit or '')
    reference.exclude.add((message.frame_id, message.is_extended_frame))
    for msg in messages:
        if msg.arbitration_id != message.frame_id or msg.is_extended_id != message.is_extended_frame:
            continue
        try:
            values = message.decode(bytes(msg.data), decode_choices=False, allow_truncated=True)
        except Exception:
            continue
        value = values.get(signal_name)
        if isinstance(value, (int, float)):
            reference.times.append(msg.timestamp)
            reference.values.append(float(value))
    return reference


_UNIT = re.compile(r'^(.*?)\s*\[(.*)\]\s*$')


def read_reference_csv(path: str | Path, time_offset: float = 0.0) -> List[Reference]:
    """One reference per numeric column of a CSV file whose first column is the time in seconds.

    Headers like 'speed [km/h]' give the unit (as in the CSV export of the signal plots). Times below 1e9 are
    relative and get `time_offset` (the time of the first frame) added; larger ones are taken as absolute.
    """
    path = Path(path)
    with path.open(newline='', encoding='utf-8-sig') as file:
        sample = file.read(4096)
        file.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(file, dialect))
    if len(rows) < 2:
        raise ValueError(f'{path.name} has no data rows')
    header = rows[0]
    references = []
    for column, title in enumerate(header[1:], start=1):
        match = _UNIT.match(title)
        name, unit = (match.group(1), match.group(2)) if match else (title.strip() or f'Column {column}', '')
        references.append(Reference(name, unit))
    for row in rows[1:]:
        try:
            time = float(row[0])
        except (ValueError, IndexError):
            continue
        if time < 1e9:
            time += time_offset
        for column, reference in enumerate(references, start=1):
            try:
                value = float(row[column])
            except (ValueError, IndexError):
                continue
            if math.isfinite(value):
                reference.times.append(time)
                reference.values.append(value)
    references = [reference for reference in references if len(reference) >= MIN_SAMPLES]
    if not references:
        raise ValueError(f'{path.name} has no numeric columns with at least {MIN_SAMPLES} values')
    return references


@dataclass(frozen=True)
class Candidate:
    field: BitField
    multiplexer: Optional[BitField] = None  # Only frames with multiplexer == mux_value carry the field
    mux_value: Optional[int] = None


@dataclass(frozen=True)
class Match:
    field: BitField  # With the factor and offset of the fit
    correlation: float
    lag: float  # Seconds the field lags behind the reference
    samples: int
    multiplexer: Optional[BitField] = None
    mux_value: Optional[int] = None


def candidates(frames: Dict[IdKey, IdFrames], analyses: Sequence[IdAnalysis] = ()) -> List[Candidate]:
    """Byte aligned 8 and 16 bit fields (Intel and Motorola, unsigned and signed) of every identifier, and the
    physical, enumeration and unknown fields found by the analysis."""
    result: Dict[Tuple, Candidate] = {}
    for key, item in frames.items():
        length = max((len(payload) for payload in item.payloads), default=0)
        for byte in range(length):
            options = [BitField(key[0], key[1], byte * 8, 8)]
            if byte + 1 < length:
                options.append(BitField(key[0], key[1], byte * 8, 16))
                options.append(BitField(key[0], key[1], byte * 8 + 7, 16, 'big_endian'))
            for option in options:
                for signed in (False, True):
                    candidate = Candidate(
                        BitField(key[0], key[1], option.start, option.length, option.byte_order, signed)
                    )
                    result[(candidate.field, None, None)] = candidate
    for analysis in analyses:
        for found in analysis.fields:
            if found.kind in ('physical', 'enum', 'unknown'):
                candidate = Candidate(found.field, found.multiplexer, found.mux_value)
                result[(found.field, found.multiplexer, found.mux_value)] = candidate
    return list(result.values())


class _Sampler:
    """The frames of one identifier (optionally of one multiplexer value), sampled at times.

    The payloads are kept as a byte matrix (frames × longest) so a field's value in every sample is extracted in
    one vectorised pass. The extraction reproduces the former per-frame integer arithmetic exactly, including that a
    Motorola field is read from the frame's end (``int.from_bytes(payload, 'big')`` is right-aligned), so it depends
    on each frame's own length.
    """

    def __init__(self, item: IdFrames, multiplexer: Optional[BitField], mux_value: Optional[int]):
        times, payloads = item.times, item.payloads
        if multiplexer is not None:
            pairs = [(t, p) for t, p in zip(times, payloads, strict=True) if multiplexer.raw(p) == mux_value]
            times, payloads = [t for t, _ in pairs], [p for _, p in pairs]
        self.times = np.asarray(times, dtype=float)
        self.lengths = np.asarray([len(payload) for payload in payloads], dtype=np.int64)
        self.maxlen = int(self.lengths.max()) if len(payloads) else 0
        if payloads and self.maxlen:
            joined = b''.join(bytes(payload).ljust(self.maxlen, b'\x00') for payload in payloads)
            self.bytes = np.frombuffer(joined, dtype=np.uint8).reshape(len(payloads), self.maxlen).astype(np.int64)
        else:
            self.bytes = np.zeros((len(payloads), 0), dtype=np.int64)

    def indices(self, times: Sequence[float], lag: float) -> np.ndarray:
        """The frame index to sample at each time (the last frame at or before time + lag, within STALE), or -1."""
        wanted = np.asarray(times, dtype=float) + lag
        if self.times.size == 0:
            return np.full(wanted.size, -1, dtype=np.int64)
        index = np.searchsorted(self.times, wanted, side='right') - 1
        has = index >= 0
        safe = np.where(has, index, 0)
        fresh = has & (wanted - self.times[safe] <= STALE)
        return np.where(fresh, index, -1)

    def values(self, field: BitField, indices: np.ndarray) -> np.ndarray:
        """The raw field value at each index as a float array, NaN where the index is -1 or the field does not fit."""
        indices = np.asarray(indices, dtype=np.int64)
        if indices.size == 0 or self.maxlen == 0:
            return np.full(indices.size, np.nan)
        present = indices >= 0
        safe = np.where(present, indices, 0)
        sizes = self.lengths[safe] * 8
        length, mask = field.length, (1 << field.length) - 1
        if field.byte_order == 'little_endian':
            start = field.start
            fits = present & (start + length <= sizes)
            byte0 = start // 8
            local = np.zeros(indices.size, dtype=np.int64)
            for offset in range((start % 8 + length + 7) // 8):
                column = byte0 + offset
                if column < self.maxlen:
                    local += self.bytes[safe, column] << (8 * offset)
            raw = (local >> (start % 8)) & mask
        else:
            first = _motorola_index(field.start)
            fits = present & (first + length <= sizes)
            shift = sizes - first - length  # Bit offset into int.from_bytes(payload, 'big'); depends on the frame
            big_byte0, big_offset = shift // 8, shift % 8  # The byte of that big-endian integer the value starts in
            local = np.zeros(indices.size, dtype=np.int64)
            for offset in range((7 + length + 7) // 8):
                # Big-endian integer byte (big_byte0 + offset) is payload byte (length_bytes - 1 - that)
                column = self.lengths[safe] - 1 - (big_byte0 + offset)
                in_range = (column >= 0) & (column < self.maxlen)
                local += np.where(in_range, self.bytes[safe, np.where(in_range, column, 0)] << (8 * offset), 0)
            raw = (local >> big_offset) & mask
        if field.signed:
            raw = np.where(raw & (1 << (length - 1)), raw - (1 << length), raw)
        return np.where(fits, raw.astype(float), np.nan)


def _pearson(xs: np.ndarray, ys: np.ndarray) -> Optional[float]:
    if len(xs) < MIN_SAMPLES:
        return None
    dx, dy = xs - xs.mean(), ys - ys.mean()
    sxx, syy = float(np.dot(dx, dx)), float(np.dot(dy, dy))
    if sxx == 0 or syy == 0:
        return None
    return float(np.dot(dx, dy)) / math.sqrt(sxx * syy)


def _fit(xs: np.ndarray, ys: np.ndarray) -> Tuple[float, float]:
    """Least squares y = scale * x + offset."""
    mean_x, mean_y = xs.mean(), ys.mean()
    dx = xs - mean_x
    scale = float(np.dot(dx, ys - mean_y) / np.dot(dx, dx))
    return scale, float(mean_y) - scale * float(mean_x)


def _round(value: float) -> float:
    """A fitted factor or offset as databases have them: a power of two (0.25, 1/256) or a short decimal (0.01)
    when within 0.01 %, otherwise to 6 significant digits."""
    if value == 0:
        return 0.0
    nice = [float(f'{value:.{digits}g}') for digits in (1, 2, 3, 4)]
    nice += [math.copysign(2.0**exponent, value) for exponent in range(-16, 17)]
    for candidate in nice:
        if abs(candidate - value) <= 1e-4 * abs(value):
            return candidate
    return float(f'{value:.6g}')


def correlate(
    reference: Reference,
    frames: Dict[IdKey, IdFrames],
    candidate_fields: Sequence[Candidate],
    max_lag: float = 1.0,
    lag_step: float = 0.05,
    top: int = 20,
) -> List[Match]:
    """The candidates correlating best with the reference, best first."""
    reference = reference.thinned()
    if len(reference) < MIN_SAMPLES:
        return []
    groups: Dict[Tuple[IdKey, Optional[BitField], Optional[int]], List[Candidate]] = {}
    for candidate in candidate_fields:
        key = (candidate.field.frame_id, candidate.field.extended)
        if key in frames and key not in reference.exclude:
            groups.setdefault((key, candidate.multiplexer, candidate.mux_value), []).append(candidate)
    samplers = {group: _Sampler(frames[group[0]], group[1], group[2]) for group in groups}
    reference_times = np.asarray(reference.times, dtype=float)
    reference_values = np.asarray(reference.values, dtype=float)

    def score(group, candidate: Candidate, lag: float, indices=None):
        sampler = samplers[group]
        indices = indices if indices is not None else sampler.indices(reference_times, lag)
        values = sampler.values(candidate.field, indices)
        valid = np.isfinite(values)
        if int(valid.sum()) < MIN_SAMPLES:
            return None
        xs, ys = values[valid], reference_values[valid]
        correlation = _pearson(xs, ys)
        return None if correlation is None else (correlation, xs, ys)

    first: List[Tuple[float, Tuple, Candidate]] = []
    for group, members in groups.items():
        indices = samplers[group].indices(reference_times, 0.0)
        for candidate in members:
            result = score(group, candidate, 0.0, indices)
            if result is not None:
                first.append((abs(result[0]), group, candidate))
    first.sort(key=lambda item: -item[0])

    lags = [0.0]
    steps = int(round(max_lag / lag_step)) if lag_step > 0 else 0
    lags += [round(sign * step * lag_step, 6) for step in range(1, steps + 1) for sign in (1, -1)]
    matches = []
    for _, group, candidate in first[:REFINED]:
        best = None
        for lag in lags:
            result = score(group, candidate, lag)
            if result is not None and (best is None or abs(result[0]) > abs(best[0][0]) + 1e-9):
                best = (result, lag)
        if best is None:
            continue
        (correlation, xs, ys), lag = best
        scale, offset = _fit(xs, ys)
        if scale == 0:
            continue
        field = candidate.field
        fitted = BitField(
            field.frame_id,
            field.extended,
            field.start,
            field.length,
            field.byte_order,
            field.signed,
            _round(scale),
            _round(offset),
        )
        matches.append(Match(fitted, correlation, lag, len(xs), candidate.multiplexer, candidate.mux_value))
    matches.sort(key=lambda match: -abs(match.correlation))
    # The same bits in several readings (e.g. signed and unsigned) only once, the best one
    seen = set()
    unique = []
    for match in matches:
        identity = (match.field.frame_id, match.field.extended, tuple(sorted(match.field.bits)), match.mux_value)
        if identity not in seen:
            seen.add(identity)
            unique.append(match)
    return unique[:top]
