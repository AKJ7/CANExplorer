"""AUTOSAR End-to-End (E2E) protection: the CRC routines of the AUTOSAR CRC library and the E2E Profile 2
protect/check, so a SOME/IP (or signal PDU) payload carrying an E2E header can be verified — the CRC and the
sequence counter catch corrupted, lost, repeated or reordered messages on the wire.

The CRC functions match the AUTOSAR SWS CRC library (verified against its published check values). Profile 2 is
the one most commonly seen in-vehicle: a one-byte CRC, a 4-bit sequence counter and a 16-entry Data ID list
indexed by the counter. Validating a real PDU needs its E2E configuration (which profile, the Data IDs, the
offset and length) — that comes from the ARXML E2E configuration; this module is the algorithm underneath it.
"""

from dataclasses import dataclass
from typing import List, Sequence


def _reflect(value: int, bits: int) -> int:
    result = 0
    for index in range(bits):
        if value & (1 << index):
            result |= 1 << (bits - 1 - index)
    return result


def crc8(data: bytes, start: int = 0xFF, first_call: bool = True) -> int:
    """AUTOSAR Crc_CalculateCRC8: SAE J1850, polynomial 0x1D, start 0xFF, final XOR 0xFF (chainable via `start`)."""
    crc = 0xFF if first_call else (start ^ 0xFF)
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def crc8h2f(data: bytes, start: int = 0xFF, first_call: bool = True) -> int:
    """AUTOSAR Crc_CalculateCRC8H2F: polynomial 0x2F, start 0xFF, final XOR 0xFF."""
    crc = 0xFF if first_call else (start ^ 0xFF)
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x2F) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def crc16(data: bytes, start: int = 0xFFFF) -> int:
    """AUTOSAR Crc_CalculateCRC16: CCITT polynomial 0x1021, start 0xFFFF, not reflected, no final XOR."""
    crc = start
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _crc32(data: bytes, reversed_poly: int, start: int = 0xFFFFFFFF) -> int:
    crc = start
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ reversed_poly if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFF


def crc32(data: bytes) -> int:
    """AUTOSAR Crc_CalculateCRC32: the standard CRC-32 (polynomial 0x04C11DB7, reflected)."""
    return _crc32(data, _reflect(0x04C11DB7, 32))


def crc32p4(data: bytes) -> int:
    """AUTOSAR Crc_CalculateCRC32P4: polynomial 0xF4ACFB13, reflected (E2E profiles 4 and 4m)."""
    return _crc32(data, _reflect(0xF4ACFB13, 32))


@dataclass(frozen=True)
class E2eResult:
    ok: bool
    counter: int
    crc: int
    expected_crc: int


class Profile2:
    """AUTOSAR E2E Profile 2: byte 0 is the CRC, the low nibble of byte 1 is the sequence counter, and the CRC8
    (SAE J1850) is computed over the data after the CRC byte followed by the Data ID selected from the 16-entry
    Data ID list by the counter."""

    def __init__(self, data_ids: Sequence[int]):
        if len(data_ids) != 16:
            raise ValueError('Profile 2 needs a Data ID list of 16 entries (one per counter value)')
        self.data_ids: List[int] = list(data_ids)

    def _crc(self, data: bytes, counter: int) -> int:
        crc = crc8(data[1:])  # Over everything after the CRC byte
        return crc8(bytes([self.data_ids[counter & 0x0F]]), start=crc, first_call=False)

    def check(self, data: bytes) -> E2eResult:
        """Verify a received PDU's CRC against its counter's Data ID."""
        counter = data[1] & 0x0F if len(data) >= 2 else 0
        expected = self._crc(data, counter)
        return E2eResult(
            ok=len(data) >= 2 and data[0] == expected,
            counter=counter,
            crc=data[0] if data else 0,
            expected_crc=expected,
        )

    def protect(self, data: bytes, counter: int) -> bytes:
        """Return `data` with byte 1's counter set and byte 0 set to the computed CRC (data[0] is reserved for it)."""
        out = bytearray(data)
        out[1] = (out[1] & 0xF0) | (counter & 0x0F)
        out[0] = self._crc(bytes(out), counter)
        return bytes(out)
