"""SAE J1939 beyond the passive decoder: NAMEs and address claims, diagnostic messages (DM1/DM2), a model of the
network, and sending parameter groups, with the transport protocol for more than 8 bytes."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import can

from can_explorer.core.protocols.j1939 import (
    ACK_CONTROL,
    ETP_CTS,
    ETP_DPO,
    ETP_EOMA,
    ETP_MAX_SIZE,
    ETP_RTS,
    GLOBAL_ADDRESS,
    PGN_ACKNOWLEDGMENT,
    PGN_COMMANDED_ADDRESS,
    PGN_ETP_CM,
    PGN_ETP_DT,
    PGN_NAMES,
    PGN_REQUEST,
    PGN_TP_CM,
    PGN_TP_DT,
    TP_ABORT,
    TP_BAM,
    TP_CTS,
    TP_EOMA,
    TP_RTS,
    J1939Id,
    TransportTracker,
    abort_reason,
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


class TransportReassembler:
    """Reassembles BAM, RTS/CTS and ETP transfers seen on the bus; `on_message` returns (pgn, source, destination,
    payload) for complete parameter groups, single frame or transported."""

    def __init__(self):
        self._tracker = TransportTracker()

    def on_message(self, msg: can.Message) -> Optional[Tuple[int, int, int, bytes]]:
        if not msg.is_extended_id or msg.is_error_frame or msg.is_remote_frame:
            return None
        ident = J1939Id.from_can_id(msg.arbitration_id)
        data = bytes(msg.data)
        transport = self._tracker.feed(ident, data)
        if transport is not None:
            return transport[1]
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


def _control_frame(control: int, body: bytes, pgn: int) -> bytes:
    """A TP.CM/ETP.CM frame: control byte, four bytes, the PGN of the transported message."""
    return bytes([control]) + body.ljust(4, b'\xff') + pgn.to_bytes(3, 'little')


class J1939Sender:
    """Sends parameter groups on a session: single frames, BAM to all nodes, RTS/CTS to one node, and the extended
    transport protocol (ETP) to one node for more than 1785 bytes."""

    def __init__(self, session, log: Optional[Callable[[str], None]] = None):
        self.session = session
        self._log = log or (lambda text: None)
        self._controls: Optional[asyncio.Queue] = None
        self._address = TOOL_ADDRESS
        self._peer = GLOBAL_ADDRESS
        self._control_pgn = PGN_TP_CM

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
        """Send a parameter group; more than 8 bytes go with the transport protocol: BAM to all nodes (at most 1785
        bytes), RTS/CTS to one node, ETP to one node for more than 1785 bytes."""
        data = bytes(data)
        if not is_pdu1(pgn):
            destination = GLOBAL_ADDRESS  # PDU2 groups are broadcast
        if len(data) > ETP_MAX_SIZE:
            raise ValueError(f'At most {ETP_MAX_SIZE} bytes')
        if len(data) > TP_MAX and destination == GLOBAL_ADDRESS:
            raise ValueError(
                f'At most {TP_MAX} bytes to all nodes (BAM); the extended transport protocol needs a destination'
            )
        if len(data) <= 8:
            self._send(build_can_id(pgn, source, destination, priority), data)
            self._log(f'Sent {pgn_label(pgn)} from 0x{source:02X}: {data.hex(" ").upper()}')
            return
        packets = [data[i : i + TP_PACKET].ljust(TP_PACKET, b'\xff') for i in range(0, len(data), TP_PACKET)]
        if destination == GLOBAL_ADDRESS:
            header = len(data).to_bytes(2, 'little') + bytes([len(packets)])
            self._send(build_can_id(PGN_TP_CM, source, GLOBAL_ADDRESS, 7), _control_frame(TP_BAM, header, pgn))
            for number, packet in enumerate(packets, start=1):
                await asyncio.sleep(BAM_INTERVAL)
                self._send(build_can_id(PGN_TP_DT, source, GLOBAL_ADDRESS, 7), bytes([number]) + packet)
            self._log(f'Sent {pgn_label(pgn)} ({len(data)} B) with BAM')
            return
        await self._send_connection(pgn, data, packets, source, destination, etp=len(data) > TP_MAX)

    async def _send_connection(self, pgn, data, packets, source, destination, etp: bool) -> None:
        """RTS/CTS (or ETP): the receiver asks for blocks of packets with CTS frames and confirms the end."""
        control_pgn, data_pgn = (PGN_ETP_CM, PGN_ETP_DT) if etp else (PGN_TP_CM, PGN_TP_DT)
        self._controls = asyncio.Queue()
        self._address, self._peer, self._control_pgn = source, destination, control_pgn
        listener = self._on_message
        self.session.add_message_listener(listener)
        control_id = build_can_id(control_pgn, source, destination, 7)
        data_id = build_can_id(data_pgn, source, destination, 7)
        try:
            if etp:
                self._send(control_id, _control_frame(ETP_RTS, len(data).to_bytes(4, 'little'), pgn))
            else:
                self._send(
                    control_id, _control_frame(TP_RTS, len(data).to_bytes(2, 'little') + bytes([len(packets)]), pgn)
                )
            while True:
                try:
                    control = await asyncio.wait_for(self._controls.get(), TP_TIMEOUT)
                except asyncio.TimeoutError:
                    raise TimeoutError(f'0x{destination:02X} did not answer the transport request') from None
                if control[0] == TP_ABORT:
                    raise ConnectionError(f'0x{destination:02X} aborted the transfer ({abort_reason(control[1])})')
                if control[0] in (TP_EOMA, ETP_EOMA):
                    protocol = 'ETP' if etp else 'RTS/CTS'
                    self._log(f'Sent {pgn_label(pgn)} ({len(data)} B) to 0x{destination:02X} with {protocol}')
                    return
                if control[0] not in (TP_CTS, ETP_CTS):
                    continue
                count = control[1]  # 0: hold the connection open, wait for the next CTS
                first = int.from_bytes(control[2:5], 'little') if etp else control[2]
                if not count:
                    continue
                if not 1 <= first <= len(packets):
                    self._send(control_id, _control_frame(TP_ABORT, bytes([15]), pgn))
                    raise ConnectionError(f'0x{destination:02X} asked for packet #{first} of {len(packets)}')
                last = min(first + count - 1, len(packets))
                if etp:  # The data packets of the block are numbered 1.. after this offset
                    self._send(
                        control_id,
                        _control_frame(ETP_DPO, bytes([last - first + 1]) + (first - 1).to_bytes(3, 'little'), pgn),
                    )
                for number in range(first, last + 1):
                    sequence = number - first + 1 if etp else number
                    self._send(data_id, bytes([sequence]) + packets[number - 1])
                    await asyncio.sleep(0)  # Let the bus and the receiver keep up with long blocks
        finally:
            self.session.remove_message_listener(listener)
            self._controls = None

    def _on_message(self, msg: can.Message) -> None:
        if self._controls is None or not msg.is_extended_id or not msg.is_rx:
            return
        ident = J1939Id.from_can_id(msg.arbitration_id)
        if (
            ident.pgn == self._control_pgn
            and ident.destination_address == self._address
            and ident.source_address == self._peer
            and len(msg.data) >= 8
            and msg.data[0] not in (TP_RTS, ETP_RTS, TP_BAM)
        ):
            self._controls.put_nowait(bytes(msg.data))


# Receiving transported messages addressed to the tool


@dataclass
class _Incoming:
    pgn: int
    size: int
    packets: int
    max_per_cts: int
    etp: bool
    next_packet: int = 1
    block_end: int = 0  # Last packet of the block asked for with the last CTS
    offset: int = 0  # ETP: data packet offset (DPO)
    retries: int = 0
    timer: Optional[asyncio.TimerHandle] = None


class TransportResponder:
    """Answers the transport sessions other nodes open to `address` (RTS/CTS and ETP): clear to send, the end of
    message acknowledgment, and aborts on timeouts. Without it, a node that answers a request from the tool with
    more than 8 bytes gives up after T3, as nobody sends it CTS. The data itself is reassembled passively (by a
    TransportReassembler) by whoever wants it.

    One responder per session and address: `attach` shares it between users; `close` the returned handle."""

    _shared: Dict[Tuple[int, int], 'TransportResponder'] = {}

    def __init__(self, session, address: int, block: int = 16):
        self.session, self.address, self.block = session, address, block
        self._incoming: Dict[Tuple[bool, int], _Incoming] = {}
        self._users = 0
        session.add_message_listener(self._on_message)

    @classmethod
    def attach(cls, session, address: int) -> 'TransportResponder':
        key = (id(session), address)
        responder = cls._shared.get(key)
        if responder is None or responder.session is not session:
            responder = cls._shared[key] = cls(session, address)
        responder._users += 1
        return responder

    def close(self) -> None:
        self._users -= 1
        if self._users > 0:
            return
        self.session.remove_message_listener(self._on_message)
        for incoming in self._incoming.values():
            if incoming.timer is not None:
                incoming.timer.cancel()
        self._incoming.clear()
        key = (id(self.session), self.address)
        if self._shared.get(key) is self:
            del self._shared[key]

    def _send(self, peer: int, etp: bool, data: bytes) -> None:
        pgn = PGN_ETP_CM if etp else PGN_TP_CM
        try:
            self.session.send(
                can.Message(arbitration_id=build_can_id(pgn, self.address, peer, 7), is_extended_id=True, data=data)
            )
        except Exception:  # noqa: BLE001 - a closed bus; the sender times out on its own
            pass

    def _arm(self, peer: int, incoming: _Incoming) -> None:
        """T2: the sender must send the asked-for packets within 1.25 s, or the session is aborted."""
        if incoming.timer is not None:
            incoming.timer.cancel()
            incoming.timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        incoming.timer = loop.call_later(TP_TIMEOUT, self._timeout, peer, incoming)

    def _timeout(self, peer: int, incoming: _Incoming) -> None:
        if self._incoming.get((incoming.etp, peer)) is incoming:
            del self._incoming[(incoming.etp, peer)]
            self._send(peer, incoming.etp, _control_frame(TP_ABORT, bytes([3]), incoming.pgn))

    def _clear_to_send(self, peer: int, incoming: _Incoming) -> None:
        count = min(incoming.packets - incoming.next_packet + 1, incoming.max_per_cts, self.block)
        incoming.block_end = incoming.next_packet + count - 1
        if incoming.etp:
            body = bytes([count]) + incoming.next_packet.to_bytes(3, 'little')
            self._send(peer, True, _control_frame(ETP_CTS, body, incoming.pgn))
        else:
            self._send(peer, False, _control_frame(TP_CTS, bytes([count, incoming.next_packet]), incoming.pgn))
        self._arm(peer, incoming)

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or not msg.is_extended_id or msg.is_error_frame or msg.is_remote_frame:
            return
        ident = J1939Id.from_can_id(msg.arbitration_id)
        if ident.destination_address != self.address or ident.pgn not in (PGN_TP_CM, PGN_TP_DT, PGN_ETP_CM, PGN_ETP_DT):
            return
        data, peer = bytes(msg.data), ident.source_address
        etp = ident.pgn in (PGN_ETP_CM, PGN_ETP_DT)
        if ident.pgn in (PGN_TP_CM, PGN_ETP_CM):
            if len(data) < 8:
                return
            pgn = int.from_bytes(data[5:8], 'little')
            if data[0] == TP_RTS and not etp:
                size, packets = int.from_bytes(data[1:3], 'little'), data[3]
                if size > TP_MAX or packets != -(-size // TP_PACKET):
                    self._send(peer, False, _control_frame(TP_ABORT, bytes([9 if size > TP_MAX else 250]), pgn))
                    return
                self._start(peer, _Incoming(pgn, size, packets, data[4] or 255, False))
            elif data[0] == ETP_RTS and etp:
                size = int.from_bytes(data[1:5], 'little')
                if not TP_MAX < size <= ETP_MAX_SIZE:
                    self._send(peer, True, _control_frame(TP_ABORT, bytes([250]), pgn))
                    return
                self._start(peer, _Incoming(pgn, size, -(-size // TP_PACKET), 255, True))
            elif data[0] == ETP_DPO and etp:
                incoming = self._incoming.get((True, peer))
                if incoming is not None:
                    incoming.offset = int.from_bytes(data[2:5], 'little')
                    if incoming.offset + 1 != incoming.next_packet:
                        self._abort(peer, incoming, 12)
            elif data[0] == TP_ABORT:
                incoming = self._incoming.pop((etp, peer), None)
                if incoming is not None and incoming.timer is not None:
                    incoming.timer.cancel()
            return
        incoming = self._incoming.get((etp, peer))
        if incoming is None or not data:
            return
        packet = incoming.offset + data[0] if etp else data[0]
        if packet < incoming.next_packet:
            return  # A packet repeated before our CTS took effect
        if packet != incoming.next_packet:
            incoming.retries += 1
            if incoming.retries > 2:
                self._abort(peer, incoming, 5)
            else:
                self._clear_to_send(peer, incoming)  # Ask again from the missing packet
            return
        incoming.next_packet += 1
        if incoming.next_packet > incoming.packets:
            del self._incoming[(etp, peer)]
            if incoming.timer is not None:
                incoming.timer.cancel()
            if etp:
                self._send(peer, True, _control_frame(ETP_EOMA, incoming.size.to_bytes(4, 'little'), incoming.pgn))
            else:
                body = incoming.size.to_bytes(2, 'little') + bytes([incoming.packets])
                self._send(peer, False, _control_frame(TP_EOMA, body, incoming.pgn))
        elif packet >= incoming.block_end:
            self._clear_to_send(peer, incoming)
        else:
            self._arm(peer, incoming)

    def _start(self, peer: int, incoming: _Incoming) -> None:
        previous = self._incoming.get((incoming.etp, peer))
        if previous is not None and previous.timer is not None:
            previous.timer.cancel()  # A new RTS from the same node replaces its earlier session
        self._incoming[(incoming.etp, peer)] = incoming
        self._clear_to_send(peer, incoming)

    def _abort(self, peer: int, incoming: _Incoming, reason: int) -> None:
        self._incoming.pop((incoming.etp, peer), None)
        if incoming.timer is not None:
            incoming.timer.cancel()
        self._send(peer, incoming.etp, _control_frame(TP_ABORT, bytes([reason]), incoming.pgn))


# Requesting a parameter group and waiting for the answers


@dataclass(frozen=True)
class RequestAnswer:
    source: int
    payload: Optional[bytes]  # None for an acknowledgment instead of data
    acknowledgment: Optional[str] = None  # 'ACK', 'NACK', 'Access denied', 'Cannot respond'


async def request_pgn(
    session,
    pgn: int,
    destination: int = GLOBAL_ADDRESS,
    source: int = TOOL_ADDRESS,
    timeout: float = TP_TIMEOUT,
) -> List[RequestAnswer]:
    """Request a parameter group and collect the answers: data (single frame, BAM, RTS/CTS or ETP, which the tool
    accepts) or an acknowledgment (e.g. NACK for a PGN the node does not support). A request to one node returns
    on its first answer; a global request collects answers for `timeout` seconds (longer while a transfer is in
    progress, up to four times that)."""
    answers: List[RequestAnswer] = []
    done = asyncio.Event()
    reassembler = TransportReassembler()
    transferring = {'active': False}

    def on_message(msg: can.Message) -> None:
        if not msg.is_rx:
            return
        if msg.is_extended_id:
            ident = J1939Id.from_can_id(msg.arbitration_id)
            if ident.pgn in (PGN_TP_CM, PGN_ETP_CM) and msg.data and msg.data[0] in (TP_RTS, ETP_RTS, TP_BAM):
                transferring['active'] = int.from_bytes(bytes(msg.data[5:8]), 'little') == pgn
        result = reassembler.on_message(msg)
        if result is None:
            return
        got_pgn, sender, receiver, payload = result
        if destination != GLOBAL_ADDRESS and sender != destination:
            return
        if receiver not in (source, GLOBAL_ADDRESS):
            return
        if got_pgn == pgn:
            answers.append(RequestAnswer(sender, payload))
        elif got_pgn == PGN_ACKNOWLEDGMENT and len(payload) >= 8 and int.from_bytes(payload[5:8], 'little') == pgn:
            answers.append(RequestAnswer(sender, None, ACK_CONTROL.get(payload[0], f'control {payload[0]}')))
        else:
            return
        transferring['active'] = False
        if destination != GLOBAL_ADDRESS:
            done.set()

    responder = TransportResponder.attach(session, source)
    session.add_message_listener(on_message)
    try:
        J1939Sender(session).request(pgn, destination, source)
        loop = asyncio.get_running_loop()
        deadline, limit = loop.time() + timeout, loop.time() + 4 * timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                if transferring['active'] and loop.time() < limit:
                    deadline = min(loop.time() + timeout, limit)
                    continue
                break
            try:
                await asyncio.wait_for(done.wait(), remaining)
                break
            except asyncio.TimeoutError:
                continue
    finally:
        session.remove_message_listener(on_message)
        responder.close()
    return answers


# Address claiming (J1939-81)

ARBITRARY_FIRST, ARBITRARY_LAST = 128, 247  # Self-configurable addresses of industry group 0 (global)
CLAIM_WAIT = 0.25  # A node may use its address 250 ms after its claim when nobody contended


class AddressClaimError(Exception):
    pass


class AddressClaimer:
    """Claims an address for the tool (J1939-81) and keeps it: answers requests for address claims, defends the
    address against claims with a lower priority NAME, and moves to another address (when its NAME is arbitrary
    address capable) or announces that it cannot claim one when it loses. A commanded address (PGN 0xFED8) for its
    NAME moves it there.

    `claim` sends the claim and waits 250 ms for contention; the claimer then stays active until `close`."""

    def __init__(self, session, name: J1939Name, address: int, log: Optional[Callable[[str], None]] = None):
        if not 0 <= address <= 0xFD:
            raise ValueError('Addresses are 0x00 to 0xFD')
        self.session, self.name = session, name
        self.address: Optional[int] = None  # The claimed address, None while claiming or after losing it
        self._wanted = address
        self._taken: Dict[int, int] = {}  # Address -> NAME value of other nodes' claims
        self._log = log or (lambda text: None)
        self._candidate = address  # The address being claimed
        self._lost = asyncio.Event()
        self._listening = False

    def _send_claim(self, address: int) -> None:
        self.session.send(
            can.Message(
                arbitration_id=build_can_id(PGN_ADDRESS_CLAIMED, address, GLOBAL_ADDRESS, 6),
                is_extended_id=True,
                data=self.name.to_bytes(),
            )
        )

    async def claim(self) -> int:
        """Claim the address; returns it (or the arbitrary address it moved to). Raises AddressClaimError."""
        if not self._listening:
            self.session.add_message_listener(self._on_message)
            self._listening = True
        address = self._wanted
        while True:
            self._lost.clear()
            self.address = None
            self._candidate = address
            self._send_claim(address)
            try:
                await asyncio.wait_for(self._lost.wait(), CLAIM_WAIT)
            except asyncio.TimeoutError:
                self.address = address
                self._log(f'Claimed address 0x{address:02X}')
                return address
            address = self._next_address(address)
            if address is None:
                self._cannot_claim()
                raise AddressClaimError(
                    f'Cannot claim address 0x{self._candidate:02X}: a node with a higher priority NAME holds it'
                )

    def _next_address(self, lost: int) -> Optional[int]:
        if not self.name.arbitrary_address_capable:
            return None
        for address in range(ARBITRARY_FIRST, ARBITRARY_LAST + 1):
            if address != lost and address not in self._taken:
                return address
        return None

    def _cannot_claim(self) -> None:
        self.address = None
        self._send_claim(NULL_ADDRESS)
        self._log('Cannot claim an address')

    def close(self) -> None:
        if self._listening:
            self.session.remove_message_listener(self._on_message)
            self._listening = False
        self.address = None

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or not msg.is_extended_id or msg.is_error_frame:
            return
        ident = J1939Id.from_can_id(msg.arbitration_id)
        data = bytes(msg.data)
        ours = self.address if self.address is not None else self._candidate
        if ident.pgn == PGN_ADDRESS_CLAIMED and len(data) >= 8 and ident.source_address != NULL_ADDRESS:
            other = J1939Name.from_bytes(data).value
            self._taken[ident.source_address] = other
            if ident.source_address != ours or other == self.name.value:
                return
            if other < self.name.value:  # The lower NAME wins the address
                self._log(f'Lost address 0x{ours:02X} to NAME {other:016X}')
                if self.address is None:
                    self._lost.set()  # claim() moves on
                else:
                    self.address = None
                    asyncio.ensure_future(self._reclaim(ours))
            else:
                self._send_claim(ours)  # Defend
        elif ident.pgn == PGN_REQUEST and len(data) >= 3 and int.from_bytes(data[:3], 'little') == PGN_ADDRESS_CLAIMED:
            if ident.destination_address in (GLOBAL_ADDRESS, ours):
                if self.address is not None:
                    self._send_claim(self.address)
        elif ident.pgn == PGN_COMMANDED_ADDRESS and len(data) >= 9 and J1939Name.from_bytes(data[:8]) == self.name:
            self._wanted = data[8]
            self._log(f'Commanded to address 0x{data[8]:02X}')
            self.address = None
            asyncio.ensure_future(self._reclaim(None))

    async def _reclaim(self, lost: Optional[int]) -> None:
        if lost is not None:
            following = self._next_address(lost)
            if following is None:
                self._cannot_claim()
                return
            self._wanted = following
        try:
            await self.claim()
        except AddressClaimError:
            pass
        except Exception as e:  # noqa: BLE001 - the bus closed
            self._log(f'Claiming failed: {e}')
