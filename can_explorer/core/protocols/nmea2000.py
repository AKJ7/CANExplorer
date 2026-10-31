"""Passive NMEA 2000 decoder: the marine CAN protocol, built on SAE J1939 addressing (29-bit identifiers at
250 kbit/s). It reuses J1939's identifier fields and adds the NMEA 2000 PGN set, Fast Packet reassembly (NMEA
2000's own multi-frame transport, distinct from J1939's TP/BAM) and field decoding for the common PGNs.

Only a subset of PGNs is decoded into fields; the rest are named and shown as hex. Values use NMEA 2000's standard
resolutions, with marine units (knots, degrees, metres, °C) where natural; a field at its "not available" value is
shown as '—'.
"""

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

import can

from can_explorer.core.frames import format_hex
from can_explorer.core.protocols.j1939 import J1939Id

RAD_TO_DEG = 180.0 / math.pi
MS_TO_KNOTS = 1.0 / 0.514444

# PGN -> name (decimal, as NMEA 2000 is conventionally written)
PGN_NAMES = {
    59392: 'ISO Acknowledgement',
    59904: 'ISO Request',
    60928: 'ISO Address Claim',
    126992: 'System Time',
    126993: 'Heartbeat',
    126996: 'Product Information',
    126998: 'Configuration Information',
    127245: 'Rudder',
    127250: 'Vessel Heading',
    127251: 'Rate of Turn',
    127257: 'Attitude',
    127258: 'Magnetic Variation',
    127488: 'Engine Parameters, Rapid Update',
    127489: 'Engine Parameters, Dynamic',
    127493: 'Transmission Parameters, Dynamic',
    127497: 'Trip Parameters, Engine',
    127505: 'Fluid Level',
    127508: 'Battery Status',
    128259: 'Speed',
    128267: 'Water Depth',
    128275: 'Distance Log',
    129025: 'Position, Rapid Update',
    129026: 'COG & SOG, Rapid Update',
    129029: 'GNSS Position Data',
    129033: 'Time & Date',
    129283: 'Cross Track Error',
    129284: 'Navigation Data',
    129285: 'Route/WP Information',
    129540: 'GNSS Sats in View',
    130306: 'Wind Data',
    130310: 'Environmental Parameters',
    130311: 'Environmental Parameters',
    130312: 'Temperature',
    130313: 'Humidity',
    130314: 'Actual Pressure',
    130316: 'Temperature, Extended Range',
}

# PGNs that use Fast Packet (payloads longer than one 8-byte frame)
FAST_PACKET_PGNS = {
    126996,
    126998,
    127489,
    127497,
    129029,
    129284,
    129285,
    129540,
    130074,
    130577,
}


def _u(data: bytes, offset: int, size: int) -> Optional[int]:
    """An unsigned little-endian integer, or None when it is out of the payload or at its 'not available' value."""
    if offset + size > len(data):
        return None
    value = int.from_bytes(data[offset : offset + size], 'little')
    return None if value == (1 << (size * 8)) - 1 else value


def _s(data: bytes, offset: int, size: int) -> Optional[int]:
    if offset + size > len(data):
        return None
    value = int.from_bytes(data[offset : offset + size], 'little', signed=True)
    return None if value == (1 << (size * 8 - 1)) - 1 else value


def _fmt(value: Optional[float], unit: str = '', digits: int = 1) -> str:
    return '—' if value is None else f'{value:.{digits}f}{unit}'


def _engine_rapid(data: bytes) -> str:
    instance = data[0] if data else 0
    speed = _u(data, 1, 2)
    rpm = None if speed is None else speed * 0.25
    return f'engine {instance}: {_fmt(rpm, " rpm", 0)}'


def _position_rapid(data: bytes) -> str:
    lat, lon = _s(data, 0, 4), _s(data, 4, 4)
    return f'lat {_fmt(None if lat is None else lat * 1e-7, "°", 6)}, lon {_fmt(None if lon is None else lon * 1e-7, "°", 6)}'


def _cog_sog_rapid(data: bytes) -> str:
    cog, sog = _u(data, 2, 2), _u(data, 4, 2)
    cog_deg = None if cog is None else cog * 0.0001 * RAD_TO_DEG
    sog_kn = None if sog is None else sog * 0.01 * MS_TO_KNOTS
    return f'COG {_fmt(cog_deg, "°")}, SOG {_fmt(sog_kn, " kn")}'


