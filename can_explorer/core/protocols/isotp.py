from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import can

from can_explorer.core.frames import format_hex

FLOW_STATUS = {0: 'CTS', 1: 'WAIT', 2: 'OVFLW'}


@dataclass(slots=True)
class _Transfer:
    length: int
    data: bytearray = field(default_factory=bytearray)
    next_sn: int = 1


class IsoTpDecoder:
    """Passive ISO 15765-2 decoder (normal addressing): labels frames and reassembles segmented payloads."""

    name = 'ISO-TP'

    def __init__(self, preview_bytes: int = 32):
        self._preview = preview_bytes
        self._transfers: Dict[Tuple[int, bool], _Transfer] = {}

    def reset(self) -> None:
        self._transfers.clear()

    def describe(self, msg: can.Message) -> Optional[str]:
        return self.feed(msg)[0]

    def feed(self, msg: can.Message) -> Tuple[Optional[str], Optional[bytes]]:
        """Returns (description, payload) where payload is set when a message is complete."""
        data = msg.data
        if not data:
            return None, None
        key = (msg.arbitration_id, msg.is_extended_id)
        match data[0] >> 4:
            case 0:
                length, offset = data[0] & 0x0F, 1
                if length == 0 and len(data) > 8:  # CAN FD escape sequence
                    length, offset = data[1], 2
                if length == 0 or length > len(data) - offset:
                    return None, None
                self._transfers.pop(key, None)
                payload = bytes(data[offset : offset + length])
                return f'ISO-TP SF ({length} B): {format_hex(payload, self._preview)}', payload
            case 1:
                if len(data) < 2:
                    return None, None
                length, offset = ((data[0] & 0x0F) << 8) | data[1], 2
                if length == 0:  # Escape sequence for payloads > 4095 bytes
                    if len(data) < 6:
                        return None, None
                    length, offset = int.from_bytes(data[2:6], 'big'), 6
                self._transfers[key] = _Transfer(length, bytearray(data[offset:]))
                return f'ISO-TP FF (total {length} B)', None
            case 2:
                sn = data[0] & 0x0F
                transfer = self._transfers.get(key)
                if transfer is None:
                    return f'ISO-TP CF sn={sn} (no transfer in progress)', None
                if sn != transfer.next_sn:
                    del self._transfers[key]
                    return f'ISO-TP CF sn={sn} (expected {transfer.next_sn}, transfer aborted)', None
                transfer.data.extend(data[1:])
                transfer.next_sn = (sn + 1) & 0x0F
                if len(transfer.data) >= transfer.length:
                    del self._transfers[key]
                    payload = bytes(transfer.data[: transfer.length])
                    return f'ISO-TP complete ({transfer.length} B): {format_hex(payload, self._preview)}', payload
                return f'ISO-TP CF sn={sn} ({len(transfer.data)}/{transfer.length} B)', None
            case 3:
                status = FLOW_STATUS.get(data[0] & 0x0F)
                if status is None or len(data) < 3:
                    return None, None
                return f'ISO-TP FC {status} BS={data[1]} STmin={data[2]:#04x}', None
        return None, None
