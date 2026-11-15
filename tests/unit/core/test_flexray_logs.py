"""FlexRay in Vector BLF and ASC logs, and symbols in pcap captures.

The BLF objects are built with the member offsets of Vector's FlexRay BLF specification, as checked against files
written by Vector's binlog library (core/flexray_logs.py)."""

import struct
import zlib

import pytest

from can_explorer.core import flexray
from can_explorer.core.flexray import FlexRayError, FlexRayEvent, FlexRayFrame
from can_explorer.core.flexray_logs import read_asc, read_blf

# --- Building BLF files ---


def _object(kind: int, body: bytes, nanoseconds: int) -> bytes:
    header = struct.pack('<4sHHLL', b'LOBJ', 32, 1, 32 + len(body), kind)
    header += struct.pack('<LHHQ', 2, 0, 0, nanoseconds)  # Object header v1, flags 2: nanoseconds
    return header + body + bytes(len(body) % 4)


def _receive_ex(slot, cycle, payload, mask=1, direction=0, flags=0x02, cc_type=5, state=0x20, crc=(0x123, 0x456)):
    body = struct.pack(
        '<HHHHIIHHHHHHIIIIIIHHH',
        1,  # channel
        1,  # version
        mask,
        direction,
        0,  # client index
        0,  # cluster number
        slot,
        crc[0],
        crc[1],
        len(payload),  # byte count
        len(payload),  # stored bytes
        cycle,
        cc_type,
        state,
        flags,
        0,  # application parameter
        0,  # frame CRC
        0,  # frame length in ns
        0,  # frame ID of the PDU's frame
        0,  # PDU offset
        0,  # BLF log mask
    )
    return body.ljust(84, b'\0') + payload


def _receive(slot, cycle, payload, mask=2, flags=0x02):
    body = struct.pack(
        '<HHHBxIIHHHHHBxIIII', 2, 1, mask, 0, 0, 1, slot, 0, 0, len(payload), len(payload), cycle, 4, 0, flags, 0
    )
    return body + payload


def _status(symbol=0, mask=1, cycle=7, cc_type=5, wus=0, poc=0x02):
    return struct.pack('<HHHBxIIIIIIIH', 1, 1, mask, cycle, 0, 0, wus, 0, cc_type, poc, 0, symbol) + bytes(32)


def _error(tag, data1, mask=3, cycle=9):
    return struct.pack('<HHHBxIIIIIII', 1, 1, mask, cycle, 0, 0, 5, tag, data1, 0, 0) + bytes(4)


def _container(objects: bytes, compress: bool) -> bytes:
    data = zlib.compress(objects) if compress else objects
    body = struct.pack('<H6xL4x', 2 if compress else 0, len(objects)) + data
    header = struct.pack('<4sHHLL', b'LOBJ', 16, 1, 16 + len(body), 10)
    return header + body + bytes(len(body) % 4)


def blf_file(containers) -> bytes:
    from can.io.blf import FILE_HEADER_STRUCT

    start = (2026, 11, 0, 15, 8, 0, 0, 0)  # SYSTEMTIME: year, month, day of week, day, hour, minute, s, ms
    header = FILE_HEADER_STRUCT.pack(b'LOGG', 144, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, *start, *start)
    return header.ljust(144, b'\0') + b''.join(containers)


def test_blf_frames_symbols_and_errors(tmp_path):
    objects = b''.join(
        [
            _object(66, _receive_ex(12, 4, bytes(range(8)), mask=3, flags=0x02 | 0x04 | 0x08), 1_000_000),
            _object(66, _receive_ex(13, 4, b'', flags=0x01 | 0x02, state=0x300), 2_000_000),  # Null, CRC errors
            _object(66, _receive_ex(14, 4, b'\x01\x02', direction=1), 3_000_000),  # Sent by the logger's node
            _object(66, _receive_ex(15, 4, b'\x01\x02', direction=2), 3_500_000),  # A Tx request: not on the bus
            _object(66, _receive_ex(16, 4, b'\x01\x02', flags=0x10002), 3_600_000),  # A PDU entry
            _object(50, _receive(20, 5, b'\xaa\xbb'), 4_000_000),  # The older receive object
            _object(48, _status(symbol=1), 5_000_000),  # CAS
            _object(48, _status(), 6_000_000),  # POC state normal active
            _object(47, _error(0, 1), 7_000_000),  # POC mode: passive
            _object(47, _error(5, 0x00010000), 8_000_000),  # Controller error: error on channel A
            _object(41, bytes(80), 9_000_000),  # An obsolete type
        ]
    )
    # Split across two containers, the second compressed: objects may continue in the next container
    middle = len(objects) // 2 + 3
    log = read_blf(blf_file([_container(objects[:middle], False), _container(objects[middle:], True)]))
    assert log.format == 'BLF' and log.skipped == 3
    frames = [(f.channel, f.slot, f.cycle, f.payload, f.sync, f.startup, f.null_frame) for f in log.frames]
    assert frames == [
        ('A', 12, 4, bytes(range(8)), True, True, False),
        ('B', 12, 4, bytes(range(8)), True, True, False),  # Channel mask 3: on both
        ('A', 13, 4, b'', False, False, True),
        ('A', 14, 4, b'\x01\x02', False, False, False),
        ('B', 20, 5, b'\xaa\xbb', False, False, False),
    ]
    assert log.frames[0].header_crc == 0x123 and log.frames[1].header_crc == 0x456
    assert log.frames[2].errors == ['header CRC', 'frame CRC']  # VN frame state bits 8, 9
    assert log.frames[3].transmitted and not log.frames[0].transmitted
    start = log.frames[0].timestamp
    assert log.frames[3].timestamp - start == pytest.approx(0.002, abs=1e-6)  # Absolute floats: µs
    assert [(e.kind, e.text, e.channel, e.cycle) for e in log.events] == [
        ('symbol', 'CAS', 'A', 7),
        ('status', 'POC state: normal active', 'A', 7),
        ('error', 'POC mode changed: passive', 'A+B', 9),
        ('error', 'Controller error: error detected on channel A', 'A+B', 9),
    ]
    # Through read_log, by its contents
    path = tmp_path / 'drive.blf'
    path.write_bytes(blf_file([_container(objects, True)]))
    assert len(flexray.read_log(path).frames) == 5
    with pytest.raises(FlexRayError, match='Damaged BLF'):
        read_blf(blf_file([b'XXXX' + bytes(12)]))


