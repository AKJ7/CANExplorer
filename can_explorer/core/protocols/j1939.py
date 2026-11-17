from dataclasses import dataclass, field
from typing import Callable, Dict, NamedTuple, Optional, Tuple

import can

from can_explorer.core.frames import format_hex

PGN_ACKNOWLEDGMENT = 0xE800
PGN_REQUEST = 0xEA00
PGN_TP_CM = 0xEC00
PGN_TP_DT = 0xEB00
PGN_ETP_CM = 0xC800  # Extended transport protocol (messages over 1785 bytes), connection management
PGN_ETP_DT = 0xC700  # Extended transport protocol, data transfer
PGN_COMMANDED_ADDRESS = 0xFED8
GLOBAL_ADDRESS = 0xFF
# Identification parameter groups whose data is ASCII text (shown as text, fields not interpreted)
TEXT_PGNS = {0xFEDA: 'Software Identification', 0xFEEB: 'Component Identification', 0xFEEC: 'Vehicle Identification'}

PGN_NAMES = {
    PGN_ACKNOWLEDGMENT: 'Acknowledgment',
    PGN_REQUEST: 'Request',
    PGN_TP_DT: 'TP.DT',
    PGN_TP_CM: 'TP.CM',
    PGN_ETP_DT: 'ETP.DT',
    PGN_ETP_CM: 'ETP.CM',
    0xEE00: 'Address Claimed',
    0xF003: 'EEC2',
    0xF004: 'EEC1',
    0xFECA: 'DM1',
    PGN_COMMANDED_ADDRESS: 'Commanded Address',
    0xFEEC: 'Vehicle Identification',
    0xFEEE: 'ET1',
    0xFEF1: 'CCVS1',
}

# Control bytes of TP.CM and ETP.CM (J1939-21; values as in the Linux kernel's J1939 stack)
TP_RTS, TP_CTS, TP_EOMA, TP_BAM, TP_ABORT = 16, 17, 19, 32, 255
ETP_RTS, ETP_CTS, ETP_DPO, ETP_EOMA = 20, 21, 22, 23
TP_CONTROL = {TP_RTS: 'RTS', TP_CTS: 'CTS', TP_EOMA: 'EndOfMsgAck', TP_BAM: 'BAM', TP_ABORT: 'Abort'}
ETP_CONTROL = {ETP_RTS: 'RTS', ETP_CTS: 'CTS', ETP_DPO: 'DPO', ETP_EOMA: 'EndOfMsgAck', TP_ABORT: 'Abort'}
TP_PACKET_BYTES = 7
TP_MAX_SIZE = 7 * 255  # 1785 bytes
ETP_MAX_SIZE = 7 * 0xFFFFFF

# Connection abort reasons (J1939-21)
ABORT_REASONS = {
    1: 'already in a session',
    2: 'resources needed for another task',
    3: 'timeout',
    4: 'CTS while data transfer in progress',
    5: 'retransmit limit reached',
    6: 'unexpected data packet',
    7: 'bad sequence number',
    8: 'duplicate sequence number',
    9: 'unexpected DPO, or message larger than 1785 bytes',
    10: 'bad PGN in DPO',
    11: 'DPO packets exceed CTS',
    12: 'bad DPO offset',
    13: 'other (deprecated code)',
    14: 'bad PGN in CTS',
    15: 'CTS requests more packets than the message has',
    250: 'other',
}

# Control byte of the Acknowledgment PGN (J1939-21)
ACK_CONTROL = {0: 'ACK', 1: 'NACK', 2: 'Access denied', 3: 'Cannot respond'}


def abort_reason(code: int) -> str:
    return ABORT_REASONS.get(code, f'reason {code}')


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


def default_pgn_label(pgn: int) -> str:
    name = PGN_NAMES.get(pgn)
    return f'PGN 0x{pgn:05X}' + (f' {name}' if name else '')


# Transport protocol sessions, as seen on the bus


