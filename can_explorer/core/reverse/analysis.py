"""Automatic analysis of the payloads of an identifier: checksums, multiplexers, fields and what kind they are.

1. Checksums: bytes that are a CRC, XOR or sum of the other bytes (see checksums.py).
2. Multiplexer: a byte or nibble taking a few values and changing often, grouping the frames by which makes many
   other bits change much less from one frame of a group to the next. The fields are then analysed per multiplexer value.
3. Fields: within a field the bits change more often the less significant they are (a counter's lowest bit in
   every frame, the next one in every second frame, …). Walking the bits from the most to the least significant
   one, a bit changing much less often than the one before starts a new field; bits that never change separate
   fields. This is done in Intel and in Motorola bit order.
4. Neighbouring fields that the first rule joins are split again: in a real field the upper part changes only when
   the lower part wraps around (255 → 0), in two independent fields it does not; and of rarely changing fields
   (flags, enumerations), parts that hardly ever change in the same frame are separate fields.
5. Each field is classified: constant, counter (the same step from frame to frame through its whole range), flag,
   enumeration (few values,
   rarely changing), physical value (changing smoothly, read signed when that is smoother) or unknown. Of the
   fields found in both bit orders the most plausible non-overlapping ones are kept.
"""

from collections import deque
from dataclasses import dataclass, field as dataclass_field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import can

from can_explorer.core.reverse.checksums import find_checksums
from can_explorer.core.reverse.fields import BitField, _from_motorola_index, _motorola_index

IdKey = Tuple[int, bool]

MIN_FRAMES = 20
MAX_FRAMES = 20_000  # The most recent frames of each identifier are analysed
DROP = 0.5  # A bit changing less than this share as often as the more significant one before it starts a new field
COUPLING = 0.3  # Below this share of upper part changes with the lower part wrapping, a field is split in two
MIN_UPPER_CHANGES = 5
SMOOTH = 0.1  # Mean change per frame relative to the range below which a field is a physical value
COUNTER_SHARE = 0.9
ENUM_VALUES = 16
ENUM_CHANGES = 0.3
MUX_MIN_GROUP = 5

KINDS = ('checksum', 'counter', 'multiplexer', 'physical', 'enum', 'flag', 'constant', 'unknown')
# Value of a field per bit when choosing between overlapping fields (physical values: their confidence)
_WEIGHT = {'checksum': 1.2, 'counter': 1.1, 'enum': 0.8, 'flag': 0.8, 'unknown': 0.1, 'constant': 0.0}
MUX_CALM = 0.3  # Share of the changing bits that must calm down when the frames are grouped by the multiplexer
MUX_MIN_CALM_BITS = 4  # Fewer may calm down by coincidence, e.g. with two correlated values (speed and torque)


@dataclass
class IdFrames:
    """The frames of one identifier: timestamps and payloads."""

    key: IdKey
    times: List[float] = dataclass_field(default_factory=list)
    payloads: List[bytes] = dataclass_field(default_factory=list)


def collect(
    messages: Iterable[can.Message], max_frames: int = MAX_FRAMES, exclude: Optional[Set[IdKey]] = None
) -> Dict[IdKey, IdFrames]:
    """The most recent `max_frames` frames of each identifier (without remote and error frames)."""
    buffers: Dict[IdKey, Tuple[deque, deque]] = {}
    for msg in messages:
        if msg.is_remote_frame or msg.is_error_frame:
            continue
        key = (msg.arbitration_id, msg.is_extended_id)
        if exclude and key in exclude:
            continue
        buffer = buffers.get(key)
        if buffer is None:
            buffer = buffers[key] = (deque(maxlen=max_frames), deque(maxlen=max_frames))
        buffer[0].append(msg.timestamp)
        buffer[1].append(bytes(msg.data))
    return {key: IdFrames(key, list(times), list(payloads)) for key, (times, payloads) in buffers.items()}


@dataclass(frozen=True)
class FoundField:
    field: BitField
    kind: str
    confidence: float  # 0 to 1
    details: str = ''
    minimum: float = 0  # Raw values (signed when the field is)
    maximum: float = 0
    distinct: int = 0
    multiplexer: Optional[BitField] = None  # Only sent with multiplexer == mux_value
    mux_value: Optional[int] = None


@dataclass
class IdAnalysis:
    key: IdKey
    frames: int
    length: int
    fields: List[FoundField]
    multiplexer: Optional[FoundField] = None
    note: str = ''


