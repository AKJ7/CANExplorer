"""ReadDataByPeriodicIdentifier (UDS 0x2A) and DynamicallyDefineDataIdentifier (UDS 0x2C): let an ECU send data
on its own, without polling, and combine several data identifiers or memory areas into one.

A periodic data identifier (PDID) is the low byte of a DID in the range 0xF200-0xF2FF. After a positive response
the ECU sends the PDID's data at the requested rate as **periodic response messages**: per ISO 14229-1:2020 (as
the AUTOSAR Dcm specification R23-11 §7.6.2.11.1 states) the PDID followed by the data, without a service ID —
on CAN in single unsegmented frames (UUDT), usually on a CAN identifier of their own. Earlier ECUs send them as
ordinary (USDT) responses on the diagnostic response identifier, with the service ID 0x6A in front.
`PeriodicDataReceiver` takes both: frames on a CAN identifier (`attach`) and 0x6A messages that reach a UdsClient
as unsolicited responses (`listen_to`).

A CAN frame carries no data length, so the data of a UUDT frame includes the frame's padding; `lengths` (or a
diagnostic description) trims it to the DID's length.

Layouts checked against Wireshark's UDS dissector (transmission modes, the defineByIdentifier elements) and the
AUTOSAR Dcm specification (the PDID range and the periodic message layout).
"""

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

import can

from can_explorer.core.frames import format_hex
from can_explorer.core.uds import UdsError, did_name

SID = 0x2A
DDDI_SID = 0x2C
PERIODIC_DID_BASE = 0xF200

SEND_AT_SLOW_RATE = 0x01
SEND_AT_MEDIUM_RATE = 0x02
SEND_AT_FAST_RATE = 0x03
STOP_SENDING = 0x04
TRANSMISSION_MODES = {
    SEND_AT_SLOW_RATE: 'sendAtSlowRate',
    SEND_AT_MEDIUM_RATE: 'sendAtMediumRate',
    SEND_AT_FAST_RATE: 'sendAtFastRate',
    STOP_SENDING: 'stopSending',
}

DEFINE_BY_IDENTIFIER = 0x01
DEFINE_BY_MEMORY_ADDRESS = 0x02
CLEAR_DYNAMICALLY_DEFINED = 0x03
DDDI_TYPES = {
    DEFINE_BY_IDENTIFIER: 'defineByIdentifier',
    DEFINE_BY_MEMORY_ADDRESS: 'defineByMemoryAddress',
    CLEAR_DYNAMICALLY_DEFINED: 'clearDynamicallyDefinedDataIdentifier',
}


def periodic_id(did: int) -> int:
    """The PDID (one byte) of a DID 0xF200-0xF2FF; a value below 0x100 is taken as a PDID already."""
    if 0 <= did <= 0xFF:
        return did
    if PERIODIC_DID_BASE <= did <= PERIODIC_DID_BASE + 0xFF:
        return did & 0xFF
    raise UdsError(f'DID 0x{did:04X} is not a periodic data identifier (0xF200-0xF2FF)')


# DynamicallyDefineDataIdentifier


@dataclass(frozen=True, slots=True)
class SourceElement:
    """A part of a source DID: `size` bytes from `position` (1-based, as on the wire)."""

    did: int
    position: int
    size: int


def parse_sources(text: str) -> Tuple[str, list]:
    """The sources of a dynamically defined DID as typed: DID elements `DID:position:size` (DID in hex, the 1-based
    position and the size in decimal, e.g. `0100:1:2 F190:1:4`) or memory areas `@address:size` (address in hex,
    size in decimal, e.g. `@20001000:4`). Returns ('did', [SourceElement]) or ('memory', [(address, size)]);
    both kinds cannot be mixed in one definition (they are different sub-functions)."""
    tokens = [token for token in re.split(r'[\s,;]+', text.strip()) if token]
    if not tokens:
        raise UdsError('Give the sources: DID:position:size, or @address:size for memory')
    elements: List[SourceElement] = []
    areas: List[Tuple[int, int]] = []
    for token in tokens:
        try:
            if token.startswith('@'):
                address, size = token[1:].split(':')
                areas.append((int(address, 16), int(size)))
            else:
                did, position, size = token.split(':')
                elements.append(SourceElement(int(did, 16), int(position), int(size)))
        except ValueError:
            raise UdsError(f'{token!r}: expected DID:position:size or @address:size') from None
    if elements and areas:
        raise UdsError('Define a DID from data identifiers or from memory, not both at once')
    return ('memory', areas) if areas else ('did', elements)


