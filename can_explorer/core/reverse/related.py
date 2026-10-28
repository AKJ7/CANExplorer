"""Finding fields that carry the same signal across the capture, without any external reference.

Each value or state field found by the analysis is turned into a reference from its own samples, and the other
fields of the trace are correlated with it (reusing the reference-correlation machinery: aligned sampling, Pearson
correlation, a linear fit and a time-lag search). Fields that follow one another — the same quantity rebroadcast on
another message or bus, or a scaled or lagged copy of it — are grouped. Within each group every member's factor,
offset, lag and correlation are given relative to the group's representative.
"""

from dataclasses import dataclass, field as dataclass_field
from typing import Dict, List, Optional, Sequence, Tuple

from can_explorer.core.reverse.analysis import IdAnalysis, IdFrames, IdKey
from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.reference import Candidate, Reference, _Sampler, candidates, correlate

MIN_CORRELATION = 0.99  # Fields below this are not considered the same signal
MAX_SOURCES = 60  # Value/state fields used as references, strongest first (keeps the pass bounded)
SOURCE_KINDS = ('physical', 'enum')

_Identity = Tuple[int, bool, Tuple[int, ...], Optional[int]]


@dataclass(frozen=True)
class GroupMember:
    field: BitField  # Factor and offset convert its raw value to the representative's
    correlation: float
    lag: float  # Seconds this member lags behind the representative
    multiplexer: Optional[BitField] = None
    mux_value: Optional[int] = None
    representative: bool = False


@dataclass
class SignalGroup:
    members: List[GroupMember] = dataclass_field(default_factory=list)

    @property
    def correlation(self) -> float:
        return max((abs(member.correlation) for member in self.members if not member.representative), default=0.0)


def _identity(field: BitField, mux_value: Optional[int]) -> _Identity:
    return field.frame_id, field.extended, tuple(sorted(field.bits)), mux_value


def _raw(field: BitField) -> BitField:
    """The same bits without a conversion, so fields are compared and grouped by their bits, not their fit."""
    return BitField(field.frame_id, field.extended, field.start, field.length, field.byte_order, field.signed)


def reference_from_field(
    item: IdFrames,
    field: BitField,
    multiplexer: Optional[BitField] = None,
    mux_value: Optional[int] = None,
    name: str = '',
) -> Reference:
    """A reference built from a field's own raw values over time, to correlate the other fields with it."""
    sampler = _Sampler(item, multiplexer, mux_value)
    values = sampler.values(field, list(range(len(sampler.times))))
    reference = Reference(name or field.key, exclude={(field.frame_id, field.extended)})
    for time, value in zip(sampler.times, values, strict=True):
        if value is not None:
            reference.times.append(time)
            reference.values.append(float(value))
    return reference


class _Union:
    def __init__(self):
        self._parent: Dict[_Identity, _Identity] = {}

    def find(self, item: _Identity) -> _Identity:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: _Identity, b: _Identity) -> None:
        self._parent[self.find(a)] = self.find(b)


def signal_groups(
    frames: Dict[IdKey, IdFrames],
    analyses: Sequence[IdAnalysis],
    min_correlation: float = MIN_CORRELATION,
    max_lag: float = 0.5,
    max_sources: int = MAX_SOURCES,
    top: int = 20,
) -> List[SignalGroup]:
    """Groups of fields that carry the same signal across the trace, most-correlated and largest groups first."""
    sources: List[Tuple[float, BitField, Optional[BitField], Optional[int]]] = []
    fields: Dict[_Identity, Tuple[BitField, Optional[BitField], Optional[int]]] = {}
    for analysis in analyses:
        for found in analysis.fields:
            if found.kind in SOURCE_KINDS:
                sources.append((found.confidence, found.field, found.multiplexer, found.mux_value))
                fields[_identity(found.field, found.mux_value)] = (
                    _raw(found.field),
                    found.multiplexer,
                    found.mux_value,
                )
    sources.sort(key=lambda item: -item[0])
    sources = sources[:max_sources]
    if not sources:
        return []

    pool = candidates(frames, analyses)
    union = _Union()
    for _, field, multiplexer, mux_value in sources:
        key = (field.frame_id, field.extended)
        if key not in frames:
            continue
        reference = reference_from_field(frames[key], field, multiplexer, mux_value)
        if len(reference) == 0:
            continue
        source_identity = _identity(field, mux_value)
        for match in correlate(reference, frames, pool, max_lag=max_lag, top=top):
            if abs(match.correlation) < min_correlation:
                continue
            identity = _identity(match.field, match.mux_value)
            fields.setdefault(identity, (_raw(match.field), match.multiplexer, match.mux_value))
            union.union(source_identity, identity)

    components: Dict[_Identity, List[_Identity]] = {}
    for identity in fields:
        components.setdefault(union.find(identity), []).append(identity)

    groups: List[SignalGroup] = []
    for member_identities in components.values():
        if len(member_identities) < 2:
            continue
        group = _build_group(member_identities, fields, frames, max_lag)
        if group is not None:
            groups.append(group)
    groups.sort(key=lambda group: (-len(group.members), -group.correlation))
    return groups


def _build_group(
    member_identities: Sequence[_Identity],
    fields: Dict[_Identity, Tuple[BitField, Optional[BitField], Optional[int]]],
    frames: Dict[IdKey, IdFrames],
    max_lag: float,
) -> Optional[SignalGroup]:
    """Describe a component relative to one representative: each member's factor, offset, lag and correlation."""
    # The representative is the member with the most frames, so the fit of the others against it is well sampled
    representative = max(
        member_identities,
        key=lambda identity: len(frames.get((identity[0], identity[1]), IdFrames((identity[0], identity[1]))).times),
    )
    rep_field, rep_multiplexer, rep_mux = fields[representative]
    key = (rep_field.frame_id, rep_field.extended)
    if key not in frames:
        return None
    reference = reference_from_field(frames[key], rep_field, rep_multiplexer, rep_mux)
    others = [
        Candidate(fields[identity][0], fields[identity][1], fields[identity][2])
        for identity in member_identities
        if identity != representative
    ]
    representative_member = GroupMember(rep_field, 1.0, 0.0, rep_multiplexer, rep_mux, representative=True)
    # One member per identifier: overlapping candidate fields (8/16 bit, signed, analysis fields) of the same
    # message all correlate, so keep only the best-correlated, longest one of each.
    best: Dict[Tuple[int, bool, Optional[int]], GroupMember] = {}
    for match in correlate(reference, frames, others, max_lag=max_lag, top=len(others) or 1):
        member = GroupMember(match.field, match.correlation, match.lag, match.multiplexer, match.mux_value)
        key = (match.field.frame_id, match.field.extended, match.mux_value)
        current = best.get(key)
        if current is None or (abs(member.correlation), member.field.length) > (
            abs(current.correlation),
            current.field.length,
        ):
            best[key] = member
    members = [representative_member] + sorted(best.values(), key=lambda member: -abs(member.correlation))
    if len(members) < 2:
        return None
    return SignalGroup(members)
