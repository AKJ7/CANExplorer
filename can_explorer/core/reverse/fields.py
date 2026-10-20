"""Bit fields of a frame's payload, numbered like DBC files and cantools.

Bit `n` is bit `n % 8` of byte `n // 8` (bit 0 the least significant). A little endian (Intel) field starts at its
least significant bit and continues upwards; a big endian (Motorola) field starts at its most significant bit and
continues with the next lower bit, from bit 0 of a byte to bit 7 of the next byte.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

BYTE_ORDERS = ('little_endian', 'big_endian')  # Intel, Motorola


def _motorola_index(bit: int) -> int:
    """Position of a bit counted from the most significant bit of byte 0 (the order of a big endian field)."""
    return bit // 8 * 8 + 7 - bit % 8


def _from_motorola_index(index: int) -> int:
    return index // 8 * 8 + 7 - index % 8


@dataclass(frozen=True)
class BitField:
    """`length` bits of the frames of one identifier, decoded as raw * scale + offset."""

    frame_id: int
    extended: bool
    start: int  # Least significant bit (little endian) or most significant bit (big endian)
    length: int
    byte_order: str = 'little_endian'
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0

    def __post_init__(self):
        if self.byte_order not in BYTE_ORDERS:
            raise ValueError(f'Byte order must be one of {", ".join(BYTE_ORDERS)}')
        if not 1 <= self.length <= 64:
            raise ValueError('Fields are 1 to 64 bits long')
        if self.start < 0:
            raise ValueError('The start bit cannot be negative')
        if self.scale == 0:
            raise ValueError('The factor cannot be 0')

    @classmethod
    def spanning(
        cls, frame_id: int, extended: bool, first: int, last: int, byte_order: str = 'little_endian', **options: Any
    ) -> 'BitField':
        """The field from bit `first` to bit `last` (in either order) in the given byte order, e.g. from two
        clicks in a bit grid."""
        if byte_order == 'big_endian':
            low, high = sorted((_motorola_index(first), _motorola_index(last)))
            return cls(frame_id, extended, _from_motorola_index(low), high - low + 1, byte_order, **options)
        low, high = sorted((first, last))
        return cls(frame_id, extended, low, high - low + 1, byte_order, **options)

    @property
    def id_text(self) -> str:
        return f'{self.frame_id:08X}' if self.extended else f'{self.frame_id:03X}'

    @property
    def key(self) -> str:
        """E.g. '1A0[16|8@1+]': the identifier and the field in DBC notation (start|length@order sign)."""
        order = '1' if self.byte_order == 'little_endian' else '0'
        return f'{self.id_text}[{self.start}|{self.length}@{order}{"-" if self.signed else "+"}]'

    @property
    def bits(self) -> List[int]:
        """The bits of the field, most significant first."""
        if self.byte_order == 'little_endian':
            return list(range(self.start + self.length - 1, self.start - 1, -1))
        first = _motorola_index(self.start)
        return [_from_motorola_index(index) for index in range(first, first + self.length)]

    @property
    def min_length(self) -> int:
        """Bytes a payload needs to hold the field."""
        return max(self.bits) // 8 + 1

    def raw(self, data: bytes) -> Optional[int]:
        """The raw value in `data`, or None when the payload is too short."""
        size = len(data) * 8
        mask = (1 << self.length) - 1
        if self.byte_order == 'little_endian':
            if self.start + self.length > size:
                return None
            value = (int.from_bytes(data, 'little') >> self.start) & mask
        else:
            first = _motorola_index(self.start)
            if first + self.length > size:
                return None
            value = (int.from_bytes(data, 'big') >> (size - first - self.length)) & mask
        if self.signed and value >> (self.length - 1):
            value -= 1 << self.length
        return value

    def value(self, data: bytes) -> Optional[float]:
        raw = self.raw(data)
        return None if raw is None else raw * self.scale + self.offset

    def to_dict(self) -> Dict[str, Any]:
        return {
            'frame_id': self.frame_id,
            'extended': self.extended,
            'start': self.start,
            'length': self.length,
            'byte_order': self.byte_order,
            'signed': self.signed,
            'scale': self.scale,
            'offset': self.offset,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BitField':
        return cls(
            frame_id=int(data['frame_id']),
            extended=bool(data.get('extended', False)),
            start=int(data['start']),
            length=int(data['length']),
            byte_order=str(data.get('byte_order', 'little_endian')),
            signed=bool(data.get('signed', False)),
            scale=float(data.get('scale', 1.0)),
            offset=float(data.get('offset', 0.0)),
        )

    @classmethod
    def from_signal(cls, frame_id: int, extended: bool, signal) -> 'BitField':
        """The field of a cantools signal."""
        return cls(
            frame_id,
            extended,
            signal.start,
            signal.length,
            signal.byte_order,
            signal.is_signed,
            float(signal.scale),
            float(signal.offset),
        )
