from dataclasses import dataclass
from typing import Optional

import can


@dataclass(slots=True)
class Frame:
    """A received or transmitted CAN message together with its decoded description."""

    message: can.Message
    info: str = ''


def format_hex(data: bytes | bytearray, limit: Optional[int] = None) -> str:
    shown = data if limit is None else data[:limit]
    text = ' '.join(f'{byte:02X}' for byte in shown)
    if limit is not None and len(data) > limit:
        text += ' …'
    return text


def parse_hex_bytes(text: str) -> bytes:
    """Parse '01 02 AA', '0102AA' or '01,02,AA' into bytes."""
    cleaned = text.replace(',', ' ').strip()
    if not cleaned:
        return b''
    tokens = cleaned.split()
    if len(tokens) == 1 and len(tokens[0]) > 2:
        token = tokens[0].removeprefix('0x').removeprefix('0X')
        if len(token) % 2:
            raise ValueError(f'Odd number of hex digits: {text!r}')
        tokens = [token[i : i + 2] for i in range(0, len(token), 2)]
    try:
        values = [int(token, 16) for token in tokens]
    except ValueError:
        raise ValueError(f'Invalid hex data: {text!r}') from None
    if any(not 0 <= value <= 0xFF for value in values):
        raise ValueError(f'Byte out of range in: {text!r}')
    return bytes(values)


def parse_can_id(text: str, extended: bool) -> int:
    try:
        can_id = int(text.strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid hex identifier: {text!r}') from None
    limit = 0x1FFFFFFF if extended else 0x7FF
    if not 0 <= can_id <= limit:
        raise ValueError(f'Identifier {can_id:#x} out of range (max {limit:#x})')
    return can_id


def frame_type(msg: can.Message) -> str:
    parts = ['Ext' if msg.is_extended_id else 'Std']
    if msg.is_fd:
        parts.append('FD')
    if msg.is_remote_frame:
        parts.append('RTR')
    if msg.is_error_frame:
        parts.append('ERR')
    return ' '.join(parts)


def clone_message(msg: can.Message) -> can.Message:
    return can.Message(
        arbitration_id=msg.arbitration_id,
        is_extended_id=msg.is_extended_id,
        is_remote_frame=msg.is_remote_frame,
        is_fd=msg.is_fd,
        bitrate_switch=msg.bitrate_switch,
        data=bytes(msg.data),
        dlc=msg.dlc,
        channel=msg.channel,
    )
