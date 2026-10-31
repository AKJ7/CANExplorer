"""Passive SAE J1587 decoder: the application layer of the older heavy-truck diagnostic bus (engine, transmission,
brakes…). A J1587 message is a MID (the module that sent it) followed by parameters, each a PID and its data.

J1587 runs over J1708, which is a 9600-baud serial (RS-485) bus, **not** CAN — so there is no live J1708 capture
through a CAN interface. This decoder works on J1587 message bytes: a J1708 serial log, a gateway that bridges
J1708 frames onto the bus, or the scripting console. Each frame's data is treated as one complete J1587 message
(MID, parameters and, when present, the trailing checksum).

A subset of MIDs and PIDs is named, and the well-known parameters are scaled; the rest are shown as raw bytes.
"""

from typing import Callable, Dict, Optional

import can

MID_NAMES = {
    128: 'Engine #1',
    130: 'Transmission',
    136: 'Brakes, Power Unit (ABS)',
    137: 'Brakes, Trailer (ABS)',
    140: 'Instrument Cluster',
    142: 'Vehicle Management System',
    143: 'Fuel System',
    162: 'Vehicle Navigation',
    171: 'Driver Information',
    178: 'Vehicle Security',
    181: 'Communication Unit',
    190: 'Engine #2',
}

PID_NAMES = {
    84: 'Road Speed',
    85: 'Cruise Control Set Speed',
    91: 'Accelerator Pedal',
    92: 'Engine Load',
    100: 'Engine Oil Pressure',
    102: 'Boost Pressure',
    110: 'Engine Coolant Temperature',
    190: 'Engine Speed',
    191: 'Transmission Output Shaft Speed',
    182: 'Trip Fuel',
    245: 'Total Vehicle Distance',
    247: 'Total Engine Hours',
}


def _mph(data: bytes) -> str:
    return f'{data[0] * 0.5:.0f} mph' if data else '—'  # PID 84/85: 0.5 mph/bit


def _rpm(data: bytes) -> str:
    return f'{int.from_bytes(data[:2], "little") * 0.25:.0f} rpm' if len(data) >= 2 else '—'  # 0.25 rpm/bit


def _accelerator(data: bytes) -> str:
    return f'{data[0] * 0.4:.0f} %' if data else '—'  # PID 91: 0.4 %/bit


def _load(data: bytes) -> str:
    return f'{data[0] * 0.5:.0f} %' if data else '—'  # PID 92: 0.5 %/bit


# PID -> how to render its data bytes (the well-known, confidently scaled parameters only)
PID_DECODERS: Dict[int, Callable[[bytes], str]] = {
    84: _mph,
    85: _mph,
    91: _accelerator,
    92: _load,
    190: _rpm,
    191: _rpm,
}


def mid_label(mid: int) -> str:
    name = MID_NAMES.get(mid)
    return f'MID {mid}' + (f' {name}' if name else '')


def pid_label(pid: int) -> str:
    name = PID_NAMES.get(pid)
    return f'PID {pid}' + (f' {name}' if name else '')


def _pid_length(pid: int) -> Optional[int]:
    """Bytes of a PID's data from its number: 0-127 one byte, 128-191 two bytes, 192+ variable (a count byte
    precedes the data). Returns None for a variable-length PID."""
    number = pid if pid < 256 else pid - 256
    if number <= 127:
        return 1
    if number <= 191:
        return 2
    return None


def decode_j1587(data: bytes) -> Optional[str]:
    """Decode one J1587 message (MID, parameters, optional trailing checksum). Returns a readable description."""
    if len(data) < 2:
        return None
    body = data[:-1] if len(data) >= 3 and sum(data) % 256 == 0 else data  # Drop a valid trailing checksum
    parts = [mid_label(body[0])]
    index = 1
    while index < len(body):
        pid = body[index]
        index += 1
        if pid == 255:  # Page-2 extension: the next byte is the PID on page 2
            if index >= len(body):
                break
            pid = 256 + body[index]
            index += 1
        length = _pid_length(pid)
        if length is None:  # Variable length: a count byte precedes the data
            if index >= len(body):
                break
            length = body[index]
            index += 1
        if index + length > len(body):
            parts.append(f'{pid_label(pid)}=…')
            break
        chunk = body[index : index + length]
        index += length
        decoder = PID_DECODERS.get(pid)
        parts.append(f'{pid_label(pid)} {decoder(chunk)}' if decoder is not None else f'{pid_label(pid)}={chunk.hex()}')
    return ', '.join(parts)


class J1587Decoder:
    """Passive SAE J1587 decoder. Each frame's data is treated as one J1587 message (see the module docstring on
    the J1708 serial transport, which is not CAN)."""

    name = 'J1587'

    def __init__(self, preview_bytes: int = 32):
        self._preview = preview_bytes

    def reset(self) -> None:
        pass

    def describe(self, msg: can.Message) -> Optional[str]:
        data = bytes(msg.data)
        if not data:
            return None
        return decode_j1587(data)
