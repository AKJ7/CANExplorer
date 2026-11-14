"""AUTOSAR End-to-End (E2E) protection: the CRC routines of the AUTOSAR CRC library and the E2E profile
protect/check functions, so a SOME/IP (or signal PDU) payload carrying an E2E header can be verified — the CRC and
the sequence counter catch corrupted, lost, repeated or reordered messages on the wire.

The CRC functions match the AUTOSAR SWS CRC library (verified against its published check values). Three profiles
are implemented, each in its default header position (offset 0), and verified against the worked examples in the
AUTOSAR E2E Protocol Specification (PRS_E2EProtocol):

- **Profile 2** — a one-byte CRC (CRC8, 0x1D), a 4-bit sequence counter and a 16-entry Data ID list.
- **Profile 5** — a two-byte CRC (CRC16/CCITT, 0x1021, little endian), an 8-bit counter and an implicit 16-bit
  Data ID (spec §6.8).
- **Profile 22** — the Profile 2 bus layout but with the CRC8H2F polynomial (0x2F) (spec §6.13).

Three more, the profiles of SOME/IP and other large, variable-length data, carry an explicit Length and can sit at
any byte offset in the data (e.g. after the SOME/IP header). Their header fields are big endian, and the CRC covers
the whole data except the CRC field (PRS_E2E_01207), including the bytes before the header. They are verified
against the protocol examples of the R23-11 specification (§6.7.3, §6.9.5, §6.10.3), with and without the
SOME/IP header in front:

- **Profile 4** (§6.7) — Length 16, Counter 16, Data ID 32, then CRC-32P4: a 12-byte header.
- **Profile 6** (§6.9) — CRC-16 (CCITT, 0x1021), Length 16, Counter 8: a 5-byte header; the 16-bit Data ID is
  not sent but appended to the CRC input after the data, high byte first.
- **Profile 7** (§6.10) — CRC-64 (ECMA, 0x42F0E1EBA9EA3693), Length 32, Counter 32, Data ID 32: a 20-byte header.

Validating a real PDU needs its E2E configuration (which profile, the Data IDs, the offset and length) — that comes
from the ARXML E2E configuration or, for SOME/IP, the user (core/someip_e2e.py); this module is the algorithm
underneath it. (Profiles 1 and 11, with their configurable Data-ID nibble modes, and the method profiles 4m/7m are
not implemented here.)
"""

from dataclasses import dataclass
from typing import Callable, List, Sequence


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


_CRC64_REFLECTED = _reflect(0x42F0E1EBA9EA3693, 64)


def crc64(data: bytes) -> int:
    """AUTOSAR Crc_CalculateCRC64: CRC-64 (ECMA) polynomial 0x42F0E1EBA9EA3693, reflected, start and final XOR
    0xFFFFFFFFFFFFFFFF (E2E profile 7)."""
    crc = 0xFFFFFFFFFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ _CRC64_REFLECTED if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFFFFFFFFFF


@dataclass(frozen=True)
class E2eResult:
    ok: bool
    counter: int
    crc: int
    expected_crc: int
    error: str = ''  # A header field that does not match (length, Data ID), or why the data cannot be checked


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


class Profile5:
    """AUTOSAR E2E Profile 5: a 16-bit CRC (CRC16/CCITT, polynomial 0x1021) in bytes 0-1 little-endian, an 8-bit
    sequence counter in byte 2, then the user data. The CRC is computed over the counter and the user data (i.e.
    everything after the two CRC bytes), extended at the end with the 16-bit Data ID, low byte first. Only the
    default header position (offset 0) is implemented."""

    def __init__(self, data_id: int):
        self.data_id = data_id & 0xFFFF

    def _crc(self, data: bytes) -> int:
        crc = crc16(data[2:])  # The counter byte and the user data, after the two CRC bytes
        crc = crc16(bytes([self.data_id & 0xFF]), start=crc)
        return crc16(bytes([(self.data_id >> 8) & 0xFF]), start=crc)

    def check(self, data: bytes) -> E2eResult:
        """Verify a received PDU's 16-bit CRC against its Data ID."""
        if len(data) < 3:
            return E2eResult(ok=False, counter=0, crc=0, expected_crc=0)
        expected = self._crc(data)
        received = data[0] | (data[1] << 8)
        return E2eResult(ok=received == expected, counter=data[2], crc=received, expected_crc=expected)

    def protect(self, data: bytes, counter: int) -> bytes:
        """Return `data` with byte 2's counter set and bytes 0-1 set to the computed CRC (low byte first)."""
        out = bytearray(data)
        out[2] = counter & 0xFF
        crc = self._crc(bytes(out))
        out[0], out[1] = crc & 0xFF, (crc >> 8) & 0xFF
        return bytes(out)


class Profile22:
    """AUTOSAR E2E Profile 22: the Profile 2 bus layout (byte 0 is the CRC, the low nibble of byte 1 is the 4-bit
    counter) but with the CRC8H2F polynomial (0x2F). The CRC is computed over everything after the CRC byte,
    followed by the Data ID selected from the 16-entry Data ID list by the counter. Only the default header
    position (offset 0) is implemented."""

    def __init__(self, data_ids: Sequence[int]):
        if len(data_ids) != 16:
            raise ValueError('Profile 22 needs a Data ID list of 16 entries (one per counter value)')
        self.data_ids: List[int] = list(data_ids)

    def _crc(self, data: bytes, counter: int) -> int:
        crc = crc8h2f(data[1:])  # Over everything after the CRC byte
        return crc8h2f(bytes([self.data_ids[counter & 0x0F]]), start=crc, first_call=False)

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


