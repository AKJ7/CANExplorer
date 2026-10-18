"""SAE J1939 beyond the passive decoder: NAMEs and address claims, diagnostic messages (DM1/DM2), a model of the
network, and sending parameter groups, with the transport protocol for more than 8 bytes."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import can

from can_explorer.core.protocols.j1939 import (
    GLOBAL_ADDRESS,
    PGN_NAMES,
    PGN_REQUEST,
    PGN_TP_CM,
    PGN_TP_DT,
    J1939Id,
)

PGN_ADDRESS_CLAIMED = 0xEE00
PGN_DM1 = 0xFECA  # Active diagnostic trouble codes
PGN_DM2 = 0xFECB  # Previously active diagnostic trouble codes
PGN_DM3 = 0xFECC  # Clear previously active diagnostic trouble codes
PGN_DM11 = 0xFED3  # Clear active diagnostic trouble codes
NULL_ADDRESS = 0xFE  # Source address of "cannot claim address"
TOOL_ADDRESS = 0xF9  # Off-board diagnostic service tool
DEFAULT_PRIORITY = 6
TP_PACKET = 7  # Data bytes per transport packet
TP_MAX = 1785
BAM_INTERVAL = 0.05  # 50 to 200 ms between BAM packets
TP_TIMEOUT = 1.25  # T1..T3 of J1939-21

TOOL_PGN_NAMES = {
    **PGN_NAMES,
    PGN_DM2: 'DM2',
    PGN_DM3: 'DM3',
    PGN_DM11: 'DM11',
    0xFEDA: 'Software Identification',
    0xFEEB: 'Component Identification',
    0xFEE5: 'Engine Hours, Revolutions',
    0xFEEF: 'Engine Fluid Level/Pressure 1',
    0xFEF2: 'Fuel Economy',
    0xFEF6: 'Inlet/Exhaust Conditions 1',
    0xFEF7: 'Vehicle Electrical Power 1',
    0xFEFC: 'Dash Display',
    0xFE6C: 'Tachograph',
}

# J1939-81 function codes of industry group 0 (global); 128 and above depend on the industry group
FUNCTION_NAMES = {
    0: 'Engine',
    1: 'Auxiliary power unit',
    2: 'Electric propulsion control',
    3: 'Transmission',
    4: 'Battery pack monitor',
    5: 'Shift control/console',
    6: 'Power take-off (main or rear)',
    7: 'Axle - steering',
    8: 'Axle - drive',
    9: 'Brakes - system controller',
    10: 'Brakes - steer axle',
    11: 'Brakes - drive axle',
    12: 'Retarder - engine',
    13: 'Retarder - driveline',
    14: 'Cruise control',
    15: 'Fuel system',
    16: 'Steering controller',
    17: 'Suspension - steer axle',
    18: 'Suspension - drive axle',
    19: 'Instrument cluster',
    20: 'Trip recorder',
    21: 'Cab climate control',
    22: 'Aerodynamic control',
    23: 'Vehicle navigation',
    24: 'Vehicle security',
    25: 'Network interconnect ECU',
    26: 'Body controller',
    27: 'Power take-off (secondary or front)',
    28: 'Off vehicle gateway',
    29: 'Virtual terminal (in cab)',
    30: 'Management computer',
    31: 'Propulsion battery charger',
    32: 'Headway controller',
    33: 'System monitor',
    34: 'Hydraulic pump controller',
    35: 'Suspension - system controller',
    36: 'Pneumatic - system controller',
    37: 'Cab controller',
    38: 'Tire pressure control',
    39: 'Ignition control module',
    40: 'Seat control',
    41: 'Lighting - operator controls',
    42: 'Water pump control',
    43: 'Transmission display',
    44: 'Exhaust emission control',
    45: 'Vehicle dynamic stability control',
    46: 'Oil sensor unit',
    47: 'Information system controller',
    48: 'Ramp control',
    49: 'Clutch/converter control',
    50: 'Auxiliary heater',
    51: 'Forward-looking collision warning system',
    52: 'Chassis controller',
    53: 'Alternator/charging system',
    54: 'Communications unit, cellular',
    55: 'Communications unit, satellite',
    56: 'Communications unit, radio',
    57: 'Steering column unit',
    58: 'Fan drive control',
    59: 'Starter',
    60: 'Cab display',
    61: 'File server/printer',
    62: 'On-board diagnostic unit',
    63: 'Engine valve controller',
    64: 'Endurance braking',
    65: 'Gas flow measurement',
    66: 'I/O controller',
    67: 'Electrical system controller',
    125: 'Not available',
    126: 'Not available',
    127: 'Not available',
    129: 'Off-board diagnostic service tool',
}

INDUSTRY_GROUPS = {
    0: 'Global',
    1: 'On-highway',
    2: 'Agriculture and forestry',
    3: 'Construction',
    4: 'Marine',
    5: 'Industrial/process control',
}

# J1939-73 failure mode identifiers
FMI_TEXT = {
    0: 'Data valid but above normal operational range - most severe',
    1: 'Data valid but below normal operational range - most severe',
    2: 'Data erratic, intermittent or incorrect',
    3: 'Voltage above normal, or shorted to high source',
    4: 'Voltage below normal, or shorted to low source',
    5: 'Current below normal or open circuit',
    6: 'Current above normal or grounded circuit',
    7: 'Mechanical system not responding or out of adjustment',
    8: 'Abnormal frequency or pulse width or period',
    9: 'Abnormal update rate',
    10: 'Abnormal rate of change',
    11: 'Root cause not known',
    12: 'Bad intelligent device or component',
    13: 'Out of calibration',
    14: 'Special instructions',
    15: 'Data valid but above normal operating range - least severe',
    16: 'Data valid but above normal operating range - moderately severe',
    17: 'Data valid but below normal operating range - least severe',
    18: 'Data valid but below normal operating range - moderately severe',
    19: 'Received network data in error',
    20: 'Data drifted high',
    21: 'Data drifted low',
    31: 'Condition exists',
}

# Common suspect parameter numbers
SPN_NAMES = {
    27: 'Engine EGR valve position',
    51: 'Engine throttle valve position',
    84: 'Wheel-based vehicle speed',
    91: 'Accelerator pedal position 1',
    94: 'Engine fuel delivery pressure',
    96: 'Fuel level 1',
    97: 'Water in fuel indicator',
    98: 'Engine oil level',
    100: 'Engine oil pressure',
    102: 'Engine intake manifold 1 pressure',
    105: 'Engine intake manifold 1 temperature',
    108: 'Barometric pressure',
    110: 'Engine coolant temperature',
    111: 'Engine coolant level',
    157: 'Engine injector metering rail 1 pressure',
    158: 'Keyswitch battery potential',
    168: 'Battery potential / power input 1',
    171: 'Ambient air temperature',
    175: 'Engine oil temperature 1',
    190: 'Engine speed',
    247: 'Engine total hours of operation',
    629: 'Controller #1',
    636: 'Engine position sensor',
    639: 'J1939 network #1',
    651: 'Engine injector cylinder #1',
    723: 'Engine speed sensor #2',
    1569: 'Engine protection torque derate',
    3216: 'Aftertreatment 1 intake NOx',
    3226: 'Aftertreatment 1 outlet NOx',
    3251: 'Aftertreatment 1 DPF differential pressure',
    4364: 'Aftertreatment 1 SCR conversion efficiency',
}

LAMPS = ('MIL', 'RSL', 'AWL', 'PL')  # Malfunction indicator, red stop, amber warning, protect lamp
LAMP_STATES = {0: 'off', 1: 'on', 2: 'error', 3: 'not available'}


# Identifiers


def build_can_id(pgn: int, source: int, destination: int = GLOBAL_ADDRESS, priority: int = DEFAULT_PRIORITY) -> int:
    """The 29 bit identifier of a parameter group; PDU1 groups (PF < 240) carry the destination address."""
    if not 0 <= priority <= 7:
        raise ValueError('The priority is 0 to 7')
    if not 0 <= pgn <= 0x3FFFF:
        raise ValueError('PGNs are 0 to 0x3FFFF')
    pdu_format = (pgn >> 8) & 0xFF
    if pdu_format < 240:
        pgn = (pgn & 0x3FF00) | (destination & 0xFF)
    return (priority << 26) | (pgn << 8) | (source & 0xFF)


def is_pdu1(pgn: int) -> bool:
    return ((pgn >> 8) & 0xFF) < 240


def pgn_label(pgn: int) -> str:
    name = TOOL_PGN_NAMES.get(pgn)
    return f'0x{pgn:05X}' + (f' {name}' if name else '')


def parse_pgn(text: str, database_names: Optional[Dict[str, int]] = None) -> int:
    """A PGN from '0xFEF1', '65265', 'CCVS1' or a database message name."""
    text = text.strip()
    if not text:
        raise ValueError('Enter a PGN')
    for names in (database_names or {}, {name: pgn for pgn, name in TOOL_PGN_NAMES.items()}):
        for name, pgn in names.items():
            if name.lower() == text.lower():
                return pgn
    match = re.match(r'^(0x[0-9a-fA-F]+|\d+)', text)  # Also '0xFEF1 CCVS1' as listed
    if match is None:
        raise ValueError(f'Unknown PGN {text!r}')
    pgn = int(match.group(1), 0)
    if not 0 <= pgn <= 0x3FFFF:
        raise ValueError('PGNs are 0 to 0x3FFFF')
    return pgn


# NAME (J1939-81)


@dataclass(frozen=True)
class J1939Name:
    """The 64 bit NAME a node claims its address with."""

    value: int

    @property
    def identity_number(self) -> int:
        return self.value & 0x1FFFFF

    @property
    def manufacturer_code(self) -> int:
        return (self.value >> 21) & 0x7FF

    @property
    def ecu_instance(self) -> int:
        return (self.value >> 32) & 0x7

    @property
    def function_instance(self) -> int:
        return (self.value >> 35) & 0x1F

    @property
    def function(self) -> int:
        return (self.value >> 40) & 0xFF

    @property
    def vehicle_system(self) -> int:
        return (self.value >> 49) & 0x7F

    @property
    def vehicle_system_instance(self) -> int:
        return (self.value >> 56) & 0xF

    @property
    def industry_group(self) -> int:
        return (self.value >> 60) & 0x7

    @property
    def arbitrary_address_capable(self) -> bool:
        return bool(self.value >> 63)

    @property
    def function_name(self) -> str:
        if self.function < 128 or self.function == 129:
            return FUNCTION_NAMES.get(self.function, f'Function {self.function}')
        return f'Function {self.function} ({INDUSTRY_GROUPS.get(self.industry_group, "industry group")})'

    @classmethod
    def from_bytes(cls, data: bytes) -> 'J1939Name':
        return cls(int.from_bytes(bytes(data[:8]).ljust(8, b'\x00'), 'little'))

    def to_bytes(self) -> bytes:
        return self.value.to_bytes(8, 'little')

    def describe(self) -> str:
        return (
            f'{self.function_name}, manufacturer {self.manufacturer_code}, identity {self.identity_number}, '
            f'ECU instance {self.ecu_instance}, {INDUSTRY_GROUPS.get(self.industry_group, "industry group " + str(self.industry_group))}'
            + (', arbitrary address capable' if self.arbitrary_address_capable else '')
        )


# Diagnostic messages (J1939-73)


@dataclass(frozen=True)
class Dtc:
    spn: int
    fmi: int
    occurrences: int
    conversion_method: int = 0

    @property
    def spn_name(self) -> str:
        return SPN_NAMES.get(self.spn, '')

    @property
    def fmi_text(self) -> str:
        return FMI_TEXT.get(self.fmi, f'FMI {self.fmi}')

    def describe(self) -> str:
        name = f' {self.spn_name}' if self.spn_name else ''
        return f'SPN {self.spn}{name}, FMI {self.fmi} ({self.fmi_text}), {self.occurrences}×'


@dataclass(frozen=True)
class DiagnosticMessage:
    """DM1 or DM2: lamp states and trouble codes."""

    lamps: Dict[str, str]
    dtcs: Tuple[Dtc, ...]

    def describe(self) -> str:
        lamps = [f'{lamp} {state}' for lamp, state in self.lamps.items() if state in ('on', 'error')]
        codes = '; '.join(dtc.describe() for dtc in self.dtcs) if self.dtcs else 'no trouble codes'
        return (', '.join(lamps) + ': ' if lamps else '') + codes


def decode_dm(data: bytes) -> Optional[DiagnosticMessage]:
    """Decode a DM1 or DM2 payload: two lamp bytes, then four bytes per trouble code."""
    data = bytes(data)
    if len(data) < 6:
        return None
    lamps = {lamp: LAMP_STATES[(data[0] >> shift) & 0x3] for lamp, shift in zip(LAMPS, (6, 4, 2, 0), strict=True)}
    dtcs = []
    for offset in range(2, len(data) - 3, 4):
        chunk = data[offset : offset + 4]
        spn = chunk[0] | (chunk[1] << 8) | ((chunk[2] >> 5) << 16)
        fmi = chunk[2] & 0x1F
        if spn == 0 and fmi == 0:  # "No active DTC" placeholder
            continue
        if chunk == b'\xff\xff\xff\xff':
            continue  # Padding
        dtcs.append(Dtc(spn, fmi, chunk[3] & 0x7F, chunk[3] >> 7))
    return DiagnosticMessage(lamps, tuple(dtcs))


def encode_dm(dtcs: List[Dtc], lamps: Optional[Dict[str, str]] = None) -> bytes:
    """A DM1/DM2 payload (padded with 0xFF to 8 bytes)."""
    states = {state: value for value, state in LAMP_STATES.items()}
    lamp_byte = 0
    for lamp, shift in zip(LAMPS, (6, 4, 2, 0), strict=True):
        lamp_byte |= states[(lamps or {}).get(lamp, 'off')] << shift
    payload = bytearray([lamp_byte, 0xFF])
    for dtc in dtcs or [Dtc(0, 0, 0)]:
        payload += bytes(
            [
                dtc.spn & 0xFF,
                (dtc.spn >> 8) & 0xFF,
                ((dtc.spn >> 16) << 5) | dtc.fmi,
                (dtc.conversion_method << 7) | dtc.occurrences,
            ]
        )
    return bytes(payload.ljust(8, b'\xff'))


# Passive transport reassembly


@dataclass
class _Transfer:
    pgn: int
    size: int
    packets: int
    data: bytearray = field(default_factory=bytearray)
    next_sequence: int = 1


class TransportReassembler:
    """Reassembles BAM and RTS/CTS transfers seen on the bus; `on_message` returns (pgn, source, destination,
    payload) for complete parameter groups, single frame or transported."""

    def __init__(self):
        self._transfers: Dict[Tuple[int, int], _Transfer] = {}

    def on_message(self, msg: can.Message) -> Optional[Tuple[int, int, int, bytes]]:
        if not msg.is_extended_id or msg.is_error_frame or msg.is_remote_frame:
            return None
        ident = J1939Id.from_can_id(msg.arbitration_id)
        data = bytes(msg.data)
        key = (ident.source_address, ident.destination_address)
        if ident.pgn == PGN_TP_CM and len(data) >= 8:
            if data[0] in (16, 32):  # RTS, BAM
                self._transfers[key] = _Transfer(
                    int.from_bytes(data[5:8], 'little'), int.from_bytes(data[1:3], 'little'), data[3]
                )
            elif data[0] == 255:
                self._transfers.pop(key, None)
                self._transfers.pop((ident.destination_address, ident.source_address), None)
            return None
        if ident.pgn == PGN_TP_DT and data:
            transfer = self._transfers.get(key)
            if transfer is None or data[0] != transfer.next_sequence:
                self._transfers.pop(key, None)
                return None
            transfer.data += data[1:8]
            transfer.next_sequence += 1
            if data[0] >= transfer.packets:
                del self._transfers[key]
                return (
                    transfer.pgn,
                    ident.source_address,
                    ident.destination_address,
                    bytes(transfer.data[: transfer.size]),
                )
            return None
        return ident.pgn, ident.source_address, ident.destination_address, data


# The network


@dataclass
class J1939Node:
    address: int
    name: Optional[J1939Name] = None
    claimed: Optional[float] = None  # Time of the last address claim
    frames: int = 0
    pgns: Dict[int, int] = field(default_factory=dict)  # PGN -> frames
    last_seen: float = 0.0
    dm1: Optional[DiagnosticMessage] = None
    dm2: Optional[DiagnosticMessage] = None


class J1939Network:
    """The nodes of a J1939 network as seen on the bus: address claims, traffic and diagnostic messages."""

    def __init__(self):
        self.nodes: Dict[int, J1939Node] = {}
        self.cannot_claim: List[J1939Name] = []
        self._reassembler = TransportReassembler()
        self.changed = False

    def node(self, address: int) -> J1939Node:
        if address not in self.nodes:
            self.nodes[address] = J1939Node(address)
        return self.nodes[address]

    def on_message(self, msg: can.Message) -> None:
        result = self._reassembler.on_message(msg)
        if msg.is_extended_id and not msg.is_error_frame:
            ident = J1939Id.from_can_id(msg.arbitration_id)
            if ident.source_address != NULL_ADDRESS:
                node = self.node(ident.source_address)
                node.frames += 1
                node.last_seen = msg.timestamp
                self.changed = True
        if result is None:
            return
        pgn, source, _, payload = result
        if source == NULL_ADDRESS:
            if pgn == PGN_ADDRESS_CLAIMED and len(payload) >= 8:
                self.cannot_claim.append(J1939Name.from_bytes(payload))
            return
        node = self.node(source)
        node.pgns[pgn] = node.pgns.get(pgn, 0) + 1
        if pgn == PGN_ADDRESS_CLAIMED and len(payload) >= 8:
            node.name = J1939Name.from_bytes(payload)
            node.claimed = msg.timestamp
        elif pgn == PGN_DM1:
            node.dm1 = decode_dm(payload)
        elif pgn == PGN_DM2:
            node.dm2 = decode_dm(payload)
        self.changed = True

    def clear(self) -> None:
        self.nodes.clear()
        self.cannot_claim.clear()
        self._reassembler = TransportReassembler()
        self.changed = True


# Sending


class J1939Sender:
    """Sends parameter groups on a session: single frames, BAM to all nodes, RTS/CTS to one node."""

    def __init__(self, session, log: Optional[Callable[[str], None]] = None):
        self.session = session
        self._log = log or (lambda text: None)
        self._cts: Optional[asyncio.Queue] = None
        self._address = TOOL_ADDRESS

    def _send(self, can_id: int, data: bytes) -> None:
        self.session.send(can.Message(arbitration_id=can_id, is_extended_id=True, data=data))

    def request(self, pgn: int, destination: int = GLOBAL_ADDRESS, source: int = TOOL_ADDRESS) -> None:
        """Request a parameter group (PGN 59904), e.g. address claims or DM2."""
        self._send(build_can_id(PGN_REQUEST, source, destination, priority=6), pgn.to_bytes(3, 'little'))
        self._log(
            f'Requested {pgn_label(pgn)} from {"all nodes" if destination == GLOBAL_ADDRESS else f"0x{destination:02X}"}'
        )

    async def send(
        self,
        pgn: int,
        data: bytes,
        source: int = TOOL_ADDRESS,
        destination: int = GLOBAL_ADDRESS,
        priority: int = DEFAULT_PRIORITY,
    ) -> None:
        """Send a parameter group; more than 8 bytes go with the transport protocol."""
        data = bytes(data)
        if len(data) > TP_MAX:
            raise ValueError(f'At most {TP_MAX} bytes')
        if not is_pdu1(pgn):
            destination = GLOBAL_ADDRESS  # PDU2 groups are broadcast
        if len(data) <= 8:
            self._send(build_can_id(pgn, source, destination, priority), data)
            self._log(f'Sent {pgn_label(pgn)} from 0x{source:02X}: {data.hex(" ").upper()}')
            return
        packets = [data[i : i + TP_PACKET].ljust(TP_PACKET, b'\xff') for i in range(0, len(data), TP_PACKET)]
        header = len(data).to_bytes(2, 'little') + bytes([len(packets)])
        pgn_bytes = pgn.to_bytes(3, 'little')
        if destination == GLOBAL_ADDRESS:
            self._send(build_can_id(PGN_TP_CM, source, GLOBAL_ADDRESS, 7), bytes([32]) + header + b'\xff' + pgn_bytes)
            for number, packet in enumerate(packets, start=1):
                await asyncio.sleep(BAM_INTERVAL)
                self._send(build_can_id(PGN_TP_DT, source, GLOBAL_ADDRESS, 7), bytes([number]) + packet)
            self._log(f'Sent {pgn_label(pgn)} ({len(data)} B) with BAM')
            return
        await self._send_rts_cts(pgn, data, packets, header, pgn_bytes, source, destination)

    async def _send_rts_cts(self, pgn, data, packets, header, pgn_bytes, source, destination) -> None:
        self._cts = asyncio.Queue()
        self._address = source
        listener = self._on_message
        self.session.add_message_listener(listener)
        try:
            self._send(build_can_id(PGN_TP_CM, source, destination, 7), bytes([16]) + header + b'\xff' + pgn_bytes)
            while True:
                try:
                    control = await asyncio.wait_for(self._cts.get(), TP_TIMEOUT)
                except asyncio.TimeoutError:
                    raise TimeoutError(f'0x{destination:02X} did not answer the transport request') from None
                if control[0] == 255:
                    raise ConnectionError(f'0x{destination:02X} aborted the transfer (reason {control[1]})')
                if control[0] == 19:  # EndOfMsgAck
                    self._log(f'Sent {pgn_label(pgn)} ({len(data)} B) to 0x{destination:02X} with RTS/CTS')
                    return
                if control[0] == 17:  # CTS: number of packets, next packet number
                    count, first = control[1], control[2]
                    for number in range(first, min(first + count, len(packets) + 1)):  # Count 0: wait
                        self._send(
                            build_can_id(PGN_TP_DT, source, destination, 7), bytes([number]) + packets[number - 1]
                        )
        finally:
            self.session.remove_message_listener(listener)
            self._cts = None

    def _on_message(self, msg: can.Message) -> None:
        if self._cts is None or not msg.is_extended_id:
            return
        ident = J1939Id.from_can_id(msg.arbitration_id)
        if ident.pgn == PGN_TP_CM and ident.destination_address == self._address and len(msg.data) >= 8:
            self._cts.put_nowait(bytes(msg.data))