ASC = """date Sun Nov 15 08:00:00.000 am 2026
base {base}  timestamps absolute
internal events logged
Begin Triggerblock Sun Nov 15 08:00:00.000 am 2026
   0.000000 Start of measurement
   0.010000 Fr RMSG 0 0 1 1 {slot} 7 Rx 0 {flags} 5 {state} {crc} EngineStatus 8 8 {data} 0 0 0
   0.010010 Fr RMSG 0 0 1 3 2 7 Rx 0 3 5 32 96 x 4 0 0 0 0
   0.010020 Fr RMSG 0 0 1 2 3 7 Tx 0 2 5 32 96 x 2 2 {two} 0 0 0
   0.010030 Fr RMSG 0 0 1 1 4 7 TxRq 0 2 5 32 96 x 2 2 {two} 0 0 0
   0.010040 Fr PDU 0 0 1 1 5 7 Rx 0 2 5 32 96 x 2 2 {two} 0 0 0 0
   0.020000 Fr 2 V9 {slot} 8 0 1 {crc} x 4 {four} 01d0 9a
   0.030000 Fr SE 0 0 1 1 9 5 0 0 0 1 0 0
   0.040000 Fr EE 0 0 1 2 5 5 {flag} 0 0
End TriggerBlock
"""


@pytest.mark.parametrize('base', ['dec', 'hex'])
def test_asc_frames_and_events(base):
    def number(value):
        return f'{value:x}' if base == 'hex' else str(value)

    text = ASC.format(
        base=base,
        slot=number(26),
        flags=number(0x0E),  # Valid, sync, startup
        state=number(0x320),  # VN: header and frame CRC errors (bits 8, 9) and valid (5)
        crc=number(1193),
        data=' '.join(number(b) for b in range(10, 18)),
        two=' '.join(number(b) for b in (0xAB, 0xCD)),
        four=' '.join(number(b) for b in (1, 2, 3, 250)),
        flag=number(0x10000),
    )
    log = read_asc(text)
    assert log.format == 'ASC' and log.skipped == 1  # The PDU line
    rows = [(f.timestamp, f.channel, f.slot, f.cycle, f.payload, f.sync, f.startup, f.null_frame) for f in log.frames]
    assert rows == [
        (0.01, 'A', 26, 7, bytes(range(10, 18)), True, True, False),
        (0.01001, 'A', 2, 7, b'', False, False, True),  # Null frame on both channels
        (0.01001, 'B', 2, 7, b'', False, False, True),
        (0.01002, 'B', 3, 7, b'\xab\xcd', False, False, False),  # TxRq is skipped
        (0.02, 'B', 26, 8, bytes([1, 2, 3, 250]), True, False, False),  # V9: channel 2 is B
    ]
    assert log.frames[0].errors == ['header CRC', 'frame CRC'] and log.frames[0].header_crc == 1193
    assert log.frames[3].transmitted
    assert [(e.kind, e.text, e.channel) for e in log.events] == [
        ('symbol', 'CAS', 'A'),
        ('error', 'Controller error: error detected on channel A', 'B'),
    ]


def test_not_a_log():
    with pytest.raises(FlexRayError, match='Not a pcap, pcapng, BLF or ASC'):
        read_asc('VERSION ""\nBO_ 1 X: 8 Y\n')


def test_pcap_symbols_round_trip(tmp_path):
    path = tmp_path / 'symbols.pcap'
    writer = flexray.CaptureWriter(path)
    writer.write(FlexRayFrame(1.0, 'A', 5, 1, b'\x01\x02'))
    writer.write_symbol(FlexRayEvent(1.001, 'symbol', 'CAS', 'B'), length=30)
    writer.close()
    log = flexray.read_log(path)
    assert log.format == 'pcap' and len(log.frames) == 1
    assert [(e.kind, e.channel, e.text) for e in log.events] == [('symbol', 'B', 'Symbol (length 30)')]
    # The LINKTYPE_FLEXRAY symbol packet: measurement header (channel B, type 2) and the symbol length
    assert path.read_bytes()[-2:] == bytes([0x82, 30])
