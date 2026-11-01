import can

from can_explorer.core.protocols.nmea2000 import NmeaDecoder


def msg(can_id, data):
    return can.Message(arbitration_id=can_id, is_extended_id=True, data=bytes(data))


def nmea_id(priority, pgn, source):
    # PDU2 (broadcast) PGNs: identifier = priority<<26 | pgn<<8 | source
    return (priority << 26) | (pgn << 8) | source


def test_engine_rapid_update_is_decoded():
    # PGN 127488, engine instance 0, speed 2400 rpm (raw 9600 at 0.25 rpm/bit)
    raw = (9600).to_bytes(2, 'little')
    description = NmeaDecoder().describe(msg(nmea_id(2, 127488, 0x23), [0x00, *raw, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]))
    assert description == 'PGN 127488 Engine Parameters, Rapid Update SA=35 P2: engine 0: 2400 rpm'


def test_position_rapid_update_latitude_longitude():
    lat = round(37.7749 / 1e-7).to_bytes(4, 'little', signed=True)
    lon = round(-122.4194 / 1e-7).to_bytes(4, 'little', signed=True)
    description = NmeaDecoder().describe(msg(nmea_id(3, 129025, 0x10), [*lat, *lon]))
    assert 'lat 37.774900°, lon -122.419400°' in description


def test_not_available_values_show_dash():
    description = NmeaDecoder().describe(msg(nmea_id(2, 128267, 0x20), [0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00]))
    assert description.endswith('depth —')


def test_fast_packet_is_reassembled():
    decoder = NmeaDecoder()
    # PGN 129029 (GNSS Position Data) is a Fast Packet PGN; 14 bytes over two frames
    payload = bytes(range(1, 15))
    first = decoder.describe(msg(nmea_id(3, 129029, 0x05), [0x00, len(payload), *payload[:6]]))
    assert first.endswith('Fast Packet …')
    second = decoder.describe(msg(nmea_id(3, 129029, 0x05), [0x01, *payload[6:13]]))
    # 6 + 7 = 13 bytes so far, one short; the third frame completes it
    assert second.endswith('Fast Packet …')
    third = decoder.describe(msg(nmea_id(3, 129029, 0x05), [0x02, payload[13], *[0xFF] * 6]))
    assert 'complete (14 B): 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E' in third


def test_standard_frames_are_ignored():
    assert NmeaDecoder().describe(can.Message(arbitration_id=0x100, is_extended_id=False)) is None
