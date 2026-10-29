"""Temporal "flow" playback of one identifier's frames: the payload at any point in time, for a scrubber.

Given the frames of an identifier (timestamps and payloads), it answers what the bus carried at a scrub time —
the payload then, and when each bit last changed up to that moment — so a view can play the capture back and show
the bits flowing and flipping, or jump to any instant with a slider.

Scrubbing and playback are by time, so a frame shown stays on screen until the next one, exactly as on the bus.
"""

from bisect import bisect_right
from typing import Dict, List, Optional, Sequence


class IdTimeline:
    """The frames of one identifier over time."""

    def __init__(self, times: Sequence[float], payloads: Sequence[bytes]):
        if len(times) != len(payloads):
            raise ValueError('times and payloads must have the same length')
        self.times: List[float] = list(times)
        self.payloads: List[bytes] = [bytes(payload) for payload in payloads]
        self.length = max((len(payload) for payload in self.payloads), default=0)
        # Per bit, the ascending frame indices at which it changed from the previous frame (for the fade/flow)
        self._changes: Dict[int, List[int]] = {}
        previous = 0
        for index, payload in enumerate(self.payloads):
            value = int.from_bytes(payload, 'little')
            if index:
                difference = value ^ previous
                while difference:
                    lowest = difference & -difference
                    self._changes.setdefault(lowest.bit_length() - 1, []).append(index)
                    difference ^= lowest
            previous = value

    def __len__(self) -> int:
        return len(self.times)

    @property
    def start(self) -> float:
        return self.times[0] if self.times else 0.0

    @property
    def end(self) -> float:
        return self.times[-1] if self.times else 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def index_at(self, time: float) -> int:
        """The index of the last frame at or before `time` (0 before the first frame, the last index after it)."""
        if not self.times:
            return -1
        return max(0, bisect_right(self.times, time) - 1)

    def time_at(self, index: int) -> float:
        return self.times[index] if 0 <= index < len(self.times) else self.start

    def payload_at(self, index: int) -> bytes:
        return self.payloads[index] if 0 <= index < len(self.payloads) else b''

    def bit_value(self, index: int, bit: int) -> int:
        payload = self.payload_at(index)
        byte = bit // 8
        return (payload[byte] >> (bit % 8)) & 1 if byte < len(payload) else 0

    def last_change_time(self, index: int, bit: int) -> Optional[float]:
        """The time this bit last changed at or before `index`, or None if it never changed up to then."""
        changes = self._changes.get(bit)
        if not changes:
            return None
        position = bisect_right(changes, index) - 1
        return self.times[changes[position]] if position >= 0 else None

    def changed_bits(self, index: int) -> List[int]:
        """The bits that changed into the frame at `index` (empty for the first frame)."""
        if index <= 0 or index >= len(self.payloads):
            return []
        difference = int.from_bytes(self.payloads[index], 'little') ^ int.from_bytes(self.payloads[index - 1], 'little')
        bits = []
        while difference:
            lowest = difference & -difference
            bits.append(lowest.bit_length() - 1)
            difference ^= lowest
        return bits