class _Data:
    """Payloads of one length as integers in both byte orders, for extracting fields quickly."""

    def __init__(self, payloads: Sequence[bytes]):
        self.length = len(payloads[0]) if payloads else 0
        self.size = 8 * self.length
        self.little = [int.from_bytes(payload, 'little') for payload in payloads]
        self.big = [int.from_bytes(payload, 'big') for payload in payloads]
        self.payloads = list(payloads)

    def subset(self, indices: Sequence[int]) -> '_Data':
        return _Data([self.payloads[index] for index in indices])

    def __len__(self) -> int:
        return len(self.little)

    def raw(self, field: BitField) -> List[int]:
        """Unsigned raw values of a field."""
        mask = (1 << field.length) - 1
        if field.byte_order == 'little_endian':
            shift = field.start
            return [(value >> shift) & mask for value in self.little]
        shift = self.size - _motorola_index(field.start) - field.length
        return [(value >> shift) & mask for value in self.big]

    def rates(self, mask: int = -1) -> List[float]:
        """Per bit, the share of frame-to-frame transitions changing it."""
        counts = [0] * self.size
        for previous, value in zip(self.little, self.little[1:], strict=False):
            difference = (previous ^ value) & mask
            while difference:
                lowest = difference & -difference
                counts[lowest.bit_length() - 1] += 1
                difference ^= lowest
        transitions = max(1, len(self.little) - 1)
        return [count / transitions for count in counts]


def _signed(values: List[int], length: int) -> List[int]:
    modulus, sign = 1 << length, 1 << (length - 1)
    return [value - modulus if value & sign else value for value in values]


def _smoothness(values: List[int]) -> float:
    """Mean change from frame to frame relative to the range of the values (0: constant or perfectly smooth)."""
    low, high = min(values), max(values)
    if high == low or len(values) < 2:
        return 0.0
    total = sum(abs(b - a) for a, b in zip(values, values[1:], strict=False))
    return total / (len(values) - 1) / (high - low)


def _just_below_zero(raw: List[int], length: int) -> bool:
    """Values all in the top quarter of the unsigned range: more likely small negative numbers."""
    return length >= 4 and min(raw) >= 3 << (length - 2)


def _variation(values: List[int]) -> int:
    return sum(abs(b - a) for a, b in zip(values, values[1:], strict=False))


def _most_common(values: Iterable[int]) -> Tuple[int, int]:
    counts: Dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda item: item[1]) if counts else (0, 0)