def define_by_identifier_request(dddid: int, sources: Sequence[SourceElement | Tuple[int, int, int]]) -> bytes:
    """2C 01 DDDID, then per element: source DID (2), positionInSourceDataRecord (1, from 1), memorySize (1)."""
    if not sources:
        raise UdsError('defineByIdentifier needs at least one source')
    payload = bytes([DDDI_SID, DEFINE_BY_IDENTIFIER]) + dddid.to_bytes(2, 'big')
    for source in sources:
        element = source if isinstance(source, SourceElement) else SourceElement(*source)
        if not 1 <= element.position <= 0xFF or not 1 <= element.size <= 0xFF:
            raise UdsError('Source positions start at 1 and sizes are 1-255 bytes')
        payload += element.did.to_bytes(2, 'big') + bytes([element.position, element.size])
    return payload


def define_by_memory_address_request(
    dddid: int, areas: Sequence[Tuple[int, int]], address_bytes: int = 4, size_bytes: int = 1
) -> bytes:
    """2C 02 DDDID, addressAndLengthFormatIdentifier, then per area: address and size in those widths."""
    if not areas:
        raise UdsError('defineByMemoryAddress needs at least one memory area')
    if not 1 <= address_bytes <= 15 or not 1 <= size_bytes <= 15:
        raise UdsError('Address and size widths are 1-15 bytes')
    payload = bytes([DDDI_SID, DEFINE_BY_MEMORY_ADDRESS]) + dddid.to_bytes(2, 'big')
    payload += bytes([(size_bytes << 4) | address_bytes])
    for address, size in areas:
        if address >= 1 << (8 * address_bytes) or size >= 1 << (8 * size_bytes):
            raise UdsError(f'0x{address:X} or {size} bytes do not fit the address and length format')
        payload += address.to_bytes(address_bytes, 'big') + size.to_bytes(size_bytes, 'big')
    return payload


async def define_by_identifier(client, dddid: int, sources) -> None:
    await client.call(define_by_identifier_request(dddid, sources))


async def define_by_memory_address(client, dddid: int, areas, address_bytes: int = 4, size_bytes: int = 1) -> None:
    await client.call(define_by_memory_address_request(dddid, areas, address_bytes, size_bytes))


async def clear_dynamically_defined(client, dddid: Optional[int] = None) -> None:
    """Clear one dynamically defined DID, or all of them (`dddid` None)."""
    payload = bytes([DDDI_SID, CLEAR_DYNAMICALLY_DEFINED])
    await client.call(payload + (dddid.to_bytes(2, 'big') if dddid is not None else b''))


# ReadDataByPeriodicIdentifier


def read_periodic_request(transmission_mode: int, periodic_ids: Sequence[int] = ()) -> bytes:
    if transmission_mode not in TRANSMISSION_MODES:
        raise UdsError(f'Unknown transmission mode 0x{transmission_mode:02X}')
    ids = [periodic_id(identifier) for identifier in periodic_ids]
    if transmission_mode != STOP_SENDING and not ids:
        raise UdsError(f'{TRANSMISSION_MODES[transmission_mode]} needs at least one periodic data identifier')
    return bytes([SID, transmission_mode]) + bytes(ids)  # stopSending without identifiers: stop all


async def read_periodic(client, transmission_mode: int, periodic_ids: Sequence[int] = ()) -> None:
    """Start (slow, medium or fast rate) or stop the periodic transmission of PDIDs (stopSending without
    identifiers stops all). The data then arrives as periodic messages, see PeriodicDataReceiver."""
    await client.call(read_periodic_request(transmission_mode, periodic_ids))