class _LengthProfile:
    """The profiles with an explicit Length field, placed at a byte `offset` in the data (spec §6.3 placeholders:
    each field's position and length within the header; a Data ID length of 0 means it is not transmitted)."""

    number = 0
    LENGTH = (0, 0)  # (position, bytes) within the header
    COUNTER = (0, 0)
    DATA_ID = (0, 0)
    CRC = (0, 0)
    HEADER = 0
    IMPLICIT_DATA_ID = 0  # Bytes of Data ID appended to the CRC input after the data (Profile 6)
    calculate_crc: Callable[[bytes], int] = staticmethod(crc32p4)

    def __init__(self, data_id: int, offset: int = 0):
        """`offset`: the byte position of the E2E header in the data (bit offset / 8; 8 after a SOME/IP header)."""
        width = self.DATA_ID[1] or self.IMPLICIT_DATA_ID
        if not 0 <= data_id < 1 << (8 * width):
            raise ValueError(f'Profile {self.number} has a {8 * width}-bit Data ID')
        if offset < 0:
            raise ValueError('The E2E header offset cannot be negative')
        self.data_id = data_id
        self.offset = offset

    def _field(self, data: bytes, field) -> int:
        start = self.offset + field[0]
        return int.from_bytes(data[start : start + field[1]], 'big')

    def _crc(self, data: bytes) -> int:
        """The CRC over all of the data except the CRC field (and the implicit Data ID, if any)."""
        crc_start = self.offset + self.CRC[0]
        covered = data[:crc_start] + data[crc_start + self.CRC[1] :]
        if self.IMPLICIT_DATA_ID:
            covered += self.data_id.to_bytes(self.IMPLICIT_DATA_ID, 'big')
        return self.calculate_crc(covered)

    def check(self, data: bytes) -> E2eResult:
        """Verify the CRC, and that the Length field is the length of the data and the Data ID the configured one."""
        data = bytes(data)
        if len(data) < self.offset + self.HEADER:
            return E2eResult(False, 0, 0, 0, f'shorter than the {self.HEADER}-byte E2E header at byte {self.offset}')
        counter, received, expected = self._field(data, self.COUNTER), self._field(data, self.CRC), self._crc(data)
        length = self._field(data, self.LENGTH)
        error = ''
        if length != len(data):
            error = f'length field {length}, data {len(data)} bytes'
        elif self.DATA_ID[1] and self._field(data, self.DATA_ID) != self.data_id:
            error = f'Data ID 0x{self._field(data, self.DATA_ID):X}, expected 0x{self.data_id:X}'
        return E2eResult(received == expected and not error, counter, received, expected, error)

    def protect(self, data: bytes, counter: int) -> bytes:
        """`data` (with room for the header at the offset) with the Length, Counter, Data ID and CRC written."""
        out = bytearray(data)
        if len(out) < self.offset + self.HEADER:
            raise ValueError(f'The data has no room for the {self.HEADER}-byte E2E header at byte {self.offset}')

        def write(field, value: int) -> None:
            start = self.offset + field[0]
            out[start : start + field[1]] = (value & ((1 << (8 * field[1])) - 1)).to_bytes(field[1], 'big')

        write(self.LENGTH, len(out))
        write(self.COUNTER, counter)
        if self.DATA_ID[1]:
            write(self.DATA_ID, self.data_id)
        write(self.CRC, self._crc(bytes(out)))
        return bytes(out)


class Profile4(_LengthProfile):
    """AUTOSAR E2E Profile 4 (PRS §6.7): Length (16), Counter (16), Data ID (32), CRC-32P4 (32), big endian."""

    number = 4
    LENGTH, COUNTER, DATA_ID, CRC = (0, 2), (2, 2), (4, 4), (8, 4)
    HEADER = 12
    calculate_crc = staticmethod(crc32p4)


class Profile6(_LengthProfile):
    """AUTOSAR E2E Profile 6 (PRS §6.9): CRC-16 (16), Length (16), Counter (8); the 16-bit Data ID is implicit."""

    number = 6
    CRC, LENGTH, COUNTER, DATA_ID = (0, 2), (2, 2), (4, 1), (0, 0)
    HEADER = 5
    IMPLICIT_DATA_ID = 2
    calculate_crc = staticmethod(crc16)


class Profile7(_LengthProfile):
    """AUTOSAR E2E Profile 7 (PRS §6.10): CRC-64 (64), Length (32), Counter (32), Data ID (32), big endian."""

    number = 7
    CRC, LENGTH, COUNTER, DATA_ID = (0, 8), (8, 4), (12, 4), (16, 4)
    HEADER = 20
    calculate_crc = staticmethod(crc64)


LENGTH_PROFILES = {4: Profile4, 6: Profile6, 7: Profile7}
