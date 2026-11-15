"""FlexRay frames, and FlexRay in packet captures.

A FlexRay frame is identified by its slot (frame ID, 1–2047), the communication cycle it was sent in (0–63) and the
channel (A or B); its payload has 0–254 bytes, an even number. The header also carries the payload preamble, null
frame, sync frame and startup frame indicators.

Captures (pcap and pcapng) hold FlexRay frames in two ways, both read here:

- LINKTYPE_FLEXRAY (210), as the tcpdump group documents it (tcpdump.org/linktypes/LINKTYPE_FLEXRAY.html) and
  Wireshark dissects it: a measurement header (channel and type), the error flags, then the 5-byte frame header and
  the payload (the frame CRC is not included). Recordings are written in this format too, so Wireshark opens them.
- FlexRay tunnelled over Ethernet in IEEE 1722 ACF messages (core/avtp_acf.py).

The 5-byte header (ISO 17458-2 §8, paywalled) is read as Wireshark's FlexRay dissector reads it (epan/dissectors/
packet-flexray.[ch]): reserved, payload preamble, null frame indicator (0 = null frame), sync, startup, frame ID (11
bits), payload length (7 bits, in 16-bit words), header CRC (11 bits), cycle count (6 bits). The header CRC written
for simulated frames is CRC-11/FLEXRAY (polynomial 0x385, initial value 0x01A, per the reveng CRC catalogue) over the
20 header bits from the sync indicator to the payload length; reading never depends on it.
"""

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, List, Optional, Tuple

LINKTYPE_ETHERNET = 1
LINKTYPE_FLEXRAY = 210
HEADER_LENGTH = 5
MAX_PAYLOAD = 254
MAX_SLOT = 2047
CYCLES = 64

# Measurement header (LINKTYPE_FLEXRAY)
MEASUREMENT_CHANNEL_B = 0x80
TYPE_FRAME, TYPE_SYMBOL = 0x01, 0x02
# Error flags (LINKTYPE_FLEXRAY)
ERROR_FLAGS = {0x10: 'frame CRC', 0x08: 'header CRC', 0x04: 'frame end sequence', 0x02: 'coding', 0x01: 'TSS'}


class FlexRayError(ValueError):
    pass


@dataclass
class FlexRayFrame:
    timestamp: float
    channel: str  # 'A' or 'B'
    slot: int
    cycle: int
    payload: bytes = b''
    null_frame: bool = False
    sync: bool = False
    startup: bool = False
    preamble: bool = False
    errors: List[str] = field(default_factory=list)  # From the measurement device (CRC, coding, …)
    header_crc: Optional[int] = None
    transmitted: bool = False  # Sent by this tool's interface (a transmit acknowledgement or a logged Tx)

    def __post_init__(self):
        if self.channel not in ('A', 'B'):
            raise FlexRayError(f'A FlexRay channel is A or B, not {self.channel!r}')
        if not 0 <= self.slot <= MAX_SLOT:
            raise FlexRayError(f'A FlexRay slot is 1 to {MAX_SLOT}, not {self.slot}')
        if not 0 <= self.cycle < CYCLES:
            raise FlexRayError(f'A FlexRay cycle is 0 to 63, not {self.cycle}')
        if len(self.payload) > MAX_PAYLOAD:
            raise FlexRayError(f'A FlexRay payload has up to {MAX_PAYLOAD} bytes, not {len(self.payload)}')

    def describe(self) -> str:
        flags = [name for name, on in (('null', self.null_frame), ('sync', self.sync), ('startup', self.startup)) if on]
        if self.transmitted:
            flags.append('Tx')
        text = f'slot {self.slot}, cycle {self.cycle}, channel {self.channel}, {len(self.payload)} bytes'
        if flags:
            text += f' ({", ".join(flags)})'
        if self.errors:
            text += f' — {", ".join(self.errors)} error'
        return text


@dataclass
class FlexRayEvent:
    """Something on the bus or in the controller that is not a frame: a symbol (CAS/MTS, wakeup), a controller
    state change, or an error the interface reported."""

    timestamp: float
    kind: str  # 'symbol', 'status' or 'error'
    text: str
    channel: str = ''  # 'A', 'B', 'A+B' or '' (not channel specific)
    cycle: Optional[int] = None


