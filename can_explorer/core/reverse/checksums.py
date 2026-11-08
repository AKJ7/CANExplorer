"""Checksums of CAN payloads: CRC-8 variants, XOR and sums, and finding which byte of a frame is one.

CRCs are affine in their start value: for payloads of one length, crc(start, data) = crc(0, data) ^ k(start). So a
byte is a CRC of the other bytes, with any start value, final XOR or constant prefix (such as an AUTOSAR E2E data
ID), when byte ^ crc(0, other bytes) is the same in every frame; no start values have to be tried. For XOR and
sums likewise, with the difference instead.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

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


# (name, function of the other-byte columns, how the checksum byte and the result give the constant, is a CRC).
# Each works on a whole column of frames at once: `function` takes the (frames, other-bytes) matrix and returns one
# value per frame, and `constant_of` combines the per-frame checksum byte and result into the per-frame constant.
_BatchAlgorithm = Tuple[str, Callable[[np.ndarray], np.ndarray], Callable[[np.ndarray, np.ndarray], np.ndarray], bool]


def _batch_algorithms() -> List[_BatchAlgorithm]:
    xor = lambda value, result: value ^ result  # noqa: E731

    def crc(algorithm: Crc8) -> Callable[[np.ndarray], np.ndarray]:
        table = np.asarray(_table(algorithm.polynomial, algorithm.reflected), dtype=np.uint8)

        def compute(columns: np.ndarray) -> np.ndarray:
            value = np.zeros(columns.shape[0], dtype=np.uint8)
            for index in range(columns.shape[1]):
                value = table[value ^ columns[:, index]]
            return value

        return compute

    def summed(columns: np.ndarray) -> np.ndarray:
        return (columns.sum(axis=1, dtype=np.int64) & 0xFF).astype(np.int64)

    result: List[_BatchAlgorithm] = [(algorithm.name, crc(algorithm), xor, True) for algorithm in CRCS]
    result.append(
        ('XOR of the bytes', lambda columns: np.bitwise_xor.reduce(columns, axis=1).astype(np.int64), xor, False)
    )
    result.append(('Sum of the bytes', summed, lambda value, result: (value - result) & 0xFF, False))
    result.append(('Inverted sum', summed, lambda value, result: (value + result) & 0xFF, False))
    return result


def _dominant(values: np.ndarray) -> Tuple[int, float]:
    uniques, counts = np.unique(values, return_counts=True)
    index = int(np.argmax(counts))
    return int(uniques[index]), int(counts[index]) / len(values)


def find_checksums(payloads: Sequence[bytes], sample: int = 400) -> List[ChecksumMatch]:
    """The bytes of payloads (all of the same length) that are a checksum of the other bytes.

    A byte qualifies only when it changes and the other bytes do (in at least two bytes, or one byte for CRCs, so
    that a byte copying another one is not taken for a checksum).
    """
    payloads = list(payloads[-sample:])
    if len(payloads) < MIN_FRAMES or not payloads[0]:
        return []
    length = len(payloads[0])
    matrix = np.frombuffer(b''.join(payloads), dtype=np.uint8).reshape(len(payloads), length)
    changing = [bool((matrix[:, index] != matrix[0, index]).any()) for index in range(length)]
    algorithms = _batch_algorithms()
    matches = []
    for byte in range(length):
        others = [index for index in range(length) if index != byte]
        changing_others = sum(changing[index] for index in others)
        if not changing[byte] or changing_others == 0:
            continue
        columns = matrix[:, others]
        targets = matrix[:, byte].astype(np.int64)
        for name, function, constant_of, is_crc in algorithms:
            if changing_others < 2 and not is_crc:
                continue
            constants = np.asarray(constant_of(targets, function(columns).astype(np.int64)))
            constant, share = _dominant(constants)
            if share >= MIN_MATCH:
                matches.append(ChecksumMatch(byte, name, constant, share=share))
                break
            # AUTOSAR E2E profile 2: the data ID depends on the counter in the low nibble of another byte
            found = _per_counter(constants, matrix, others) if is_crc else None
            if found is not None:
                matches.append(ChecksumMatch(byte, name, None, counter_byte=found, share=1.0))
                break
    return matches


def _per_counter(constants: np.ndarray, matrix: np.ndarray, others: List[int]) -> Optional[int]:
    for index in others:
        counter = matrix[:, index] & 0x0F
        sizes: List[int] = []
        representatives: List[int] = []
        consistent = True
        for value in np.unique(counter):
            members = constants[counter == value]
            distinct = np.unique(members)
            if len(distinct) != 1:
                consistent = False
                break
            sizes.append(len(members))
            representatives.append(int(distinct[0]))
        if consistent and len(sizes) >= 4 and min(sizes) >= 3 and len(set(representatives)) > 1:
            return index
    return None
