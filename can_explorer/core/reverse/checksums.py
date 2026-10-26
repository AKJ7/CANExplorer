"""Checksums of CAN payloads: CRC-8 variants, XOR and sums, and finding which byte of a frame is one.

CRCs are affine in their start value: for payloads of one length, crc(start, data) = crc(0, data) ^ k(start). So a
byte is a CRC of the other bytes, with any start value, final XOR or constant prefix (such as an AUTOSAR E2E data
ID), when byte ^ crc(0, other bytes) is the same in every frame; no start values have to be tried. For XOR and
sums likewise, with the difference instead.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Sequence, Tuple

MIN_FRAMES = 20
MIN_MATCH = 0.98  # Share of the frames that must match (a few corrupted or late frames are tolerated)


@dataclass(frozen=True)
class Crc8:
    name: str
    polynomial: int
    reflected: bool = False


CRCS = (
    Crc8('CRC-8 SAE J1850 (AUTOSAR)', 0x1D),
    Crc8('CRC-8H2F (AUTOSAR)', 0x2F),
    Crc8('CRC-8 (SMBus)', 0x07),
    Crc8('CRC-8 Maxim', 0x31, reflected=True),
    Crc8('CRC-8 CDMA2000', 0x9B),
)


@lru_cache(maxsize=None)
def _table(polynomial: int, reflected: bool) -> Tuple[int, ...]:
    table = []
    for byte in range(256):
        if reflected:
            reverse = int(f'{polynomial:08b}'[::-1], 2)
            crc = byte
            for _ in range(8):
                crc = (crc >> 1) ^ reverse if crc & 1 else crc >> 1
        else:
            crc = byte
            for _ in range(8):
                crc = ((crc << 1) ^ polynomial) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return tuple(table)


def crc8(data: Sequence[int], polynomial: int = 0x1D, start: int = 0, final_xor: int = 0, reflected: bool = False):
    table = _table(polynomial, reflected)
    crc = start
    for byte in data:
        crc = table[crc ^ byte]
    return crc ^ final_xor


def sae_j1850(data: Sequence[int]) -> int:
    """CRC-8 SAE J1850 as AUTOSAR uses it (start and final XOR 0xFF)."""
    return crc8(data, 0x1D, 0xFF, 0xFF)


@dataclass(frozen=True)
class ChecksumMatch:
    byte: int
    algorithm: str
    constant: Optional[int]  # The constant (start value, data ID or offset) folded in; None: it depends on a counter
    counter_byte: Optional[int] = None  # With a constant per counter value: the byte holding the counter (low nibble)
    share: float = 1.0

    @property
    def description(self) -> str:
        if self.constant is None:
            return f'{self.algorithm}, data ID per counter value (counter in byte {self.counter_byte})'
        return f'{self.algorithm}, constant 0x{self.constant:02X}'


def _algorithms() -> List[Tuple[str, Callable[[Sequence[int]], int], Callable[[int, int], int]]]:
    """(name, function of the other bytes, how the checksum byte and the function's result give the constant)."""
    xor = lambda value, result: value ^ result  # noqa: E731

    def crc(algorithm: Crc8):
        table = _table(algorithm.polynomial, algorithm.reflected)

        def compute(data: Sequence[int]) -> int:
            value = 0
            for byte in data:
                value = table[value ^ byte]
            return value

        return compute

    def xor_all(data: Sequence[int]) -> int:
        value = 0
        for byte in data:
            value ^= byte
        return value

    result = [(algorithm.name, crc(algorithm), xor) for algorithm in CRCS]
    result.append(('XOR of the bytes', xor_all, xor))
    result.append(('Sum of the bytes', lambda data: sum(data) & 0xFF, lambda value, result: (value - result) & 0xFF))
    result.append(('Inverted sum', lambda data: sum(data) & 0xFF, lambda value, result: (value + result) & 0xFF))
    return result


def _dominant(values: List[int]) -> Tuple[int, float]:
    counts: Dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    value, count = max(counts.items(), key=lambda item: item[1])
    return value, count / len(values)


def find_checksums(payloads: Sequence[bytes], sample: int = 400) -> List[ChecksumMatch]:
    """The bytes of payloads (all of the same length) that are a checksum of the other bytes.

    A byte qualifies only when it changes and the other bytes do (in at least two bytes, or one byte for CRCs, so
    that a byte copying another one is not taken for a checksum).
    """
    payloads = list(payloads[-sample:])
    if len(payloads) < MIN_FRAMES or not payloads[0]:
        return []
    length = len(payloads[0])
    changing = [len({payload[index] for payload in payloads}) > 1 for index in range(length)]
    algorithms = _algorithms()
    matches = []
    for byte in range(length):
        others = [index for index in range(length) if index != byte]
        changing_others = sum(changing[index] for index in others)
        if not changing[byte] or changing_others == 0:
            continue
        data = [[payload[index] for index in others] for payload in payloads]
        targets = [payload[byte] for payload in payloads]
        for name, function, constant_of in algorithms:
            if changing_others < 2 and not name.startswith('CRC'):
                continue
            constants = [constant_of(target, function(row)) for target, row in zip(targets, data, strict=True)]
            constant, share = _dominant(constants)
            if share >= MIN_MATCH:
                matches.append(ChecksumMatch(byte, name, constant, share=share))
                break
            # AUTOSAR E2E profile 2: the data ID depends on the counter in the low nibble of another byte
            found = _per_counter(constants, payloads, others) if name.startswith('CRC') else None
            if found is not None:
                matches.append(ChecksumMatch(byte, name, None, counter_byte=found, share=1.0))
                break
    return matches


def _per_counter(constants: List[int], payloads: List[bytes], others: List[int]) -> Optional[int]:
    for index in others:
        groups: Dict[int, set] = {}
        sizes: Dict[int, int] = {}
        for constant, payload in zip(constants, payloads, strict=True):
            counter = payload[index] & 0x0F
            groups.setdefault(counter, set()).add(constant)
            sizes[counter] = sizes.get(counter, 0) + 1
        if len(groups) >= 4 and min(sizes.values()) >= 3 and all(len(values) == 1 for values in groups.values()):
            if len({next(iter(values)) for values in groups.values()}) > 1:
                return index
    return None
