"""FlexRay in Vector's log formats: BLF (binary) and ASC (text), as CANoe, CANalyzer and Vector loggers write them.

BLF: Vector's "FlexRay BLF Logging Format" specification (version 1.7.2, 2017) names the objects and their members;
the byte offsets (members are naturally aligned) were checked against BLF files written by Vector's binlog library
and published as test data of the open-source vector_blf reader. Read:

- VBLFLEXRAYVFrReceiveMsgEx (object type 66) and VBLFLEXRAYVFrReceiveMsg (50): a frame, with its channel mask
  (1 = A, 2 = B, 3 = both), direction (0 Rx, 1 Tx, 2 Tx request — not on the bus, skipped), slot, cycle, header
  CRCs, payload length and stored bytes, the frame flags (bit 0 null frame, 2 sync, 3 startup, 4 payload preamble,
  6 error, 16 PDU entry — PDU entries repeat a frame's bytes and are skipped) and the controller's frame state,
  whose bits depend on the controller type (§3.3.4; the error bits of the VN interfaces, Cyclone I/II and
  BUSDOCTOR are reported). Offsets: type 66 payload at 84 bytes into the object body, type 50 at 44.
- VBLFLEXRAYVFrStatus (48): symbols (CAS, MTS, wakeup) and, on Vector interfaces, the controller's POC state.
- VBLFLEXRAYVFrError (47): the errors Vector interfaces report (POC mode, sync frames, clock correction, NIT, …).

The older types the specification calls obsolete or kept for compatibility (29, 30, 40, 41, 45) are not read.
Objects are in LOG_CONTAINERs, zlib compressed or not, as python-can's BLF reader reads them.

ASC: the FlexRay lines as the open-source vector_asc reader parses them (Vector's ASC FlexRay specification is not
public): "Fr RMSG" frames (cluster, client, channel, channel mask, slot, cycle, Rx/Tx/TxRq, application parameter,
frame flags, controller type, frame state, header CRC, name, payload length, stored length, bytes, frame CRC, spy
flag, frame length in ns), the older "Fr <1|2> V9" frames (slot, cycle, payload preamble, sync, header CRC, name,
length, bytes, frame state, header bit mask; 1 is channel A and 2 channel B, as Vector's converter maps them to
BLF), "Fr SE" status lines (symbols) and "Fr EE" error lines. Numbers follow the file's "base hex|dec" header; the
cluster, client and channel numbers are decimal. "Fr PDU" lines are PDU entries and are skipped.
"""

import struct
import zlib
from decimal import Decimal
from typing import Iterator, List, Optional, Tuple

from can_explorer.core.flexray import MAX_PAYLOAD, FlexRayError, FlexRayEvent, FlexRayFrame, FlexRayLog

# BLF object types
LOG_CONTAINER = 10
FR_ERROR, FR_STATUS, FR_STARTCYCLE, FR_RCVMESSAGE, FR_RCVMESSAGE_EX = 47, 48, 49, 50, 66
OBSOLETE_TYPES = {29, 30, 40, 41, 45}

# Frame flags (§3.3.5)
FLAG_NULL, FLAG_SYNC, FLAG_STARTUP, FLAG_PREAMBLE, FLAG_ERROR, FLAG_PDU = 0x01, 0x04, 0x08, 0x10, 0x40, 0x10000

