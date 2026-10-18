from dataclasses import dataclass, field
from typing import Dict, NamedTuple, Optional, Tuple

import can

from can_explorer.core.frames import format_hex

PGN_REQUEST = 0xEA00
PGN_TP_CM = 0xEC00
PGN_TP_DT = 0xEB00
GLOBAL_ADDRESS = 0xFF

PGN_NAMES = {
    0xE800: 'Acknowledgment',
    PGN_REQUEST: 'Request',
    PGN_TP_DT: 'TP.DT',
    PGN_TP_CM: 'TP.CM',
    0xEE00: 'Address Claimed',
    0xF003: 'EEC2',
    0xF004: 'EEC1',
    0xFECA: 'DM1',
    0xFEEC: 'Vehicle Identification',
    0xFEEE: 'ET1',
    0xFEF1: 'CCVS1',
}

TP_CONTROL = {16: 'RTS', 17: 'CTS', 19: 'EndOfMsgAck', 32: 'BAM', 255: 'Abort'}


class J1939Id(NamedTuple):
    priority: int
    pgn: int
    source_address: int
    destination_address: int

    @classmethod
    def from_can_id(cls, can_id: int) -> 'J1939Id':
        priority = (can_id >> 26) & 0x7
        pgn = (can_id >> 8) & 0x3FFFF
        pdu_format = (pgn >> 8) & 0xFF
        if pdu_format < 240:  # PDU1: the PS field is the destination address
            destination = pgn & 0xFF
            pgn &= 0x3FF00
        else:  # PDU2: broadcast, the PS field is the group extension
            destination = GLOBAL_ADDRESS
        return cls(priority, pgn, can_id & 0xFF, destination)


@dataclass(slots=True)
class _TpSession:
    size: int
    packets: int
    pgn: int
    data: bytearray = field(default_factory=bytearray)
    next_sequence: int = 1


class J1939Decoder:
    """Passive SAE J1939 decoder: identifier fields, requests and transport protocol (BAM and RTS/CTS)."""

    name = 'J1939'

    def __init__(self, preview_bytes: int = 32):
        self._preview = preview_bytes
        self._sessions: Dict[Tuple[int, int], _TpSession] = {}

    def reset(self) -> None:
        self._sessions.clear()

    @staticmethod
    def pgn_label(pgn: int) -> str:
        name = PGN_NAMES.get(pgn)
        return f'PGN 0x{pgn:05X}' + (f' {name}' if name else '')

    def describe(self, msg: can.Message) -> Optional[str]:
        if not msg.is_extended_id:
            return None
        j1939_id = J1939Id.from_can_id(msg.arbitration_id)
        header = (
            f'{self.pgn_label(j1939_id.pgn)} SA=0x{j1939_id.source_address:02X} '
            f'DA=0x{j1939_id.destination_address:02X} P{j1939_id.priority}'
        )
        data = msg.data
        detail = None
        if j1939_id.pgn == PGN_TP_CM:
            detail = self._transport_control(j1939_id, data)
        elif j1939_id.pgn == PGN_TP_DT:
            detail = self._transport_data(j1939_id, data)
        elif j1939_id.pgn == PGN_REQUEST and len(data) >= 3:
            detail = f'requests {self.pgn_label(int.from_bytes(data[:3], "little"))}'
        else:
            detail = self._payload(j1939_id.pgn, bytes(data))
        return f'{header}: {detail}' if detail else header

    @staticmethod
    def _payload(pgn: int, data: bytes) -> Optional[str]:
        """Address claims and diagnostic messages, single frame or reassembled."""
        from can_explorer.core.j1939_tools import PGN_ADDRESS_CLAIMED, PGN_DM1, PGN_DM2, J1939Name, decode_dm

        if pgn == PGN_ADDRESS_CLAIMED and len(data) >= 8:
            return f'NAME {J1939Name.from_bytes(data).describe()}'
        if pgn in (PGN_DM1, PGN_DM2):
            message = decode_dm(data)
            return message.describe() if message is not None else None
        return None

    def _transport_control(self, j1939_id: J1939Id, data: bytearray) -> Optional[str]:
        if len(data) < 8:
            return None
        control = TP_CONTROL.get(data[0], f'control {data[0]}')
        key = (j1939_id.source_address, j1939_id.destination_address)
        pgn = int.from_bytes(data[5:8], 'little')
        match data[0]:
            case 16 | 32:
                size, packets = int.from_bytes(data[1:3], 'little'), data[3]
                self._sessions[key] = _TpSession(size, packets, pgn)
                return f'TP.{control} {size} B in {packets} packets for {self.pgn_label(pgn)}'
            case 17:
                return f'TP.CTS {data[1]} packets from #{data[2]} for {self.pgn_label(pgn)}'
            case 255:
                self._sessions.pop((j1939_id.destination_address, j1939_id.source_address), None)
                self._sessions.pop(key, None)
                return f'TP.Abort reason {data[1]} for {self.pgn_label(pgn)}'
        return f'TP.{control} for {self.pgn_label(pgn)}'

    def _transport_data(self, j1939_id: J1939Id, data: bytearray) -> Optional[str]:
        if not data:
            return None
        sequence = data[0]
        key = (j1939_id.source_address, j1939_id.destination_address)
        session = self._sessions.get(key)
        if session is None:
            return f'TP.DT #{sequence} (no session)'
        if sequence != session.next_sequence:
            del self._sessions[key]
            return f'TP.DT #{sequence} (expected #{session.next_sequence}, session aborted)'
        session.data.extend(data[1:8])
        session.next_sequence += 1
        if sequence >= session.packets:
            del self._sessions[key]
            payload = session.data[: session.size]
            text = (
                f'TP.DT #{sequence}: complete {self.pgn_label(session.pgn)} '
                f'({session.size} B): {format_hex(payload, self._preview)}'
            )
            detail = self._payload(session.pgn, bytes(payload))
            return f'{text} — {detail}' if detail else text
        return f'TP.DT #{sequence}/{session.packets}'