@dataclass
class FlexRayLog:
    """What a capture or log file holds: its frames and events, oldest first."""

    frames: List[FlexRayFrame] = field(default_factory=list)
    events: List[FlexRayEvent] = field(default_factory=list)
    format: str = ''  # 'pcap', 'BLF' or 'ASC'
    skipped: int = 0  # FlexRay records that were not read (PDU entries, older record types)


# --- The frame header ---


def header_crc(sync: bool, startup: bool, slot: int, payload_words: int) -> int:
    """CRC-11/FLEXRAY over the 20 bits sync, startup, frame ID (11), payload length (7), most significant first."""
    bits = (int(sync) << 19) | (int(startup) << 18) | ((slot & 0x7FF) << 7) | (payload_words & 0x7F)
    crc = 0x01A
    for position in range(19, -1, -1):
        feedback = ((bits >> position) & 1) ^ ((crc >> 10) & 1)
        crc = (crc << 1) & 0x7FF
        if feedback:
            crc ^= 0x385
    return crc


def crc11_flexray(data: bytes) -> int:
    """CRC-11/FLEXRAY over bytes (for checking the implementation against the catalogue's check value)."""
    crc = 0x01A
    for byte in data:
        for position in range(7, -1, -1):
            feedback = ((byte >> position) & 1) ^ ((crc >> 10) & 1)
            crc = (crc << 1) & 0x7FF
            if feedback:
                crc ^= 0x385
    return crc


def encode_header(frame: FlexRayFrame) -> bytes:
    if len(frame.payload) % 2:
        raise FlexRayError('A FlexRay payload has an even number of bytes')
    words = len(frame.payload) // 2
    crc = frame.header_crc if frame.header_crc is not None else header_crc(frame.sync, frame.startup, frame.slot, words)
    first = (
        (0x40 if frame.preamble else 0)
        | (0 if frame.null_frame else 0x20)  # NFI: 1 = frame with data, 0 = null frame
        | (0x10 if frame.sync else 0)
        | (0x08 if frame.startup else 0)
        | ((frame.slot >> 8) & 0x07)
    )
    return bytes(
        [
            first,
            frame.slot & 0xFF,
            (words << 1) | ((crc >> 10) & 1),
            (crc >> 2) & 0xFF,
            ((crc & 0x3) << 6) | frame.cycle,
        ]
    )


def decode_header(data: bytes) -> Tuple[dict, int]:
    """The fields of a 5-byte frame header, and the payload length in bytes."""
    if len(data) < HEADER_LENGTH:
        raise FlexRayError('A FlexRay frame header has 5 bytes')
    first, second, third, fourth, fifth = data[:5]
    fields = {
        'preamble': bool(first & 0x40),
        'null_frame': not (first & 0x20),
        'sync': bool(first & 0x10),
        'startup': bool(first & 0x08),
        'slot': ((first & 0x07) << 8) | second,
        'header_crc': ((third & 0x01) << 10) | (fourth << 2) | (fifth >> 6),
        'cycle': fifth & 0x3F,
    }
    return fields, (third >> 1) * 2


# --- LINKTYPE_FLEXRAY packets ---


def encode_packet(frame: FlexRayFrame) -> bytes:
    measurement = (MEASUREMENT_CHANNEL_B if frame.channel == 'B' else 0) | TYPE_FRAME
    errors = 0
    for bit, name in ERROR_FLAGS.items():
        if name in frame.errors:
            errors |= bit
    return bytes([measurement, errors]) + encode_header(frame) + bytes(frame.payload)


def encode_symbol_packet(event: FlexRayEvent, length: int = 0) -> bytes:
    return bytes([(MEASUREMENT_CHANNEL_B if event.channel == 'B' else 0) | TYPE_SYMBOL, length & 0xFF])