def _field(key: IdKey, bits: List[int], byte_order: str) -> BitField:
    """The field of bits listed from the most to the least significant one; fields within a byte read the same
    in both orders and are given as Intel fields."""
    if byte_order == 'little_endian' or len({bit // 8 for bit in bits}) == 1:
        return BitField(key[0], key[1], min(bits), len(bits))
    return BitField(key[0], key[1], bits[0], len(bits), 'big_endian')


def _orders(size: int) -> Dict[str, List[int]]:
    """All bits from the most to the least significant one, for a field covering the whole payload."""
    return {
        'little_endian': list(range(size - 1, -1, -1)),
        'big_endian': [_from_motorola_index(index) for index in range(size)],
    }


def _segments(order: List[int], rates: List[float], excluded: Set[int]) -> List[List[int]]:
    segments: List[List[int]] = []
    current: List[int] = []
    for bit in order:
        rate = rates[bit]
        if rate == 0 or bit in excluded:
            if current:
                segments.append(current)
                current = []
            continue
        if current and rate < rates[current[-1]] * DROP:
            segments.append(current)
            current = []
        current.append(bit)
    if current:
        segments.append(current)
    return segments


def _coupling(upper: List[int], lower: List[int], upper_length: int, lower_length: int) -> Optional[float]:
    """Share of the changes of the upper part that come with the lower part wrapping around.

    None when it cannot be told: the upper part changes too rarely, or the lower part changes too much from frame
    to frame (e.g. the noisy low byte of an engine speed) for a wrap to be recognised.
    """
    half = 1 << (lower_length - 1)
    lower_modulus = 1 << lower_length
    steps = sorted(
        min(step, lower_modulus - step)
        for step in ((b - a) % lower_modulus for a, b in zip(lower, lower[1:], strict=False))
    )
    if not steps or steps[len(steps) // 2] > lower_modulus // 8:
        return None
    modulus = 1 << upper_length
    changes = wraps = 0
    for index in range(1, len(upper)):
        step = (upper[index] - upper[index - 1]) % modulus
        if step == 0:
            continue
        changes += 1
        rising = step < modulus // 2 if upper_length > 1 else upper[index] > upper[index - 1]
        before, after = lower[index - 1], lower[index]
        if rising and before >= half > after or not rising and before < half <= after:
            wraps += 1
    return wraps / changes if changes >= MIN_UPPER_CHANGES else None


def _split(data: _Data, key: IdKey, bits: List[int], byte_order: str) -> List[List[int]]:
    """Split a segment at the nibble or byte boundary where its parts are least coupled, recursively."""
    best: Optional[Tuple[float, int]] = None
    for index in range(1, len(bits)):
        if bits[index - 1] // 4 == bits[index] // 4:
            continue
        upper, lower = _field(key, bits[:index], byte_order), _field(key, bits[index:], byte_order)
        coupling = _coupling(data.raw(upper), data.raw(lower), upper.length, lower.length)
        if coupling is not None and coupling < COUPLING and (best is None or coupling < best[0]):
            best = (coupling, index)
    if best is None:
        return [bits]
    index = best[1]
    return _split(data, key, bits[:index], byte_order) + _split(data, key, bits[index:], byte_order)


def _split_independent(data: _Data, key: IdKey, bits: List[int], byte_order: str) -> List[List[int]]:
    """Split a rarely changing segment (flags, enumerations) where its parts (almost) never change in the same
    frame."""
    if len(bits) < 2:
        return [bits]
    raw = data.raw(_field(key, bits, byte_order))
    if sum(1 for a, b in zip(raw, raw[1:], strict=False) if a != b) > ENUM_CHANGES * (len(raw) - 1):
        return [bits]
    for index in range(1, len(bits)):
        upper = data.raw(_field(key, bits[:index], byte_order))
        lower = data.raw(_field(key, bits[index:], byte_order))
        upper_changes = lower_changes = together = 0
        for position in range(1, len(upper)):
            upper_changed = upper[position] != upper[position - 1]
            lower_changed = lower[position] != lower[position - 1]
            upper_changes += upper_changed
            lower_changes += lower_changed
            together += upper_changed and lower_changed
        # Independent events coincide now and then; the bits of one enumeration change together often
        if upper_changes >= 3 and lower_changes >= 3 and together <= 0.1 * min(upper_changes, lower_changes):
            return _split_independent(data, key, bits[:index], byte_order) + _split_independent(
                data, key, bits[index:], byte_order
            )
    return [bits]


def classify(raw: List[int], field: BitField) -> FoundField:
    """The kind of a field from its unsigned raw values."""
    distinct = len(set(raw))
    if distinct == 1:
        return FoundField(field, 'constant', 1.0, f'always {raw[0]} (0x{raw[0]:X})', raw[0], raw[0], 1)
    length = field.length
    modulus = 1 << length
    transitions = max(1, len(raw) - 1)
    steps = [(b - a) % modulus for a, b in zip(raw, raw[1:], strict=False)]
    changes = sum(1 for step in steps if step)
    step, count = _most_common(steps)
    # Alive counters run through (most of) their whole range, unlike a value rising steadily for a while
    if length >= 2 and step and count / transitions >= COUNTER_SHARE and distinct >= 0.75 * modulus:
        signed_step = step - modulus if step > modulus // 2 else step
        details = f'counter, step {signed_step:+d}' + (f', 0–{modulus - 1}' if distinct == modulus else '')
        return FoundField(field, 'counter', count / transitions, details, min(raw), max(raw), distinct)
    if length == 1:
        return FoundField(field, 'flag', 0.6, f'changes in {changes / transitions:.1%} of the frames', 0, 1, 2)
    if distinct <= ENUM_VALUES and changes / transitions < ENUM_CHANGES:
        return FoundField(field, 'enum', 0.5, f'{distinct} values, rarely changing', min(raw), max(raw), distinct)
    # Signed when that makes the changes from frame to frame much smaller (no jumps where the value crosses 0)
    signed_values = _signed(raw, length)
    signed = _variation(signed_values) < 0.8 * _variation(raw) or _just_below_zero(raw, length)
    values = signed_values if signed else raw
    smoothness = _smoothness(values)
    if signed:
        field = BitField(
            field.frame_id, field.extended, field.start, field.length, field.byte_order, True, field.scale, field.offset
        )
    low, high = min(values), max(values)
    if smoothness < SMOOTH and distinct > 2:
        confidence = 1.0 - 0.5 * smoothness / SMOOTH
        details = f'changes smoothly, {low}…{high}' + (' (signed)' if signed else '')
        return FoundField(field, 'physical', confidence, details, low, high, distinct)
    if distinct <= ENUM_VALUES:
        return FoundField(field, 'enum', 0.4, f'{distinct} values', min(raw), max(raw), distinct)
    return FoundField(field, 'unknown', 0.2, 'changes irregularly: noise or several fields', low, high, distinct)


def _value(found: FoundField) -> float:
    weight = found.confidence if found.kind == 'physical' else _WEIGHT.get(found.kind, 0.0)
    return weight * found.field.length


def _neutral(found: FoundField) -> bool:
    """Fields within a byte read the same in both bit orders."""
    return len({bit // 8 for bit in found.field.bits}) == 1


def _select(candidates: List[FoundField], preferred: Optional[str]) -> List[FoundField]:
    """Non-overlapping fields, the most valuable first; those in the preferred bit order before the others."""

    def rank(found: FoundField):
        other_order = preferred is not None and not _neutral(found) and found.field.byte_order != preferred
        return other_order, -_value(found)

    chosen: List[FoundField] = []
    used: Set[int] = set()
    for found in sorted(candidates, key=rank):
        bits = set(found.field.bits)
        if found.kind != 'constant' and not bits & used:
            chosen.append(found)
            used |= bits
    return chosen


def _find_fields(data: _Data, key: IdKey, excluded: Set[int]) -> List[FoundField]:
    """Fields of the changing bits outside `excluded`, from both bit orders.

    A message normally uses one bit order, so the fields are chosen preferring Intel, preferring Motorola and
    without preference, and the choice with the most valuable fields per bit is kept.
    """
    mask = (1 << data.size) - 1
    for bit in excluded:
        mask &= ~(1 << bit)
    rates = data.rates(mask)
    candidates: Dict[Tuple[int, ...], FoundField] = {}
    for byte_order, order in _orders(data.size).items():
        for segment in _segments(order, rates, excluded):
            parts = [
                part
                for bits in _split(data, key, segment, byte_order)
                for part in _split_independent(data, key, bits, byte_order)
            ]
            for bits in parts:
                field = _field(key, bits, byte_order)
                identity = tuple(field.bits)  # Differs between the bit orders unless within a byte
                if identity not in candidates:
                    candidates[identity] = classify(data.raw(field), field)
    choices = [_select(list(candidates.values()), preferred) for preferred in ('little_endian', 'big_endian', None)]
    return max(choices, key=lambda chosen: sum(_value(found) for found in chosen))


def _constants(data: _Data, key: IdKey, covered: Set[int]) -> List[FoundField]:
    """Runs of bits that never change (outside other fields), of up to 64 bits."""
    rates = data.rates()
    result = []
    run: List[int] = []
    for bit in list(range(data.size)) + [-1]:
        if bit >= 0 and rates[bit] == 0 and bit not in covered and len(run) < 64:
            run.append(bit)
            continue
        if run:
            field = BitField(key[0], key[1], run[0], len(run))
            result.append(classify(data.raw(field)[:1], field))
            run = [bit] if bit >= 0 and rates[bit] == 0 and bit not in covered else []
    return result


def _multiplexer(data: _Data, key: IdKey, excluded: Set[int]) -> Optional[Tuple[BitField, Dict[int, List[int]]]]:
    """A nibble or byte grouping the frames by which makes the other bits change much less (see the module)."""
    best: Optional[Tuple[float, BitField, Dict[int, List[int]]]] = None
    overall = data.rates()
    for length in (4, 8):
        for start in range(0, data.size, length):
            candidate = BitField(key[0], key[1], start, length)
            if set(candidate.bits) & excluded:
                continue
            raw = data.raw(candidate)
            groups: Dict[int, List[int]] = {}
            for index, value in enumerate(raw):
                groups.setdefault(value, []).append(index)
            if not 2 <= len(groups) <= 64:
                continue
            changes = sum(1 for a, b in zip(raw, raw[1:], strict=False) if a != b)
            if changes < 0.3 * (len(raw) - 1):
                continue
            others = [bit for bit in range(data.size) if bit not in excluded and bit not in candidate.bits]
            within = [0] * data.size
            transitions = 0
            little = data.little
            for indices in groups.values():
                if len(indices) < MUX_MIN_GROUP:
                    continue
                transitions += len(indices) - 1
                for a, b in zip(indices, indices[1:], strict=False):
                    difference = little[a] ^ little[b]
                    while difference:
                        lowest = difference & -difference
                        within[lowest.bit_length() - 1] += 1
                        difference ^= lowest
            if transitions == 0:
                continue
            # Bits changing at random within the groups too (e.g. the low byte of a fast signal) tell nothing
            informative = [bit for bit in others if overall[bit] > 0.05 and within[bit] / transitions < 0.4]
            if not informative:
                continue
            calm = [bit for bit in informative if within[bit] / transitions < 0.5 * overall[bit]]
            share = len(calm) / len(informative)
            # Multiplexers take few values; a value's upper bits can group the frames similarly with many
            score = share - 0.01 * length - 0.02 * len(groups)
            if share >= MUX_CALM and len(calm) >= MUX_MIN_CALM_BITS and (best is None or score > best[0]):
                best = (score, candidate, groups)
    return (best[1], best[2]) if best is not None else None


def analyse(frames: IdFrames, multiplexers: bool = True) -> IdAnalysis:
    """Find the fields of the frames of one identifier (those of the most common length)."""
    key = frames.key
    if not frames.payloads:
        return IdAnalysis(key, 0, 0, [], note='No frames')
    length, _ = _most_common(len(payload) for payload in frames.payloads)
    payloads = [payload for payload in frames.payloads if len(payload) == length]
    if length == 0:
        return IdAnalysis(key, len(payloads), 0, [], note='No data bytes')
    if len(payloads) < MIN_FRAMES:
        return IdAnalysis(key, len(payloads), length, [], note=f'Too few frames ({len(payloads)}, {MIN_FRAMES} needed)')
    data = _Data(payloads)
    fields: List[FoundField] = []
    excluded: Set[int] = set()
    for match in find_checksums(payloads):
        field = BitField(key[0], key[1], match.byte * 8, 8)
        raw = data.raw(field)
        fields.append(FoundField(field, 'checksum', match.share, match.description, min(raw), max(raw), len(set(raw))))
        excluded |= set(field.bits)

    multiplexer = None
    found_mux = _multiplexer(data, key, excluded) if multiplexers else None
    if found_mux is None:
        fields += _find_fields(data, key, excluded)
    else:
        mux_field, groups = found_mux
        # Without its most significant bits that never change (a nibble with values 0-2 is a 2 bit multiplexer)
        used = max(groups).bit_length() or 1
        mux_field = BitField(key[0], key[1], mux_field.start, min(mux_field.length, used))
        values = sorted(groups)
        shown = ', '.join(str(value) for value in values[:8]) + (', …' if len(values) > 8 else '')
        multiplexer = FoundField(
            mux_field, 'multiplexer', 0.8, f'{len(values)} values: {shown}', values[0], values[-1], len(values)
        )
        fields.append(multiplexer)
        per_value: Dict[int, List[FoundField]] = {}
        for value, indices in groups.items():
            if len(indices) >= MIN_FRAMES:
                per_value[value] = _find_fields(data.subset(indices), key, excluded | set(mux_field.bits))
        # Fields found alike for every multiplexer value, and still alike in all frames together, are not
        # multiplexed (several cell voltages at the same bits are)
        common = None
        for found in per_value.values():
            identities = {(item.field, item.kind) for item in found}
            common = identities if common is None else common & identities
        common = {
            identity for identity in common or set() if classify(data.raw(identity[0]), identity[0]).kind == identity[1]
        }
        added = set()
        for value in sorted(per_value):
            for item in per_value[value]:
                if (item.field, item.kind) in common:
                    if (item.field, item.kind) not in added:
                        fields.append(item)
                        added.add((item.field, item.kind))
                    continue
                fields.append(
                    FoundField(
                        item.field,
                        item.kind,
                        item.confidence,
                        item.details,
                        item.minimum,
                        item.maximum,
                        item.distinct,
                        multiplexer=mux_field,
                        mux_value=value,
                    )
                )
    covered = {bit for item in fields for bit in item.field.bits}
    fields += _constants(data, key, covered)
    fields.sort(key=lambda item: (item.mux_value is not None, item.mux_value or 0, min(item.field.bits)))
    return IdAnalysis(key, len(payloads), length, fields, multiplexer)


def analyse_all(
    frames: Dict[IdKey, IdFrames], progress: Optional[Callable[[int, int], None]] = None
) -> List[IdAnalysis]:
    """Analyse every identifier, in the order of their identifiers; `progress(done, total)` after each."""
    keys = sorted(frames, key=lambda key: (key[1], key[0]))
    result = []
    for index, key in enumerate(keys):
        result.append(analyse(frames[key]))
        if progress is not None:
            progress(index + 1, len(keys))
    return result
