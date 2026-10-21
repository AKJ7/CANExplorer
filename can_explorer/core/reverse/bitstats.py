"""Statistics of the payload bits and bytes of each identifier, for finding signals in unknown traffic.

Per identifier: how often each bit is set and how often it changed from one frame to the next, when it last
changed, and a histogram of the values of each byte. A notch masks every bit that changed so far, so that bits
changing afterwards (e.g. after pressing a button in the vehicle) stand out.

Updating costs one histogram increment per byte plus one step per changed bit, so the statistics can follow live
traffic.
"""

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import can

IdKey = Tuple[int, bool]  # (arbitration_id, is_extended_id)


@dataclass(frozen=True, slots=True)
class BitInfo:
    bit: int
    value: int  # In the last frame
    ones: float  # Share of the frames with the bit set
    flips: float  # Share of the frame-to-frame transitions that changed the bit
    changed_at: Optional[float]  # Timestamp of its last change
    masked: bool  # Changed before the notch

    @property
    def constant(self) -> bool:
        return self.flips == 0


@dataclass(frozen=True, slots=True)
class ByteInfo:
    index: int
    value: Optional[int]  # In the last frame (None when it was shorter)
    distinct: int
    minimum: int
    maximum: int
    entropy: float  # Bits, 0 to 8
    histogram: Tuple[int, ...]  # Frames per value 0-255


class IdBitStatistics:
    """The statistics of one identifier."""

    __slots__ = ('count', 'first_time', 'last_time', 'last', 'histograms', 'flips', 'changed_at', 'changed', 'mask')

    def __init__(self):
        self.count = 0
        self.first_time = 0.0
        self.last_time = 0.0
        self.last = b''
        self.histograms: List[List[int]] = []  # Per byte, frames per value
        self.flips: List[int] = []  # Per bit
        self.changed_at: List[float] = []  # Per bit, -inf before the first change
        self.changed = 0  # Bits that changed at least once
        self.mask = 0  # Bits masked by the notch

    @property
    def length(self) -> int:
        """Bytes of the longest frame."""
        return len(self.histograms)

    def update(self, timestamp: float, data: bytes) -> None:
        length = len(data)
        if length > len(self.histograms):
            for _ in range(len(self.histograms), length):
                self.histograms.append([0] * 256)
            self.flips += [0] * (8 * length - len(self.flips))
            self.changed_at += [-math.inf] * (8 * length - len(self.changed_at))
        histograms = self.histograms
        for index, byte in enumerate(data):
            histograms[index][byte] += 1
        if self.count:
            difference = int.from_bytes(data, 'little') ^ int.from_bytes(self.last, 'little')
            if difference:
                self.changed |= difference
                flips, changed_at = self.flips, self.changed_at
                while difference:
                    lowest = difference & -difference
                    bit = lowest.bit_length() - 1
                    flips[bit] += 1
                    changed_at[bit] = timestamp
                    difference ^= lowest
        else:
            self.first_time = timestamp
        self.count += 1
        self.last_time = timestamp
        self.last = bytes(data)

    @property
    def unmasked_changes(self) -> int:
        """Bits that changed since the notch without having changed before it."""
        return self.changed & ~self.mask

    def bit(self, bit: int) -> BitInfo:
        byte = bit // 8
        histogram = self.histograms[byte] if byte < len(self.histograms) else []
        frames = sum(histogram)
        weight = 1 << (bit % 8)
        ones = sum(count for value, count in enumerate(histogram) if value & weight)
        transitions = self.count - 1
        changed_at = self.changed_at[bit] if bit < len(self.changed_at) else -math.inf
        return BitInfo(
            bit=bit,
            value=self.last[byte] >> (bit % 8) & 1 if byte < len(self.last) else 0,
            ones=ones / frames if frames else 0.0,
            flips=self.flips[bit] / transitions if transitions > 0 and bit < len(self.flips) else 0.0,
            changed_at=changed_at if changed_at > -math.inf else None,
            masked=bool(self.mask >> bit & 1),
        )

    def bits(self) -> List[BitInfo]:
        return [self.bit(bit) for bit in range(8 * self.length)]

    def byte(self, index: int) -> ByteInfo:
        histogram = self.histograms[index]
        frames = sum(histogram)
        used = [value for value, count in enumerate(histogram) if count]
        entropy = -sum(count / frames * math.log2(count / frames) for count in histogram if count) if frames else 0.0
        return ByteInfo(
            index=index,
            value=self.last[index] if index < len(self.last) else None,
            distinct=len(used),
            minimum=used[0] if used else 0,
            maximum=used[-1] if used else 0,
            entropy=abs(entropy),
            histogram=tuple(histogram),
        )

    def bytes(self) -> List[ByteInfo]:
        return [self.byte(index) for index in range(self.length)]


class BitStatistics:
    """Bit and byte statistics of all identifiers (received and transmitted alike)."""

    def __init__(self):
        self.ids: Dict[IdKey, IdBitStatistics] = {}
        self.notch_time: Optional[float] = None  # Last timestamp before the notch
        self.last_time: Optional[float] = None
        self._before_notch: set = set()  # Identifiers seen before the notch

    def on_message(self, msg: can.Message) -> None:
        if msg.is_error_frame or msg.is_remote_frame:
            return
        key = (msg.arbitration_id, msg.is_extended_id)
        statistics = self.ids.get(key)
        if statistics is None:
            statistics = self.ids[key] = IdBitStatistics()
        statistics.update(msg.timestamp, msg.data)
        self.last_time = msg.timestamp

    def update_all(self, messages: Iterable[can.Message]) -> None:
        for msg in messages:
            self.on_message(msg)

    def get(self, key: IdKey) -> Optional[IdBitStatistics]:
        return self.ids.get(key)

    # Notch

    @property
    def notched(self) -> bool:
        return self.notch_time is not None

    def notch(self) -> None:
        """Mask every bit that changed so far, so that only new changes show."""
        for statistics in self.ids.values():
            statistics.mask = statistics.changed
        self._before_notch = set(self.ids)
        self.notch_time = self.last_time if self.last_time is not None else -math.inf

    def clear_notch(self) -> None:
        for statistics in self.ids.values():
            statistics.mask = 0
        self._before_notch = set()
        self.notch_time = None

    def is_new(self, key: IdKey) -> bool:
        """Whether the identifier first appeared after the notch."""
        return self.notched and key not in self._before_notch

    def changed_since_notch(self) -> Dict[IdKey, int]:
        """The bits (as a mask) of each identifier that changed after the notch and not before it."""
        if not self.notched:
            return {}
        result = {}
        for key, statistics in self.ids.items():
            bits = statistics.unmasked_changes
            if bits:
                result[key] = bits
        return result

    def reset(self) -> None:
        self.ids.clear()
        self.last_time = None
        self.clear_notch()