def _water_depth(data: bytes) -> str:
    depth = _u(data, 1, 4)
    return f'depth {_fmt(None if depth is None else depth * 0.01, " m", 2)}'


def _speed(data: bytes) -> str:
    stw = _u(data, 1, 2)
    return f'STW {_fmt(None if stw is None else stw * 0.01 * MS_TO_KNOTS, " kn")}'


def _wind(data: bytes) -> str:
    speed, angle = _u(data, 1, 2), _u(data, 3, 2)
    speed_kn = None if speed is None else speed * 0.01 * MS_TO_KNOTS
    angle_deg = None if angle is None else angle * 0.0001 * RAD_TO_DEG
    return f'wind {_fmt(speed_kn, " kn")} @ {_fmt(angle_deg, "°", 0)}'


def _heading(data: bytes) -> str:
    heading = _u(data, 1, 2)
    return f'heading {_fmt(None if heading is None else heading * 0.0001 * RAD_TO_DEG, "°")}'


def _battery(data: bytes) -> str:
    instance = data[0] if data else 0
    voltage, current = _s(data, 1, 2), _s(data, 3, 2)
    return (
        f'battery {instance}: {_fmt(None if voltage is None else voltage * 0.01, " V", 2)}, '
        f'{_fmt(None if current is None else current * 0.1, " A")}'
    )


FIELD_DECODERS: Dict[int, Callable[[bytes], str]] = {
    127488: _engine_rapid,
    129025: _position_rapid,
    129026: _cog_sog_rapid,
    128267: _water_depth,
    128259: _speed,
    130306: _wind,
    127250: _heading,
    127508: _battery,
}


@dataclass(slots=True)
class _FastPacket:
    size: int
    data: bytearray = field(default_factory=bytearray)
    next_frame: int = 1


class NmeaDecoder:
    """Passive NMEA 2000 decoder: identifier fields, Fast Packet reassembly and the common PGNs."""

    name = 'NMEA 2000'

    def __init__(self, preview_bytes: int = 32):
        self._preview = preview_bytes
        self._sessions: Dict[Tuple[int, int, int], _FastPacket] = {}  # (source, pgn, sequence) -> session

    def reset(self) -> None:
        self._sessions.clear()

    @staticmethod
    def pgn_label(pgn: int) -> str:
        name = PGN_NAMES.get(pgn)
        return f'PGN {pgn}' + (f' {name}' if name else '')

    def describe(self, msg: can.Message) -> Optional[str]:
        if not msg.is_extended_id:
            return None
        nmea_id = J1939Id.from_can_id(msg.arbitration_id)
        pgn = nmea_id.pgn
        header = f'{self.pgn_label(pgn)} SA={nmea_id.source_address} P{nmea_id.priority}'
        data = bytes(msg.data)
        if pgn in FAST_PACKET_PGNS:
            assembled = self._fast_packet(nmea_id.source_address, pgn, data)
            if assembled is None:
                return f'{header}: Fast Packet …'
            payload, note = assembled
            detail = self._decode(pgn, payload)
            return f'{header}: {note}' + (f' — {detail}' if detail else '')
        detail = self._decode(pgn, data)
        return f'{header}: {detail}' if detail else header

    @staticmethod
    def _decode(pgn: int, payload: bytes) -> Optional[str]:
        decoder = FIELD_DECODERS.get(pgn)
        return decoder(payload) if decoder is not None else None

    def _fast_packet(self, source: int, pgn: int, data: bytes) -> Optional[Tuple[bytes, str]]:
        """Reassemble a Fast Packet sequence. The first byte is (sequence << 5) | frame; the first frame (frame 0)
        carries the total byte count in its second byte, then 6 data bytes; later frames carry 7 data bytes."""
        if not data:
            return None
        sequence = (data[0] >> 5) & 0x7
        frame = data[0] & 0x1F
        key = (source, pgn, sequence)
        if frame == 0:
            if len(data) < 2:
                return None
            session = self._sessions[key] = _FastPacket(size=data[1])
            session.data.extend(data[2:8])
        else:
            session = self._sessions.get(key)
            if session is None or frame != session.next_frame:
                self._sessions.pop(key, None)
                return None
            session.data.extend(data[1:8])
            session.next_frame += 1
        if len(session.data) >= session.size:
            del self._sessions[key]
            payload = bytes(session.data[: session.size])
            return payload, f'complete ({session.size} B): {format_hex(payload, self._preview)}'
        return None