@dataclass(slots=True)
class _TpSession:
    pgn: int
    size: int
    packets: int
    extended: bool  # ETP
    data: bytearray = field(default_factory=bytearray)
    next_packet: int = 1  # Absolute number of the next expected packet (1-based)
    offset: int = 0  # ETP: data packet offset of the current block (from DPO)

    def __post_init__(self):
        self.data = bytearray(b'\xff' * (self.packets * TP_PACKET_BYTES))


Complete = Tuple[int, int, int, bytes]  # pgn, source, destination, payload


class TransportTracker:
    """Follows the transport protocol sessions on a bus: BAM, RTS/CTS (including retransmissions a receiver asks for
    with a CTS) and the extended transport protocol (ETP, messages over 1785 bytes, with DPO).

    `feed` returns None for frames that are not transport frames; for TP.CM/TP.DT/ETP.CM/ETP.DT it returns a text
    and, when a message is complete, (pgn, source, destination, payload).
    """

    def __init__(self, pgn_label: Callable[[int], str] = default_pgn_label):
        self._label = pgn_label
        self._sessions: Dict[Tuple[bool, int, int], _TpSession] = {}  # (etp, sender, receiver)

    def reset(self) -> None:
        self._sessions.clear()

    def feed(self, ident: J1939Id, data: bytes) -> Optional[Tuple[Optional[str], Optional[Complete]]]:
        pgn = ident.pgn
        if pgn == PGN_TP_CM:
            return self._control(ident, data, False), None
        if pgn == PGN_ETP_CM:
            return self._control(ident, data, True), None
        if pgn in (PGN_TP_DT, PGN_ETP_DT):
            return self._data(ident, data, pgn == PGN_ETP_DT)
        return None

    def _control(self, ident: J1939Id, data: bytes, etp: bool) -> Optional[str]:
        if len(data) < 8:
            return None
        sa, da = ident.source_address, ident.destination_address
        prefix = 'ETP' if etp else 'TP'
        control = data[0]
        name = (ETP_CONTROL if etp else TP_CONTROL).get(control, f'control {control}')
        pgn = int.from_bytes(data[5:8], 'little')
        label = self._label(pgn)
        if control == TP_ABORT:
            self._sessions.pop((etp, sa, da), None)
            self._sessions.pop((etp, da, sa), None)
            return f'{prefix}.Abort ({abort_reason(data[1])}) for {label}'
        if not etp:
            match control:
                case 16 | 32:  # RTS, BAM
                    size, packets = int.from_bytes(data[1:3], 'little'), data[3]
                    self._sessions[(False, sa, da)] = _TpSession(pgn, size, packets, False)
                    text = f'TP.{name} {size} B in {packets} packets for {label}'
                    if control == TP_RTS and data[4] != 0xFF:
                        text += f', at most {data[4]} per CTS'
                    return text
                case 17:  # CTS from the receiver: the sender continues (or repeats) from packet data[2]
                    session = self._sessions.get((False, da, sa))
                    if session is not None and data[1]:
                        session.next_packet = data[2]
                    if data[1] == 0:
                        return f'TP.CTS hold the connection open for {label}'
                    return f'TP.CTS {data[1]} packets from #{data[2]} for {label}'
                case 19:  # EndOfMsgAck
                    self._sessions.pop((False, da, sa), None)
                    size = int.from_bytes(data[1:3], 'little')
                    return f'TP.EndOfMsgAck {size} B in {data[3]} packets for {label}'
            return f'TP.{name} for {label}'
        match control:
            case 20:  # ETP RTS
                size = int.from_bytes(data[1:5], 'little')
                packets = -(-size // TP_PACKET_BYTES)
                self._sessions[(True, sa, da)] = _TpSession(pgn, size, packets, True)
                return f'ETP.RTS {size} B in {packets} packets for {label}'
            case 21:  # ETP CTS
                first = int.from_bytes(data[2:5], 'little')
                session = self._sessions.get((True, da, sa))
                if session is not None and data[1]:
                    session.next_packet = first
                if data[1] == 0:
                    return f'ETP.CTS hold the connection open for {label}'
                return f'ETP.CTS {data[1]} packets from #{first} for {label}'
            case 22:  # DPO: the next data packets are numbered from this offset
                offset = int.from_bytes(data[2:5], 'little')
                session = self._sessions.get((True, sa, da))
                if session is not None:
                    session.offset = offset
                    session.next_packet = offset + 1
                return f'ETP.DPO {data[1]} packets after offset {offset} for {label}'
            case 23:  # ETP EndOfMsgAck
                self._sessions.pop((True, da, sa), None)
                return f'ETP.EndOfMsgAck {int.from_bytes(data[1:5], "little")} B for {label}'
        return f'ETP.{name} for {label}'

    def _data(self, ident: J1939Id, data: bytes, etp: bool) -> Tuple[Optional[str], Optional[Complete]]:
        if not data:
            return None, None
        prefix = 'ETP' if etp else 'TP'
        sequence = data[0]
        key = (etp, ident.source_address, ident.destination_address)
        session = self._sessions.get(key)
        if session is None:
            return f'{prefix}.DT #{sequence} (no session)', None
        packet = session.offset + sequence if etp else sequence
        if packet != session.next_packet or not 1 <= packet <= session.packets:
            del self._sessions[key]
            expected = session.next_packet - session.offset if etp else session.next_packet
            return f'{prefix}.DT #{sequence} (expected #{expected}, session aborted)', None
        start = (packet - 1) * TP_PACKET_BYTES
        session.data[start : start + TP_PACKET_BYTES] = bytes(data[1:8]).ljust(TP_PACKET_BYTES, b'\xff')
        session.next_packet = packet + 1
        if packet < session.packets:
            return f'{prefix}.DT #{sequence}' + ('' if etp else f'/{session.packets}'), None
        del self._sessions[key]
        payload = bytes(session.data[: session.size])
        done = (session.pgn, ident.source_address, ident.destination_address, payload)
        return f'{prefix}.DT #{sequence}: complete {self._label(session.pgn)} ({session.size} B)', done


class J1939Decoder:
    """Passive SAE J1939 decoder: identifier fields, requests, acknowledgments and the transport protocols (BAM,
    RTS/CTS and the extended transport protocol for messages over 1785 bytes)."""

    name = 'J1939'

    def __init__(self, preview_bytes: int = 32):
        self._preview = preview_bytes
        self._transport = TransportTracker(self.pgn_label)

    def reset(self) -> None:
        self._transport.reset()

    @staticmethod
    def pgn_label(pgn: int) -> str:
        return default_pgn_label(pgn)

    def describe(self, msg: can.Message) -> Optional[str]:
        if not msg.is_extended_id:
            return None
        j1939_id = J1939Id.from_can_id(msg.arbitration_id)
        header = (
            f'{self.pgn_label(j1939_id.pgn)} SA=0x{j1939_id.source_address:02X} '
            f'DA=0x{j1939_id.destination_address:02X} P{j1939_id.priority}'
        )
        data = bytes(msg.data)
        transport = self._transport.feed(j1939_id, data)
        if transport is not None:
            detail, complete = transport
            if complete is not None:
                payload = complete[3]
                detail = f'{detail}: {format_hex(payload, self._preview)}'
                extra = self.describe_payload(complete[0], payload)
                if extra:
                    detail = f'{detail} — {extra}'
        elif j1939_id.pgn == PGN_REQUEST and len(data) >= 3:
            detail = f'requests {self.pgn_label(int.from_bytes(data[:3], "little"))}'
        else:
            detail = self.describe_payload(j1939_id.pgn, data)
        return f'{header}: {detail}' if detail else header

    def describe_payload(self, pgn: int, data: bytes) -> Optional[str]:
        """Address claims, acknowledgments, commanded addresses, diagnostic messages and the text of identification
        parameter groups, single frame or reassembled; None for other parameter groups."""
        from can_explorer.core.j1939_tools import PGN_ADDRESS_CLAIMED, PGN_DM1, PGN_DM2, J1939Name, decode_dm

        if pgn == PGN_ADDRESS_CLAIMED and len(data) >= 8:
            return f'NAME {J1939Name.from_bytes(data).describe()}'
        if pgn == PGN_ACKNOWLEDGMENT and len(data) >= 8:
            control = ACK_CONTROL.get(data[0], f'control {data[0]}')
            text = f'{control} for {self.pgn_label(int.from_bytes(data[5:8], "little"))}'
            if data[4] != GLOBAL_ADDRESS:
                text += f' requested by 0x{data[4]:02X}'
            return text + (f', group function {data[1]}' if data[1] != 0xFF else '')
        if pgn == PGN_COMMANDED_ADDRESS and len(data) >= 9:
            return f'NAME {J1939Name.from_bytes(data[:8]).describe()} → address 0x{data[8]:02X}'
        if pgn in (PGN_DM1, PGN_DM2):
            message = decode_dm(data)
            return message.describe() if message is not None else None
        if pgn in TEXT_PGNS and len(data) > 8:  # Transported identification: ASCII fields delimited by '*'
            return 'text "' + ''.join(chr(b) if 32 <= b < 127 else '.' for b in data) + '"'
        return None


# SAE J1939-22 (J1939-FD) PGNs (values from the open python-can-j1939 project)
PGN_MULTI_PG = 0x2500  # Multiplexed PG container: several parameter groups in one CAN FD frame
PGN_FD_TP_CM = 0x4D00  # FD transport protocol, connection management
PGN_FD_TP_DT = 0x4E00  # FD transport protocol, data transfer
J1939_22_PGN_NAMES = {PGN_MULTI_PG: 'Multi-PG', PGN_FD_TP_CM: 'FD.TP.CM', PGN_FD_TP_DT: 'FD.TP.DT'}


class J1939_22Decoder(J1939Decoder):
    """Passive SAE J1939-22 (J1939-FD) decoder: J1939 carried on CAN FD. Ordinary single-PG frames decode exactly
    like J1939 (on the longer FD payload); the FD transport protocol (FD.TP) and the multiplexed-PG (Multi-PG)
    frames are recognised and labelled.

    Expanding a Multi-PG frame into its contained PGs, and reassembling FD.TP messages, needs the C-PG / FD-TP byte
    layout of the SAE J1939-22 standard (which is not public); until that is confirmed those frames are recognised
    but their payload is shown as raw bytes rather than split into the individual parameter groups.
    """

    name = 'J1939-22'

    @staticmethod
    def pgn_label(pgn: int) -> str:
        name = J1939_22_PGN_NAMES.get(pgn) or PGN_NAMES.get(pgn)
        return f'PGN 0x{pgn:05X}' + (f' {name}' if name else '')

    def describe(self, msg: can.Message) -> Optional[str]:
        if not msg.is_extended_id:
            return None
        j1939_id = J1939Id.from_can_id(msg.arbitration_id)
        if j1939_id.pgn in (PGN_MULTI_PG, PGN_FD_TP_CM, PGN_FD_TP_DT):
            header = (
                f'{self.pgn_label(j1939_id.pgn)} SA=0x{j1939_id.source_address:02X} '
                f'DA=0x{j1939_id.destination_address:02X} P{j1939_id.priority}'
            )
            if j1939_id.pgn == PGN_MULTI_PG:
                detail = f'multiplexed PGs, {len(msg.data)} bytes (contained PGs not expanded)'
            else:
                detail = f'FD transport, {len(msg.data)} bytes (not reassembled)'
            return f'{header}: {detail}'
        return super().describe(msg)