# Controller types (§3.3.3) and the error bits of their frame state (§3.3.4)
CC_CYCLONE_I, CC_BUSDOCTOR, CC_CYCLONE_II, CC_VN = 2, 3, 4, 5
FRAME_STATE_ERRORS = {
    CC_CYCLONE_I: {0: 'TX conflict', 1: 'boundary violation', 2: 'content', 3: 'syntax'},
    CC_BUSDOCTOR: {
        0: 'coding',
        1: 'TSS',
        2: 'header CRC',
        3: 'frame CRC',
        4: 'frame end sequence',
        7: 'boundary violation',
        8: 'NIT violation',
        9: 'symbol window violation',
        10: 'slot overbooked',
        11: 'null frame',
        12: 'sync or startup frame',
        13: 'frame ID',
        14: 'cycle counter',
        15: 'static payload length',
    },
    CC_CYCLONE_II: {0: 'syntax', 1: 'content', 2: 'slot boundary violation', 4: 'message lost'},
    CC_VN: {
        0: 'syntax',
        1: 'content',
        2: 'slot boundary violation',
        4: 'message lost',
        6: 'TX conflict',
        7: 'framing',
        8: 'header CRC',
        9: 'frame CRC',
        10: 'reserved bit',
        11: 'TX conflict (bus level)',
        12: 'redundancy (channels differ)',
        13: 'bus (spikes)',
        14: 'unknown',
    },
}
SYMBOLS = {1: 'CAS', 2: 'MTS', 3: 'Wakeup symbol', 4: 'Symbol'}
POC_STATES = {
    0x00: 'default config',
    0x01: 'ready',
    0x02: 'normal active',
    0x03: 'normal passive',
    0x04: 'halt',
    0x05: 'monitor mode',
    0x0F: 'config',
    0x10: 'wakeup standby',
    0x11: 'wakeup listen',
    0x12: 'wakeup send',
    0x13: 'wakeup detect',
    0x20: 'startup prepare',
    0x21: 'coldstart listen',
    0x22: 'coldstart collision resolution',
    0x23: 'coldstart consistency check',
    0x24: 'coldstart gap',
    0x25: 'coldstart join',
    0x26: 'integration coldstart check',
    0x27: 'integration listen',
    0x28: 'integration consistency check',
    0x29: 'initialize schedule',
    0x30: 'abort startup',
    0x31: 'startup success',
}
VN_ERRORS = {
    0: 'POC mode changed',
    1: 'sync frames below minimum',
    2: 'sync frame overload',
    3: 'clock correction failure',
    4: 'NIT failure',
    5: 'controller error',
    6: 'overflow',
}
POC_ERROR_MODES = {0: 'active', 1: 'passive', 2: 'communication halt'}
CC_ERROR_FLAGS = {  # VN interfaces, error tag 5 (controller error)
    0x00000001: 'POC error mode changed',
    0x00000004: 'sync frames below minimum',
    0x00000008: 'sync frame overflow',
    0x00000010: 'clock correction failure',
    0x00000040: 'parity error',
    0x00000200: 'illegal input buffer access',
    0x00000400: 'illegal output buffer access',
    0x00000800: 'message handler constraints',
    0x00010000: 'error detected on channel A',
    0x00020000: 'latest transmit violation on channel A',
    0x00040000: 'transmission across boundary on channel A',
    0x01000000: 'error detected on channel B',
    0x02000000: 'latest transmit violation on channel B',
    0x04000000: 'transmission across boundary on channel B',
}

_RCV_EX = struct.Struct('<HHHHIIHHHHHHIIIIIIHHH')  # channel … blfLogMask; payload at offset 84
_RCV = struct.Struct('<HHHBxIIHHHHHBxIIII')  # channel … appParameter; payload at offset 44
_STATUS = struct.Struct('<HHHBxIIIIIIIH')  # channel … data[1], symbol (mReserved[0])
_ERROR = struct.Struct('<HHHBxIIIIIII')  # channel … tag, data[4]


def _channels(mask: int) -> List[str]:
    return [letter for bit, letter in ((1, 'A'), (2, 'B')) if mask & bit]


def _channel_text(mask: int) -> str:
    return '+'.join(_channels(mask))


def frame_state_errors(cc_type: int, state: int) -> List[str]:
    return [name for bit, name in FRAME_STATE_ERRORS.get(cc_type, {}).items() if state >> bit & 1]


def _frames(
    timestamp: float,
    mask: int,
    slot: int,
    cycle: int,
    flags: int,
    cc_type: int,
    state: int,
    payload: bytes,
    crcs: Tuple[int, int],
    transmitted: bool,
) -> Iterator[FlexRayFrame]:
    errors = frame_state_errors(cc_type, state)
    if flags & FLAG_ERROR and not errors:
        errors = ['invalid frame']
    for channel in _channels(mask):
        yield FlexRayFrame(
            timestamp=timestamp,
            channel=channel,
            slot=slot & 0x7FF,
            cycle=cycle & 0x3F,
            payload=bytes(payload[:MAX_PAYLOAD]),
            null_frame=bool(flags & FLAG_NULL),
            sync=bool(flags & FLAG_SYNC),
            startup=bool(flags & FLAG_STARTUP),
            preamble=bool(flags & FLAG_PREAMBLE),
            errors=list(errors),
            header_crc=crcs[0] if channel == 'A' else crcs[1],
            transmitted=transmitted,
        )


def _status_event(timestamp: float, mask: int, cycle: int, cc_type: int, wus: int, data0: int, symbol: int):
    channel = _channel_text(mask)
    if symbol in SYMBOLS:
        return FlexRayEvent(timestamp, 'symbol', SYMBOLS[symbol], channel, cycle)
    if cc_type == CC_VN and wus == 0 and data0 in POC_STATES:
        return FlexRayEvent(timestamp, 'status', f'POC state: {POC_STATES[data0]}', channel, cycle)
    return None