def decode_symbol_packet(data: bytes, timestamp: float) -> Optional[FlexRayEvent]:
    """A LINKTYPE_FLEXRAY symbol packet (measurement header, symbol length) as an event; None for frames."""
    if len(data) < 2 or data[0] & 0x7F != TYPE_SYMBOL:
        return None
    channel = 'B' if data[0] & MEASUREMENT_CHANNEL_B else 'A'
    return FlexRayEvent(timestamp, 'symbol', f'Symbol (length {data[1]})', channel)


def decode_packet(data: bytes, timestamp: float) -> Optional[FlexRayFrame]:
    """A LINKTYPE_FLEXRAY packet as a frame; None for symbols. Truncated payloads are kept as received."""
    if len(data) < 2:
        raise FlexRayError('A FlexRay packet has at least the measurement header and the error flags')
    measurement, errors = data[0], data[1]
    if measurement & 0x7F == TYPE_SYMBOL:
        return None
    if measurement & 0x7F != TYPE_FRAME:
        raise FlexRayError(f'Unknown FlexRay packet type {measurement & 0x7F}')
    fields, length = decode_header(data[2:])
    payload = bytes(data[2 + HEADER_LENGTH : 2 + HEADER_LENGTH + length])
    return FlexRayFrame(
        timestamp=timestamp,
        channel='B' if measurement & MEASUREMENT_CHANNEL_B else 'A',
        payload=payload,
        errors=[name for bit, name in ERROR_FLAGS.items() if errors & bit],
        **fields,
    )


# --- Captures ---


_PCAP_MAGICS = {0xA1B2C3D4: ('<', 1e-6), 0xA1B23C4D: ('<', 1e-9), 0xD4C3B2A1: ('>', 1e-6), 0x4D3CB2A1: ('>', 1e-9)}


def _packets(data: bytes) -> Iterator[Tuple[int, float, bytes]]:
    """(link type, timestamp, packet) of a pcap or pcapng file."""
    if data[:4] == b'\x0a\x0d\x0d\x0a':
        yield from _pcapng_packets(data)
        return
    magic = struct.unpack('<I', data[:4])[0] if len(data) >= 24 else 0
    if magic not in _PCAP_MAGICS:
        raise FlexRayError('Not a pcap or pcapng file')
    endian, resolution = _PCAP_MAGICS[magic]
    link_type = struct.unpack_from(endian + 'I', data, 20)[0]
    offset = 24
    while offset + 16 <= len(data):
        seconds, fraction, length, _ = struct.unpack_from(endian + 'IIII', data, offset)
        offset += 16
        yield link_type, seconds + fraction * resolution, bytes(data[offset : offset + length])
        offset += length


def _pcapng_packets(data: bytes) -> Iterator[Tuple[int, float, bytes]]:
    from can_explorer.core.ethernet import _pcapng_tsresol

    offset, endian = 0, '<'
    link_types: List[int] = []
    resolutions: List[float] = []
    while offset + 12 <= len(data):
        if offset == 0 or data[offset : offset + 4] in (b'\x0a\x0d\x0d\x0a',):
            endian = '<' if struct.unpack_from('<I', data, offset + 8)[0] == 0x1A2B3C4D else '>'
            link_types, resolutions = [], []
        block_type, length = struct.unpack_from(endian + 'II', data, offset)
        if length < 12 or offset + length > len(data):
            break
        body = data[offset + 8 : offset + length - 4]
        if block_type == 0x00000001:  # Interface Description Block
            link_types.append(struct.unpack_from(endian + 'H', body, 0)[0])
            resolutions.append(_pcapng_tsresol(body[8:], endian))
        elif block_type == 0x00000006 and len(body) >= 20:  # Enhanced Packet Block
            interface, high, low, captured = struct.unpack_from(endian + 'IIII', body, 0)
            if interface < len(link_types):
                yield (
                    link_types[interface],
                    ((high << 32) | low) * resolutions[interface],
                    bytes(body[20 : 20 + captured]),
                )
        offset += length


