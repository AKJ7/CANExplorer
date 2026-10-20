"""Finding the bits that change when something happens: the user marks the moments (e.g. each time a button is
pressed), and bits changing shortly around most markers but rarely otherwise are ranked first.

For each bit: hits = markers with a change of the bit within [marker - before, marker + after]; its background rate
= changes outside these windows per second, giving the chance that a window catches a change by coincidence. The
score is the share of markers hit minus that chance. Neighbouring bits of an identifier hit by the same markers
are joined into a field, with the values it changed between.
"""

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from can_explorer.core.reverse.analysis import IdFrames, IdKey
from can_explorer.core.reverse.fields import BitField

MIN_SCORE = 0.4


@dataclass(frozen=True)
class Marker:
    time: float
    label: str = ''


@dataclass(frozen=True)
class MarkerMatch:
    field: BitField
    hits: int
    markers: int
    background: float  # Changes per second outside the marker windows
    score: float
    transitions: Tuple[str, ...]  # E.g. ('0 → 1', '1 → 0')


def _windows(markers: Sequence[Marker], before: float, after: float) -> List[Tuple[float, float]]:
    return [(marker.time - before, marker.time + after) for marker in markers]


def _inside(time: float, windows: List[Tuple[float, float]]) -> bool:
    return any(start <= time <= end for start, end in windows)


def _value_before(item: IdFrames, field: BitField, time: float):
    index = bisect_right(item.times, time) - 1
    return field.raw(item.payloads[index]) if index >= 0 else None


def find_marker_changes(
    frames: Dict[IdKey, IdFrames],
    markers: Sequence[Marker],
    before: float = 0.5,
    after: float = 1.5,
    top: int = 30,
) -> List[MarkerMatch]:
    """Fields changing around the markers, the most specific first."""
    if not markers:
        return []
    windows = _windows(markers, before, after)
    window_length = before + after
    needed = max(1, math.ceil(0.5 * len(markers)))
    matches = []
    for key, item in frames.items():
        if len(item.times) < 2:
            continue
        duration = max(1e-3, item.times[-1] - item.times[0])
        outside_duration = max(window_length, duration - len(markers) * window_length)
        changes: Dict[int, List[float]] = {}
        previous = int.from_bytes(item.payloads[0], 'little')
        for time, payload in zip(item.times[1:], item.payloads[1:], strict=True):
            value = int.from_bytes(payload, 'little')
            difference = value ^ previous
            previous = value
            while difference:
                lowest = difference & -difference
                changes.setdefault(lowest.bit_length() - 1, []).append(time)
                difference ^= lowest
        hit_bits: Dict[int, Tuple[frozenset, float, float]] = {}
        for bit, times in changes.items():
            hit = frozenset(
                index
                for index, (start, end) in enumerate(windows)
                if bisect_right(times, end) > bisect_left(times, start)
            )
            if len(hit) < needed:
                continue
            outside = sum(1 for time in times if not _inside(time, windows))
            rate = outside / outside_duration
            chance = 1 - math.exp(-rate * window_length)
            score = len(hit) / len(markers) - chance
            if score >= MIN_SCORE:
                hit_bits[bit] = (hit, rate, score)
        # Neighbouring bits hit by the same markers form one field (Intel order)
        bits = sorted(hit_bits)
        groups: List[List[int]] = []
        for bit in bits:
            if groups and groups[-1][-1] == bit - 1 and hit_bits[groups[-1][-1]][0] == hit_bits[bit][0]:
                groups[-1].append(bit)
            else:
                groups.append([bit])
        for group in groups:
            for start in range(0, len(group), 64):
                part = group[start : start + 64]
                field = BitField(key[0], key[1], part[0], len(part))
                hit, _, _ = hit_bits[part[0]]
                rate = max(hit_bits[bit][1] for bit in part)
                score = min(hit_bits[bit][2] for bit in part)
                transitions = []
                for index in sorted(hit):
                    window_start, window_end = windows[index]
                    old = _value_before(item, field, window_start)
                    new = _value_before(item, field, window_end)
                    text = f'{old} → {new}'
                    if old is not None and new is not None and old != new and text not in transitions:
                        transitions.append(text)
                matches.append(MarkerMatch(field, len(hit), len(markers), rate, score, tuple(transitions)))
    matches.sort(key=lambda match: (-match.score, -match.hits, match.field.frame_id, match.field.start))
    return matches[:top]