def _error_event(timestamp: float, mask: int, cycle: Optional[int], cc_type: int, data: Tuple[int, ...]):
    channel = _channel_text(mask)
    if cc_type == CC_VN and data and data[0] in VN_ERRORS:
        text = VN_ERRORS[data[0]]
        if data[0] == 0 and len(data) > 1 and data[1] in POC_ERROR_MODES:
            text += f': {POC_ERROR_MODES[data[1]]}'
        elif data[0] == 5 and len(data) > 1:
            flags = [name for bit, name in CC_ERROR_FLAGS.items() if data[1] & bit]
            text += f': {", ".join(flags)}' if flags else ''
        return FlexRayEvent(timestamp, 'error', text[:1].upper() + text[1:], channel, cycle)
    return FlexRayEvent(timestamp, 'error', f'Controller error ({", ".join(f"0x{d:X}" for d in data)})', channel, cycle)


# --- BLF ---


def _blf_objects(data: bytes) -> Iterator[Tuple[int, float, bytes]]:
    """(object type, timestamp, body) of every object, containers unpacked."""
    from can.io.blf import FILE_HEADER_STRUCT, systemtime_to_timestamp

    if len(data) < FILE_HEADER_STRUCT.size or data[:4] != b'LOGG':
        raise FlexRayError('Not a BLF file')
    header = FILE_HEADER_STRUCT.unpack_from(data)
    start = systemtime_to_timestamp(header[14:22])
    pending = b''
    position = header[1]
    while position + 16 <= len(data):
        signature, _, _, size, kind = struct.unpack_from('<4sHHLL', data, position)
        if signature != b'LOBJ' or size < 16:
            raise FlexRayError(f'Damaged BLF file (no object at byte {position})')
        obj = data[position : position + size]
        body = obj[16:]
        position += size + size % 4
        if kind != LOG_CONTAINER:
            yield from _blf_object(obj, start)
            continue
        method = struct.unpack_from('<H', body, 0)[0]
        contents = body[16:]
        if method == 2:
            try:
                contents = zlib.decompress(contents)
            except zlib.error as e:
                raise FlexRayError(f'Damaged BLF container: {e}') from e
        elif method != 0:
            continue
        pending += contents
        offset = 0
        while True:
            found = pending.find(b'LOBJ', offset, offset + 8)
            if found < 0 or found + 16 > len(pending):
                break
            size = struct.unpack_from('<L', pending, found + 8)[0]
            if size < 16:
                raise FlexRayError('Damaged BLF object')
            if found + size > len(pending):
                break  # Continues in the next container
            yield from _blf_object(pending[found : found + size], start)
            offset = found + size
        pending = pending[offset:]


def _blf_object(obj: bytes, start: float) -> Iterator[Tuple[int, float, bytes]]:
    _, header_size, version, _, kind = struct.unpack_from('<4sHHLL', obj, 0)
    if version == 1:
        flags, _, _, stamp = struct.unpack_from('<LHHQ', obj, 16)
    elif version == 2:
        flags, _, _, stamp = struct.unpack_from('<LBxHQ', obj, 16)
    else:
        return
    factor = Decimal('1e-5') if flags == 1 else Decimal('1e-9')
    yield kind, float(Decimal(stamp) * factor) + start, obj[header_size:]


def read_blf(data: bytes) -> FlexRayLog:
    """The FlexRay frames and events of a BLF file's contents."""
    log = FlexRayLog(format='BLF')
    for kind, timestamp, body in _blf_objects(data):
        try:
            if kind == FR_RCVMESSAGE_EX and len(body) >= 84:
                (_, _, mask, direction, _, _, slot, crc1, crc2, _, count, cycle, cc_type, state, flags, *_rest) = (
                    _RCV_EX.unpack_from(body)
                )
                payload = body[84 : 84 + count]
            elif kind == FR_RCVMESSAGE and len(body) >= 44:
                (_, _, mask, direction, _, _, slot, crc1, crc2, _, count, cycle, cc_type, state, flags, _) = (
                    _RCV.unpack_from(body)
                )
                payload = body[44 : 44 + count]
            elif kind == FR_STATUS and len(body) >= _STATUS.size:
                _, _, mask, cycle, _, _, wus, _, cc_type, data0, _, symbol = _STATUS.unpack_from(body)
                event = _status_event(timestamp, mask, cycle & 0x3F, cc_type, wus, data0, symbol)
                if event is not None:
                    log.events.append(event)
                continue
            elif kind == FR_ERROR and len(body) >= _ERROR.size:
                _, _, mask, cycle, _, _, cc_type, *data = _ERROR.unpack_from(body)
                log.events.append(_error_event(timestamp, mask, cycle & 0x3F, cc_type, tuple(data)))
                continue
            else:
                if kind in OBSOLETE_TYPES:
                    log.skipped += 1
                continue
        except struct.error:
            log.skipped += 1
            continue
        if flags & FLAG_PDU or direction == 2:
            log.skipped += 1  # A PDU entry (its frame is logged too) or a transmit request
            continue
        log.frames.extend(
            _frames(timestamp, mask, slot, cycle, flags, cc_type, state, payload, (crc1, crc2), direction == 1)
        )
    return log