def read_log(path: str | Path) -> FlexRayLog:
    """The FlexRay frames and events of a capture or log file: pcap or pcapng (LINKTYPE_FLEXRAY packets, and
    FlexRay frames tunnelled in IEEE 1722 ACF messages of Ethernet packets), Vector BLF or Vector ASC
    (core/flexray_logs.py). Raises FlexRayError when it holds no FlexRay frame."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise FlexRayError(f'Cannot read {path.name}: {e}') from e
    if data[:4] == b'LOGG':
        from can_explorer.core.flexray_logs import read_blf

        log = read_blf(data)
    elif data[:4] in (b'\x0a\x0d\x0d\x0a',) or (len(data) >= 4 and struct.unpack('<I', data[:4])[0] in _PCAP_MAGICS):
        log = _read_pcap(data)
    else:
        from can_explorer.core.flexray_logs import read_asc

        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            text = data.decode('latin-1')
        log = read_asc(text)
    if not log.frames:
        raise FlexRayError(
            f'{path.name} has no FlexRay frames (pcap with LINKTYPE_FLEXRAY or IEEE 1722 ACF, BLF or ASC)'
        )
    return log


def _read_pcap(data: bytes) -> FlexRayLog:
    log = FlexRayLog(format='pcap')
    for link_type, timestamp, packet in _packets(data):
        if link_type == LINKTYPE_FLEXRAY:
            symbol = decode_symbol_packet(packet, timestamp)
            if symbol is not None:
                log.events.append(symbol)
                continue
            try:
                frame = decode_packet(packet, timestamp)
            except FlexRayError:
                log.skipped += 1  # A damaged packet: skipped, as Wireshark shows it malformed
                continue
            if frame is not None:
                log.frames.append(frame)
        elif link_type == LINKTYPE_ETHERNET:
            log.frames.extend(_acf_frames(packet, timestamp))
    return log


def read_capture(path: str | Path) -> List[FlexRayFrame]:
    """The FlexRay frames of a capture or log file (see read_log). Raises FlexRayError when it holds none."""
    return read_log(path).frames


def _acf_frames(packet: bytes, timestamp: float) -> Iterator[FlexRayFrame]:
    from can_explorer.core.ethernet import decode as decode_ethernet

    try:
        decoded = decode_ethernet(packet, timestamp)
    except Exception:  # noqa: BLE001 - a packet the Ethernet decoder cannot read: not ACF
        return
    for acf in decoded.flexray_frames:
        for channel in ('A', 'B'):
            if acf.channel & (1 if channel == 'A' else 2):
                yield FlexRayFrame(
                    timestamp=timestamp,
                    channel=channel,
                    slot=acf.frame_id,
                    cycle=acf.cycle,
                    payload=bytes(acf.data)[:MAX_PAYLOAD],
                    null_frame=acf.null_frame,
                    sync=acf.sync,
                    startup=acf.startup,
                    preamble=acf.preamble,
                )


class CaptureWriter:
    """Writes frames as a pcap file with LINKTYPE_FLEXRAY (nanosecond timestamps), as Wireshark reads it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file: Optional[BinaryIO] = open(self.path, 'wb')  # noqa: SIM115 - closed by close()
        self._file.write(struct.pack('<IHHiIII', 0xA1B23C4D, 2, 4, 0, 0, 65535, LINKTYPE_FLEXRAY))
        self.count = 0

    def write(self, frame: FlexRayFrame) -> None:
        if self._file is None:
            raise FlexRayError('The recording is closed')
        self._write(frame.timestamp, encode_packet(frame))
        self.count += 1

    def write_symbol(self, event: FlexRayEvent, length: int = 0) -> None:
        """A symbol event as a LINKTYPE_FLEXRAY symbol packet (other events have no pcap form)."""
        if self._file is None:
            raise FlexRayError('The recording is closed')
        self._write(event.timestamp, encode_symbol_packet(event, length))

    def _write(self, timestamp: float, packet: bytes) -> None:
        seconds = int(timestamp)
        nanoseconds = int(round((timestamp - seconds) * 1e9))
        if nanoseconds >= 1_000_000_000:
            seconds, nanoseconds = seconds + 1, nanoseconds - 1_000_000_000
        self._file.write(struct.pack('<IIII', seconds, nanoseconds, len(packet), len(packet)) + packet)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def write_capture(path: str | Path, frames: Iterable[FlexRayFrame]) -> int:
    writer = CaptureWriter(path)
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.close()
    return writer.count