@dataclass(frozen=True, slots=True)
class PeriodicData:
    periodic_id: int
    data: bytes
    timestamp: float
    source: str  # 'uudt' (a CAN frame of its own) or 'usdt' (an 0x6A message on the diagnostic channel)

    @property
    def did(self) -> int:
        return PERIODIC_DID_BASE | self.periodic_id

    def describe(self) -> str:
        return f'{did_name(self.did)}: {format_hex(self.data)}'


class PeriodicDataReceiver:
    """Collects periodic response messages: UUDT frames on a CAN identifier and 0x6A messages on a UdsClient.

    `lengths` maps a PDID (or its 0xF2xx DID) to its data length, trimming the CAN frame's padding; `description`
    (a DiagnosticDescription) gives the lengths of the DIDs it knows. `on_data` is called for every message."""

    def __init__(
        self,
        lengths: Optional[Dict[int, int]] = None,
        description=None,
        on_data: Optional[Callable[[PeriodicData], None]] = None,
        limit: int = 10_000,
    ):
        self.lengths = {periodic_id(key): value for key, value in (lengths or {}).items()}
        self.description = description
        self.on_data = on_data
        self.limit = limit
        self.received: List[PeriodicData] = []
        self.latest: Dict[int, PeriodicData] = {}
        self.counts: Dict[int, int] = {}
        self._times: Dict[int, Deque[float]] = {}
        self._detach: List[Callable[[], None]] = []

    def rate(self, pdid: int) -> float:
        """Messages per second of a PDID, over its last 20 messages (0 with fewer than two)."""
        times = self._times.get(periodic_id(pdid))
        if not times or len(times) < 2 or times[-1] <= times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def clear(self) -> None:
        self.received.clear()
        self.latest.clear()
        self.counts.clear()
        self._times.clear()

    def _length(self, pdid: int) -> Optional[int]:
        if pdid in self.lengths:
            return self.lengths[pdid]
        if self.description is not None and hasattr(self.description, 'byte_length'):
            return self.description.byte_length(PERIODIC_DID_BASE | pdid)
        return None

    def _add(self, pdid: int, data: bytes, timestamp: float, source: str) -> None:
        length = self._length(pdid)
        item = PeriodicData(pdid, bytes(data[:length] if length is not None else data), timestamp, source)
        self.received.append(item)
        del self.received[: -self.limit]
        self.latest[pdid] = item
        self.counts[pdid] = self.counts.get(pdid, 0) + 1
        self._times.setdefault(pdid, deque(maxlen=20)).append(timestamp)
        if self.on_data is not None:
            self.on_data(item)

    def feed_frame(self, msg: can.Message) -> None:
        if msg.data:
            self._add(msg.data[0], bytes(msg.data[1:]), msg.timestamp or time.time(), 'uudt')

    def feed_usdt(self, payload: bytes) -> bool:
        """An unsolicited diagnostic message; True if it was periodic data (0x6A, PDID, data)."""
        if len(payload) >= 2 and payload[0] == SID + 0x40:
            self._add(payload[1], bytes(payload[2:]), time.time(), 'usdt')
            return True
        return False

    def attach(self, session, can_id: int, extended: bool = False) -> None:
        """Receive UUDT periodic frames on `can_id` from a BusSession."""

        def listener(msg: can.Message) -> None:
            if msg.is_rx and msg.arbitration_id == can_id and msg.is_extended_id == extended:
                self.feed_frame(msg)

        session.add_message_listener(listener)
        self._detach.append(lambda: session.remove_message_listener(listener))

    def listen_to(self, client) -> None:
        """Receive 0x6A periodic messages that reach a UdsClient as unsolicited responses."""

        def listener(payload: bytes) -> None:
            self.feed_usdt(payload)

        client.add_unsolicited_listener(listener)
        self._detach.append(lambda: client.remove_unsolicited_listener(listener))

    def close(self) -> None:
        for detach in self._detach:
            detach()
        self._detach.clear()

    def __enter__(self) -> 'PeriodicDataReceiver':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