# --- ASC ---


def read_asc(text: str) -> FlexRayLog:
    """The FlexRay frames and events of an ASC file's text. Raises FlexRayError when it is not an ASC file."""
    log = FlexRayLog(format='ASC')
    base = 16
    seen_header = False
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == 'base' and len(words) >= 2:
            base = 16 if words[1].lower() == 'hex' else 10
            seen_header = True
            continue
        if words[0] in ('date', 'Begin', 'End', 'internal', 'no'):
            seen_header = True
            continue
        if len(words) < 3 or words[1] != 'Fr':
            continue
        try:
            timestamp = float(words[0])
            if words[2] == 'RMSG':
                log.frames.extend(_asc_rmsg(timestamp, words, base))
            elif words[2] == 'SE':
                event = _asc_status(timestamp, words, base)
                if event is not None:
                    log.events.append(event)
            elif words[2] == 'EE':
                mask, cc_type = int(words[6]), int(words[7], base)
                data = tuple(int(word, base) for word in words[8:12])
                log.events.append(_error_event(timestamp, mask, None, cc_type, data))
            elif len(words) > 3 and words[3] == 'V9' and words[2] in ('1', '2'):
                log.frames.append(_asc_v9(timestamp, words, base))
            else:
                log.skipped += words[2] == 'PDU'
        except (ValueError, IndexError, FlexRayError):
            log.skipped += 1
    if not seen_header and not log.frames:
        raise FlexRayError('Not a pcap, pcapng, BLF or ASC file')
    return log


def _asc_rmsg(timestamp: float, words: List[str], base: int) -> Iterator[FlexRayFrame]:
    # 0 time, 1 Fr, 2 RMSG, 3 cluster, 4 client, 5 channel, 6 mask, 7 slot, 8 cycle, 9 dir, 10 app parameter,
    # 11 flags, 12 controller type, 13 frame state, 14 header CRC, 15 name, 16 payload length, 17 stored length, bytes
    mask = int(words[6])
    direction = words[9]
    if direction == 'TxRq':
        return iter(())
    count = int(words[17], base)
    payload = bytes(int(word, base) for word in words[18 : 18 + count])
    if len(payload) != count:
        raise FlexRayError('A truncated Fr RMSG line')
    crc = int(words[14], base)
    return _frames(
        timestamp,
        mask,
        int(words[7], base),
        int(words[8], base),
        int(words[11], base),
        int(words[12], base),
        int(words[13], base),
        payload,
        (crc, crc),
        direction == 'Tx',
    )


def _asc_v9(timestamp: float, words: List[str], base: int) -> FlexRayFrame:
    # 0 time, 1 Fr, 2 channel, 3 V9, 4 slot, 5 cycle, 6 payload preamble (NM), 7 sync, 8 header CRC, 9 name,
    # 10 length, bytes, frame state, header bit mask
    count = int(words[10], base)
    payload = bytes(int(word, base) for word in words[11 : 11 + count])
    if len(payload) != count or len(words) < 13 + count:
        raise FlexRayError('A truncated Fr V9 line')
    return FlexRayFrame(
        timestamp=timestamp,
        channel='A' if words[2] == '1' else 'B',
        slot=int(words[4], base) & 0x7FF,
        cycle=int(words[5]) & 0x3F,
        payload=payload[:MAX_PAYLOAD],
        preamble=words[6] == '1',
        sync=words[7] == '1',
        header_crc=int(words[8], base),
    )


def _asc_status(timestamp: float, words: List[str], base: int) -> Optional[FlexRayEvent]:
    # 0 time, 1 Fr, 2 SE, 3 cluster, 4 client, 5 channel, 6 mask, 7 cycle, 8 controller type, 9 sync state,
    # 10 data0, 11 data1, 12 symbol, 13 wakeup state, 14 spy flag. Lines with other fields are not read.
    if len(words) != 15:
        raise FlexRayError('An Fr SE line of an unknown layout')
    return _status_event(
        timestamp,
        int(words[6]),
        int(words[7], base) & 0x3F,
        int(words[8]),
        int(words[13]),
        int(words[10], base),
        int(words[12]),
    )
