"""Stimulus–response diffing: compare a baseline window of the bus (you did nothing) with an action window (you
did something) and report what reacted — identifiers that appeared, and bits that were stable during the baseline
but changed during the action, grouped into fields with their before/after values.

This is the "I did X, what changed?" loop. Unlike markers.py (which correlates many repeated marker windows), this
is a single baseline-vs-action comparison, scored simply by "changed during the action, stable otherwise". It is
passive: it only reads captured frames, it never transmits.
"""

from dataclasses import dataclass, field as dataclass_field
from typing import Dict, Iterable, List, Optional, Set, Tuple

import can

from can_explorer.core.reverse.analysis import IdFrames, IdKey, collect
from can_explorer.core.reverse.fields import BitField

MAX_FIELD_BITS = 64


@dataclass(frozen=True)
class FieldReaction:
    """A contiguous run of bits that reacted, with the value it held before and after the action."""

    field: BitField
    before: Optional[int]  # Raw value at the end of the baseline (None when the identifier is new)
    after: int  # Raw value at the end of the action
    action_flips: int  # Transitions of the run during the action


@dataclass(frozen=True)
class IdReaction:
    key: IdKey
    appeared: bool  # Seen during the action but not the baseline
    baseline_frames: int
    action_frames: int
    fields: List[FieldReaction] = dataclass_field(default_factory=list)

    @property
    def reacting_bits(self) -> int:
        return sum(item.field.length for item in self.fields)

    @property
    def score(self) -> float:
        """Higher for a bigger, more active reaction; brand new identifiers rank at the top."""
        activity = self.reacting_bits * 10 + sum(item.action_flips for item in self.fields)
        return (100_000 if self.appeared else 0) + activity

    @property
    def id_text(self) -> str:
        return f'{self.key[0]:08X}' if self.key[1] else f'{self.key[0]:03X}'


@dataclass
class ReactionReport:
    reactions: List[IdReaction]
    baseline_frames: int
    action_frames: int


def _bit_flips(payloads: List[bytes]) -> Dict[int, int]:
    """Per bit, how many times it changed from one frame to the next (only bits that changed appear)."""
    flips: Dict[int, int] = {}
    previous = None
    for payload in payloads:
        value = int.from_bytes(payload, 'little')
        if previous is not None:
            difference = value ^ previous
            while difference:
                lowest = difference & -difference
                bit = lowest.bit_length() - 1
                flips[bit] = flips.get(bit, 0) + 1
                difference ^= lowest
        previous = value
    return flips


def _bit(payload: bytes, bit: int) -> int:
    byte = bit // 8
    return (payload[byte] >> (bit % 8)) & 1 if byte < len(payload) else 0


def _run_value(payload: bytes, start: int, length: int) -> int:
    value = 0
    for offset in range(length):
        value |= _bit(payload, start + offset) << offset
    return value


def _group_runs(bits: List[int]) -> List[Tuple[int, int]]:
    """Contiguous ascending bit indices as (start, length) runs, split at 64 bits."""
    runs: List[Tuple[int, int]] = []
    for bit in sorted(bits):
        if runs and runs[-1][0] + runs[-1][1] == bit and runs[-1][1] < MAX_FIELD_BITS:
            start, length = runs[-1]
            runs[-1] = (start, length + 1)
        else:
            runs.append((bit, 1))
    return runs


def _reaction(key: IdKey, baseline: Optional[IdFrames], action: IdFrames) -> Optional[IdReaction]:
    appeared = baseline is None or not baseline.payloads
    length = max((len(payload) for payload in action.payloads), default=0)
    if length == 0:
        return IdReaction(key, appeared, 0 if baseline is None else len(baseline.payloads), len(action.payloads))
    base_flips = {} if appeared else _bit_flips(baseline.payloads)
    base_last = None if appeared else baseline.payloads[-1]
    action_flips = _bit_flips(action.payloads)
    action_last = action.payloads[-1]

    reacting: List[int] = []
    for bit in range(length * 8):
        baseline_stable = base_flips.get(bit, 0) == 0
        if not baseline_stable:
            continue  # It was already moving on its own
        before = _bit(base_last, bit) if base_last is not None else None
        changed = action_flips.get(bit, 0) > 0 or (before is not None and _bit(action_last, bit) != before)
        if changed:
            reacting.append(bit)

    fields = []
    for start, run_length in _group_runs(reacting):
        run_flips = max((action_flips.get(start + offset, 0) for offset in range(run_length)), default=0)
        field = BitField(key[0], key[1], start, run_length)
        before = _run_value(base_last, start, run_length) if base_last is not None else None
        fields.append(FieldReaction(field, before, _run_value(action_last, start, run_length), run_flips))

    if not appeared and not fields:
        return None  # Present before and after, nothing changed
    return IdReaction(key, appeared, 0 if baseline is None else len(baseline.payloads), len(action.payloads), fields)


def capture_reaction(
    baseline: Iterable[can.Message],
    action: Iterable[can.Message],
    exclude: Optional[Set[IdKey]] = None,
    top: int = 50,
) -> ReactionReport:
    """Diff a baseline window against an action window; the most reactive identifiers first."""
    base = collect(baseline, exclude=exclude)
    act = collect(action, exclude=exclude)
    reactions = []
    for key, frames in act.items():
        reaction = _reaction(key, base.get(key), frames)
        if reaction is not None:
            reactions.append(reaction)
    reactions.sort(key=lambda reaction: (-reaction.score, reaction.key[1], reaction.key[0]))
    base_total = sum(len(item.payloads) for item in base.values())
    act_total = sum(len(item.payloads) for item in act.values())
    return ReactionReport(reactions[:top], base_total, act_total)
