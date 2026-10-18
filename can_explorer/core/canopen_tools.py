"""CANopen master tools (CiA 301): object dictionaries from EDS/DCF files, an SDO client, NMT, PDO mappings,
a network tracker and a simulated node."""

import asyncio
import configparser
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import can

from can_explorer.core.protocols.canopen import NMT_COMMANDS, NMT_STATES

NMT_START, NMT_STOP, NMT_PRE_OPERATIONAL, NMT_RESET_NODE, NMT_RESET_COMMUNICATION = 0x01, 0x02, 0x80, 0x81, 0x82
STATE_BOOT_UP, STATE_STOPPED, STATE_OPERATIONAL, STATE_PRE_OPERATIONAL = 0x00, 0x04, 0x05, 0x7F
SDO_TIMEOUT = 1.0

# Data types: name, size in bytes (0: variable), kind
DATA_TYPES: Dict[int, Tuple[str, int, str]] = {
    0x01: ('BOOLEAN', 1, 'bool'),
    0x02: ('INTEGER8', 1, 'int'),
    0x03: ('INTEGER16', 2, 'int'),
    0x04: ('INTEGER32', 4, 'int'),
    0x05: ('UNSIGNED8', 1, 'uint'),
    0x06: ('UNSIGNED16', 2, 'uint'),
    0x07: ('UNSIGNED32', 4, 'uint'),
    0x08: ('REAL32', 4, 'float'),
    0x09: ('VISIBLE_STRING', 0, 'str'),
    0x0A: ('OCTET_STRING', 0, 'bytes'),
    0x0B: ('UNICODE_STRING', 0, 'unicode'),
    0x0F: ('DOMAIN', 0, 'bytes'),
    0x10: ('INTEGER24', 3, 'int'),
    0x11: ('REAL64', 8, 'float'),
    0x12: ('INTEGER40', 5, 'int'),
    0x13: ('INTEGER48', 6, 'int'),
    0x14: ('INTEGER56', 7, 'int'),
    0x15: ('INTEGER64', 8, 'int'),
    0x16: ('UNSIGNED24', 3, 'uint'),
    0x18: ('UNSIGNED40', 5, 'uint'),
    0x19: ('UNSIGNED48', 6, 'uint'),
    0x1A: ('UNSIGNED56', 7, 'uint'),
    0x1B: ('UNSIGNED64', 8, 'uint'),
}

SDO_ABORT_CODES = {
    0x05030000: 'Toggle bit not alternated',
    0x05040000: 'SDO protocol timed out',
    0x05040001: 'Command specifier not valid or unknown',
    0x05040002: 'Invalid block size',
    0x05040003: 'Invalid sequence number',
    0x05040004: 'CRC error',
    0x05040005: 'Out of memory',
    0x06010000: 'Unsupported access to an object',
    0x06010001: 'Attempt to read a write only object',
    0x06010002: 'Attempt to write a read only object',
    0x06020000: 'Object does not exist in the object dictionary',
    0x06040041: 'Object cannot be mapped to the PDO',
    0x06040042: 'The number and length of the objects to be mapped would exceed the PDO length',
    0x06040043: 'General parameter incompatibility',
    0x06040047: 'General internal incompatibility in the device',
    0x06060000: 'Access failed due to a hardware error',
    0x06070010: 'Data type does not match, length of service parameter does not match',
    0x06070012: 'Data type does not match, length of service parameter too high',
    0x06070013: 'Data type does not match, length of service parameter too low',
    0x06090011: 'Sub-index does not exist',
    0x06090030: 'Invalid value for parameter',
    0x06090031: 'Value of parameter written too high',
    0x06090032: 'Value of parameter written too low',
    0x06090036: 'Maximum value is less than minimum value',
    0x060A0023: 'Resource not available: SDO connection',
    0x08000000: 'General error',
    0x08000020: 'Data cannot be transferred or stored to the application',
    0x08000021: 'Data cannot be transferred or stored to the application because of local control',
    0x08000022: 'Data cannot be transferred or stored to the application because of the present device state',
    0x08000023: 'Object dictionary dynamic generation fails or no object dictionary is present',
    0x08000024: 'No data available',
}

EMCY_CLASSES = {
    0x00: 'Error reset or no error',
    0x10: 'Generic error',
    0x20: 'Current',
    0x21: 'Current, device input side',
    0x22: 'Current inside the device',
    0x23: 'Current, device output side',
    0x30: 'Voltage',
    0x31: 'Mains voltage',
    0x32: 'Voltage inside the device',
    0x33: 'Output voltage',
    0x40: 'Temperature',
    0x41: 'Ambient temperature',
    0x42: 'Device temperature',
    0x50: 'Device hardware',
    0x60: 'Device software',
    0x61: 'Internal software',
    0x62: 'User software',
    0x63: 'Data set',
    0x70: 'Additional modules',
    0x80: 'Monitoring',
    0x81: 'Communication',
    0x82: 'Protocol error',
    0x90: 'External error',
    0xF0: 'Additional functions',
    0xFF: 'Device specific',
}
EMCY_CODES = {
    0x8110: 'CAN overrun (objects lost)',
    0x8120: 'CAN in error passive mode',
    0x8130: 'Life guard or heartbeat error',
    0x8140: 'Recovered from bus off',
    0x8150: 'CAN-ID collision',
    0x8210: 'PDO not processed due to length error',
    0x8220: 'PDO length exceeded',
    0x8240: 'Unexpected SYNC data length',
    0x8250: 'RPDO timeout',
}

# Objects every device has, for when no EDS is loaded
STANDARD_OBJECTS = {
    0x1000: 'Device type',
    0x1001: 'Error register',
    0x1003: 'Pre-defined error field',
    0x1005: 'COB-ID SYNC',
    0x1008: 'Manufacturer device name',
    0x1009: 'Manufacturer hardware version',
    0x100A: 'Manufacturer software version',
    0x1017: 'Producer heartbeat time',
    0x1018: 'Identity object',
}


def emcy_text(code: int) -> str:
    if code in EMCY_CODES:
        return EMCY_CODES[code]
    return EMCY_CLASSES.get(code >> 8, EMCY_CLASSES.get((code >> 12) << 4, 'Unknown error'))


class SdoAbort(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f'SDO abort 0x{code:08X}: {SDO_ABORT_CODES.get(code, "Unknown abort code")}')


# Values


def parse_eds_int(text: str, node_id: int = 0) -> Optional[int]:
    """'0x1018', '12', '-5', '$NODEID+0x180' or '0x180+$NODEID'; None for an empty or other text."""
    text = (text or '').strip()
    if not text:
        return None
    total = 0
    for part in re.split(r'\+', text):
        part = part.strip()
        if part.upper() == '$NODEID':
            total += node_id
            continue
        try:
            total += int(part, 0) if not re.fullmatch(r'-?0\d+', part) else int(part, 10)  # '010' is decimal
        except ValueError:
            return None
    return total


def data_type_name(data_type: int) -> str:
    return DATA_TYPES.get(data_type, (f'0x{data_type:04X}', 0, 'bytes'))[0]


def decode_value(data_type: int, data: bytes) -> Any:
    _, size, kind = DATA_TYPES.get(data_type, ('', 0, 'bytes'))
    data = bytes(data)
    if kind == 'str':
        return data.split(b'\x00', 1)[0].decode('latin-1')
    if kind == 'unicode':
        return data.decode('utf-16-le', errors='replace')
    if kind == 'bytes':
        return data
    if size and len(data) < size:
        raise ValueError(f'{data_type_name(data_type)} needs {size} bytes, got {len(data)}')
    data = data[:size] if size else data
    if kind == 'float':
        return struct.unpack('<f' if size == 4 else '<d', data)[0]
    if kind == 'bool':
        return bool(data[0])
    return int.from_bytes(data, 'little', signed=kind == 'int')


def encode_value(data_type: int, value: Any) -> bytes:
    """Bytes of a value; texts are parsed (numbers in decimal or 0x hex, octet strings as hex bytes)."""
    _, size, kind = DATA_TYPES.get(data_type, ('', 0, 'bytes'))
    if kind == 'str':
        return str(value).encode('latin-1')
    if kind == 'unicode':
        return str(value).encode('utf-16-le')
    if kind == 'bytes':
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return bytes.fromhex(str(value).replace('0x', '').replace(',', ' '))
    if isinstance(value, str):
        text = value.strip()
        if kind == 'float':
            value = float(text)
        elif kind == 'bool' and text.lower() in ('true', 'false'):
            value = text.lower() == 'true'
        else:
            try:
                value = int(text, 0)
            except ValueError:
                raise ValueError(f'{data_type_name(data_type)} needs a number, got {text!r}') from None
    if kind == 'float':
        return struct.pack('<f' if size == 4 else '<d', float(value))
    if kind == 'bool':
        return bytes([1 if value else 0])
    value = int(value)
    bits = size * 8
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if kind == 'int' else (0, (1 << bits) - 1)
    if not low <= value <= high:
        raise ValueError(f'{data_type_name(data_type)} values are {low} to {high}')
    return value.to_bytes(size, 'little', signed=kind == 'int')


def format_value(data_type: int, value: Any) -> str:
    kind = DATA_TYPES.get(data_type, ('', 0, 'bytes'))[2]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex(' ').upper()
    if kind == 'uint' and isinstance(value, int) and value > 9:
        return f'{value} (0x{value:X})'
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


# Object dictionary


@dataclass
class OdEntry:
    """A variable: an object of type VAR, or a sub-index of an ARRAY or RECORD."""

    index: int
    subindex: int
    name: str
    data_type: int = 0x07
    access: str = 'rw'
    default: str = ''
    value: str = ''  # ParameterValue of a DCF
    pdo_mappable: bool = False
    low_limit: str = ''
    high_limit: str = ''
    parent: str = ''  # Name of the ARRAY or RECORD

    @property
    def full_name(self) -> str:
        return f'{self.parent}.{self.name}' if self.parent else self.name

    @property
    def readable(self) -> bool:
        return self.access != 'wo'

    @property
    def writable(self) -> bool:
        return self.access in ('rw', 'rww', 'rwr', 'wo')


@dataclass
class OdObject:
    index: int
    name: str
    object_type: int = 7  # 7 VAR, 8 ARRAY, 9 RECORD
    entries: Dict[int, OdEntry] = field(default_factory=dict)


class ObjectDictionary:
    """The objects of a device, from an EDS or DCF file (CiA 306)."""

    def __init__(self):
        self.objects: Dict[int, OdObject] = {}
        self.path: Optional[Path] = None
        self.device_info: Dict[str, str] = {}
        self.node_id = 0  # From a DCF's [DeviceComissioning]
        self.overrides: Dict[Tuple[int, int], int] = {}  # Values read from the node

    @classmethod
    def from_file(cls, path) -> 'ObjectDictionary':
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str  # Keys keep their case
        text = Path(path).read_text(encoding='latin-1')
        parser.read_string(text)
        od = cls()
        od.path = Path(path)
        sections = {name.upper(): parser[name] for name in parser.sections()}
        if 'DEVICEINFO' in sections:
            od.device_info = dict(sections['DEVICEINFO'])
        commissioning = sections.get('DEVICECOMISSIONING') or sections.get('DEVICECOMMISSIONING')
        if commissioning is not None:
            od.node_id = parse_eds_int(commissioning.get('NodeID', '')) or 0
        objects = sorted(int(name, 16) for name in sections if re.fullmatch(r'[0-9A-F]{4}', name))
        if not objects:
            raise ValueError(f'{Path(path).name} has no objects; is it an EDS or DCF file?')
        for index in objects:
            section = sections[f'{index:04X}']
            name = section.get('ParameterName', f'Object 0x{index:04X}')
            object_type = parse_eds_int(section.get('ObjectType', '0x7')) or 7
            obj = OdObject(index, name, object_type)
            if object_type == 7:
                obj.entries[0] = cls._entry(index, 0, section, '')
            else:
                subs = sorted(
                    int(key[7:], 16)
                    for key in sections
                    if key.startswith(f'{index:04X}SUB') and re.fullmatch(r'[0-9A-F]+', key[7:])
                )
                for subindex in subs:
                    obj.entries[subindex] = cls._entry(index, subindex, sections[f'{index:04X}SUB{subindex:X}'], name)
                compact = parse_eds_int(section.get('CompactSubObj', ''))
                if compact and not subs:  # CompactSubObj: a count and N entries of the same type
                    obj.entries[0] = OdEntry(index, 0, 'Number of entries', 0x05, 'ro', str(compact), parent=name)
                    for subindex in range(1, compact + 1):
                        entry = cls._entry(index, subindex, section, name)
                        entry.name = f'Entry {subindex}'
                        obj.entries[subindex] = entry
            od.objects[index] = obj
        return od

    @staticmethod
    def _entry(index: int, subindex: int, section, parent: str) -> OdEntry:
        return OdEntry(
            index,
            subindex,
            section.get('ParameterName', f'Sub {subindex}'),
            parse_eds_int(section.get('DataType', '0x7')) or 0x07,
            section.get('AccessType', 'rw').strip().lower(),
            section.get('DefaultValue', '').strip(),
            section.get('ParameterValue', '').strip(),
            (parse_eds_int(section.get('PDOMapping', '0')) or 0) != 0,
            section.get('LowLimit', '').strip(),
            section.get('HighLimit', '').strip(),
            parent,
        )

    def entry(self, index: int, subindex: int = 0) -> Optional[OdEntry]:
        obj = self.objects.get(index)
        return obj.entries.get(subindex) if obj is not None else None

    def entries(self) -> List[OdEntry]:
        return [entry for obj in self.objects.values() for _, entry in sorted(obj.entries.items())]

    def find(self, name: str) -> Optional[OdEntry]:
        """An entry by its name or 'Object.Sub' name, ignoring case."""
        name = name.strip().lower()
        for entry in self.entries():
            if entry.full_name.lower() == name or (not entry.parent and entry.name.lower() == name):
                return entry
        return None

    def int_value(self, index: int, subindex: int, node_id: int) -> Optional[int]:
        """The value read from the node, else the DCF value, else the default."""
        if (index, subindex) in self.overrides:
            return self.overrides[(index, subindex)]
        entry = self.entry(index, subindex)
        if entry is None:
            return None
        return parse_eds_int(entry.value or entry.default, node_id)

    def name(self, index: int, subindex: int) -> str:
        entry = self.entry(index, subindex)
        if entry is not None:
            return entry.full_name
        return STANDARD_OBJECTS.get(index, f'0x{index:04X}') + (f' sub {subindex}' if subindex else '')


# PDOs


@dataclass
class PdoVariable:
    index: int
    subindex: int
    bits: int
    name: str
    data_type: int


@dataclass
class Pdo:
    kind: str  # 'RPDO' or 'TPDO'
    number: int  # 1 based
    cob_id: int
    valid: bool
    transmission_type: Optional[int]
    event_timer: Optional[int]
    variables: List[PdoVariable]

    @property
    def name(self) -> str:
        return f'{self.kind}{self.number}'

    @property
    def transmission_text(self) -> str:
        kind = self.transmission_type
        if kind is None:
            return ''
        if kind == 0:
            return 'synchronous (acyclic)'
        if kind <= 240:
            return 'every SYNC' if kind == 1 else f'every {kind} SYNCs'
        if kind in (252, 253):
            return 'remote request'
        if kind >= 254:
            return 'event driven' + (f', {self.event_timer} ms' if self.event_timer else '')
        return f'type {kind}'

    @property
    def length(self) -> int:
        return (sum(variable.bits for variable in self.variables) + 7) // 8

    def decode(self, data: bytes) -> List[Tuple[str, str]]:
        """The values of the mapped variables in a PDO frame."""
        raw = int.from_bytes(bytes(data), 'little')
        offset = 0
        values = []
        for variable in self.variables:
            if offset + variable.bits > len(data) * 8:
                values.append((variable.name, '–'))
                offset += variable.bits
                continue
            bits = (raw >> offset) & ((1 << variable.bits) - 1)
            offset += variable.bits
            chunk = bits.to_bytes((variable.bits + 7) // 8, 'little')
            try:
                value = format_value(variable.data_type, decode_value(variable.data_type, chunk))
            except ValueError:
                value = chunk.hex(' ').upper()
            values.append((variable.name, value))
        return values


PDO_KINDS = (('RPDO', 0x1400, 0x1600, 0x200), ('TPDO', 0x1800, 0x1A00, 0x180))


def pdo_mappings(od: ObjectDictionary, node_id: int) -> List[Pdo]:
    """The PDOs configured in an object dictionary: communication parameters and mapped objects."""
    pdos = []
    for kind, comm_base, map_base, default_cob in PDO_KINDS:
        for number in range(512):
            if comm_base + number not in od.objects:
                continue
            cob_id = od.int_value(comm_base + number, 1, node_id)
            if cob_id is None:
                cob_id = default_cob + 0x100 * number + node_id if number < 4 else 0x80000000
            variables = []
            count = od.int_value(map_base + number, 0, node_id) or 0
            for subindex in range(1, count + 1):
                mapping = od.int_value(map_base + number, subindex, node_id)
                if not mapping:
                    continue
                index, sub, bits = mapping >> 16, (mapping >> 8) & 0xFF, mapping & 0xFF
                entry = od.entry(index, sub)
                variables.append(PdoVariable(index, sub, bits, od.name(index, sub), entry.data_type if entry else 0x07))
            pdos.append(
                Pdo(
                    kind,
                    number + 1,
                    cob_id & 0x1FFFFFFF if cob_id & 0x20000000 else cob_id & 0x7FF,
                    not cob_id & 0x80000000,
                    od.int_value(comm_base + number, 2, node_id),
                    od.int_value(comm_base + number, 5, node_id) if kind == 'TPDO' else None,
                    variables,
                )
            )
    return pdos


# Network tracking


@dataclass
class CanOpenNode:
    node_id: int
    state: Optional[int] = None
    last_heartbeat: float = 0.0
    boot_ups: int = 0
    emcy: List[Tuple[float, int, int, bytes]] = field(default_factory=list)  # time, code, register, data
    frames: int = 0

    @property
    def state_name(self) -> str:
        return NMT_STATES.get(self.state, '') if self.state is not None else ''


class CanOpenNetwork:
    """Nodes seen on the bus (heartbeats, boot-ups, EMCY) and the last data of every PDO."""

    MAX_EMCY = 50

    def __init__(self):
        self.nodes: Dict[int, CanOpenNode] = {}
        self.pdo_data: Dict[int, Tuple[float, bytes]] = {}  # COB-ID -> last frame
        self.changed = False

    def node(self, node_id: int) -> CanOpenNode:
        if node_id not in self.nodes:
            self.nodes[node_id] = CanOpenNode(node_id)
        return self.nodes[node_id]

    def on_message(self, msg: can.Message) -> None:
        if msg.is_extended_id or msg.is_error_frame or msg.is_remote_frame:
            return
        cob_id = msg.arbitration_id
        function, node_id = cob_id & 0x780, cob_id & 0x7F
        data = bytes(msg.data)
        if not node_id:
            return
        if function == 0x700 and data:
            node = self.node(node_id)
            node.state = data[0] & 0x7F
            node.last_heartbeat = msg.timestamp
            if node.state == STATE_BOOT_UP:
                node.boot_ups += 1
        elif function == 0x080 and len(data) >= 3:
            node = self.node(node_id)
            node.emcy.append((msg.timestamp, int.from_bytes(data[:2], 'little'), data[2], data[3:8]))
            del node.emcy[: -self.MAX_EMCY]
        elif 0x180 <= function <= 0x500:
            self.pdo_data[cob_id] = (msg.timestamp, data)
            if function in (0x180, 0x280, 0x380, 0x480):  # Transmitted by the node
                self.node(node_id).frames += 1
        elif function == 0x580:
            self.node(node_id).frames += 1
        else:
            return
        self.changed = True

    def clear(self) -> None:
        self.nodes.clear()
        self.pdo_data.clear()
        self.changed = True


# Master services


def nmt_message(command: int, node_id: int = 0) -> can.Message:
    """An NMT command to one node, or to all nodes with node_id 0."""
    if command not in NMT_COMMANDS:
        raise ValueError(f'Unknown NMT command 0x{command:02X}')
    if not 0 <= node_id <= 127:
        raise ValueError('Node IDs are 1 to 127 (0: all nodes)')
    return can.Message(arbitration_id=0x000, is_extended_id=False, data=bytes([command, node_id]))


class SdoClient:
    """Expedited and segmented SDO transfers with one node (default SDO channel 0x600/0x580 + node ID)."""

    def __init__(self, session, node_id: int, timeout: float = SDO_TIMEOUT):
        if not 1 <= node_id <= 127:
            raise ValueError('Node IDs are 1 to 127')
        self.session = session
        self.node_id = node_id
        self.timeout = timeout
        self._responses: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_extended_id and msg.arbitration_id == 0x580 + self.node_id and len(msg.data) == 8 and msg.is_rx:
            self._responses.put_nowait(bytes(msg.data))

    def _send(self, data: bytes) -> None:
        self.session.send(can.Message(arbitration_id=0x600 + self.node_id, is_extended_id=False, data=data))

    async def _exchange(self, request: bytes, index: int, subindex: int) -> bytes:
        self._send(request.ljust(8, b'\x00'))
        try:
            response = await asyncio.wait_for(self._responses.get(), self.timeout)
        except asyncio.TimeoutError:
            self._send(
                bytes([0x80]) + index.to_bytes(2, 'little') + bytes([subindex]) + (0x05040000).to_bytes(4, 'little')
            )
            raise TimeoutError(f'Node {self.node_id} did not answer the SDO request') from None
        if response[0] >> 5 == 4:
            raise SdoAbort(int.from_bytes(response[4:8], 'little'))
        return response

    async def upload(self, index: int, subindex: int) -> bytes:
        """Read an object from the node."""
        async with self._lock:
            self.session.add_message_listener(self._on_message)
            try:
                header = index.to_bytes(2, 'little') + bytes([subindex])
                response = await self._exchange(bytes([0x40]) + header, index, subindex)
                if response[0] >> 5 != 2:
                    raise ConnectionError(f'Unexpected SDO response 0x{response[0]:02X}')
                if response[0] & 0x02:  # Expedited
                    unused = (response[0] >> 2) & 0x3 if response[0] & 0x01 else 0
                    return response[4 : 8 - unused]
                size = int.from_bytes(response[4:8], 'little') if response[0] & 0x01 else None
                data = bytearray()
                toggle = 0
                while True:
                    response = await self._exchange(bytes([0x60 | (toggle << 4)]), index, subindex)
                    if response[0] >> 5 != 0 or (response[0] >> 4) & 1 != toggle:
                        raise ConnectionError('SDO segment out of sequence')
                    unused = (response[0] >> 1) & 0x7
                    data += response[1 : 8 - unused]
                    if response[0] & 0x01:  # Last segment
                        break
                    toggle ^= 1
                return bytes(data[:size] if size is not None else data)
            finally:
                self.session.remove_message_listener(self._on_message)

    async def download(self, index: int, subindex: int, data: bytes) -> None:
        """Write an object of the node."""
        data = bytes(data)
        async with self._lock:
            self.session.add_message_listener(self._on_message)
            try:
                header = index.to_bytes(2, 'little') + bytes([subindex])
                if 0 < len(data) <= 4:
                    command = 0x23 | ((4 - len(data)) << 2)  # Expedited, size indicated
                    response = await self._exchange(bytes([command]) + header + data, index, subindex)
                    if response[0] >> 5 != 3:
                        raise ConnectionError(f'Unexpected SDO response 0x{response[0]:02X}')
                    return
                response = await self._exchange(
                    bytes([0x21]) + header + len(data).to_bytes(4, 'little'), index, subindex
                )
                if response[0] >> 5 != 3:
                    raise ConnectionError(f'Unexpected SDO response 0x{response[0]:02X}')
                toggle = 0
                for offset in range(0, max(len(data), 1), 7):
                    segment = data[offset : offset + 7]
                    last = offset + 7 >= len(data)
                    command = (toggle << 4) | ((7 - len(segment)) << 1) | int(last)
                    response = await self._exchange(bytes([command]) + segment, index, subindex)
                    if response[0] >> 5 != 1 or (response[0] >> 4) & 1 != toggle:
                        raise ConnectionError('SDO segment acknowledgement out of sequence')
                    toggle ^= 1
            finally:
                self.session.remove_message_listener(self._on_message)


# A simulated node


class CanOpenNodeSimulator:
    """A CANopen slave on a session: NMT states, heartbeat, an SDO server and TPDOs, from an object dictionary."""

    def __init__(self, session, node_id: int, od: Optional[ObjectDictionary] = None, heartbeat_ms: int = 0):
        self.session = session
        self.node_id = node_id
        self.od = od or ObjectDictionary()
        self.values: Dict[Tuple[int, int], bytes] = {}
        for entry in self.od.entries():
            text = entry.value or entry.default
            if not text:
                continue
            try:
                number = parse_eds_int(text, node_id)
                self.values[(entry.index, entry.subindex)] = encode_value(
                    entry.data_type, number if number is not None else text
                )
            except ValueError:
                pass
        if heartbeat_ms:
            self.values[(0x1017, 0)] = heartbeat_ms.to_bytes(2, 'little')
        self.state = STATE_PRE_OPERATIONAL
        self._segments: Optional[Dict[str, Any]] = None
        self._tasks: List[asyncio.Task] = []
        self.on_write: Optional[Callable[[int, int, bytes], None]] = None

    def _send(self, cob_id: int, data: bytes) -> None:
        self.session.send(can.Message(arbitration_id=cob_id, is_extended_id=False, data=data))

    def start(self) -> None:
        self.session.add_message_listener(self._on_message)
        self._send(0x700 + self.node_id, bytes([STATE_BOOT_UP]))
        self._tasks = [asyncio.ensure_future(self._heartbeat()), asyncio.ensure_future(self._tpdos())]

    def stop(self) -> None:
        self.session.remove_message_listener(self._on_message)
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    def set_value(self, index: int, subindex: int, value: Any) -> None:
        entry = self.od.entry(index, subindex)
        self.values[(index, subindex)] = encode_value(entry.data_type if entry else 0x07, value)

    def emergency(self, code: int, register: int = 0x01, data: bytes = b'') -> None:
        self._send(0x080 + self.node_id, code.to_bytes(2, 'little') + bytes([register]) + bytes(data).ljust(5, b'\x00'))

    def _value_int(self, index: int, subindex: int) -> Optional[int]:
        data = self.values.get((index, subindex))
        return int.from_bytes(data, 'little') if data else None

    async def _heartbeat(self):
        while True:
            period = self._value_int(0x1017, 0) or 0
            await asyncio.sleep(period / 1000 if period else 0.2)
            if period:
                self._send(0x700 + self.node_id, bytes([self.state]))

    def _send_tpdos(self, synchronous: bool) -> None:
        """Event driven TPDOs (on the timer) or synchronous ones (on SYNC), as configured in the values."""
        if self.state != STATE_OPERATIONAL:
            return
        od = ObjectDictionary()
        od.objects = self.od.objects
        od.overrides = {key: int.from_bytes(value, 'little') for key, value in self.values.items() if len(value) <= 4}
        for pdo in pdo_mappings(od, self.node_id):
            if pdo.kind != 'TPDO' or not pdo.valid or not pdo.variables:
                continue
            if synchronous == (pdo.transmission_type is not None and pdo.transmission_type <= 240):
                self._send(pdo.cob_id, self._pdo_data(pdo))

    async def _tpdos(self):
        while True:
            await asyncio.sleep(0.1)
            self._send_tpdos(synchronous=False)

    def _pdo_data(self, pdo: Pdo) -> bytes:
        raw, offset = 0, 0
        for variable in pdo.variables:
            value = int.from_bytes(self.values.get((variable.index, variable.subindex), b'\x00'), 'little')
            raw |= (value & ((1 << variable.bits) - 1)) << offset
            offset += variable.bits
        return raw.to_bytes(pdo.length, 'little')

    def _on_message(self, msg: can.Message) -> None:
        if msg.is_extended_id or not msg.is_rx:
            return
        data = bytes(msg.data)
        if msg.arbitration_id == 0x000 and len(data) >= 2 and data[1] in (0, self.node_id):
            self._nmt(data[0])
        elif msg.arbitration_id == 0x080:  # SYNC
            self._send_tpdos(synchronous=True)
        elif msg.arbitration_id == 0x600 + self.node_id and len(data) == 8 and self.state != STATE_STOPPED:
            response = self._sdo(data)
            if response is not None:
                self._send(0x580 + self.node_id, response.ljust(8, b'\x00'))

    def _nmt(self, command: int) -> None:
        if command == NMT_START:
            self.state = STATE_OPERATIONAL
        elif command == NMT_STOP:
            self.state = STATE_STOPPED
        elif command == NMT_PRE_OPERATIONAL:
            self.state = STATE_PRE_OPERATIONAL
        elif command in (NMT_RESET_NODE, NMT_RESET_COMMUNICATION):
            self.state = STATE_PRE_OPERATIONAL
            self._send(0x700 + self.node_id, bytes([STATE_BOOT_UP]))

    def _abort(self, header: bytes, code: int) -> bytes:
        self._segments = None
        return bytes([0x80]) + header + code.to_bytes(4, 'little')

    def _sdo(self, data: bytes) -> Optional[bytes]:  # noqa: C901 (one branch per command)
        command = data[0] >> 5
        header = data[1:4]
        index, subindex = int.from_bytes(data[1:3], 'little'), data[3]
        if command == 2:  # Upload request
            entry = self.od.entry(index, subindex)
            if (index, subindex) not in self.values and entry is None:
                return self._abort(header, 0x06020000 if index not in self.od.objects else 0x06090011)
            if entry is not None and not entry.readable:
                return self._abort(header, 0x06010001)
            value = self.values.get((index, subindex), b'')
            if 0 < len(value) <= 4:
                return bytes([0x43 | ((4 - len(value)) << 2)]) + header + value
            self._segments = {'data': value, 'offset': 0, 'toggle': 0, 'upload': True, 'header': header}
            return bytes([0x41]) + header + len(value).to_bytes(4, 'little')
        if command == 3 and self._segments and self._segments['upload']:  # Upload segment
            state = self._segments
            if (data[0] >> 4) & 1 != state['toggle']:
                return self._abort(state['header'], 0x05030000)
            segment = state['data'][state['offset'] : state['offset'] + 7]
            state['offset'] += 7
            last = state['offset'] >= len(state['data'])
            response = bytes([(state['toggle'] << 4) | ((7 - len(segment)) << 1) | int(last)]) + segment
            state['toggle'] ^= 1
            if last:
                self._segments = None
            return response
        if command == 1:  # Download request
            entry = self.od.entry(index, subindex)
            if entry is None and (index, subindex) not in self.values:
                return self._abort(header, 0x06020000 if index not in self.od.objects else 0x06090011)
            if entry is not None and not entry.writable:
                return self._abort(header, 0x06010002)
            if data[0] & 0x02:  # Expedited
                unused = (data[0] >> 2) & 0x3 if data[0] & 0x01 else 0
                self._store(index, subindex, data[4 : 8 - unused])
                return bytes([0x60]) + header + bytes(4)
            self._segments = {
                'data': bytearray(),
                'toggle': 0,
                'upload': False,
                'header': header,
                'key': (index, subindex),
            }
            return bytes([0x60]) + header + bytes(4)
        if command == 0 and self._segments and not self._segments['upload']:  # Download segment
            state = self._segments
            if (data[0] >> 4) & 1 != state['toggle']:
                return self._abort(state['header'], 0x05030000)
            unused = (data[0] >> 1) & 0x7
            state['data'] += data[1 : 8 - unused]
            response = bytes([0x20 | (state['toggle'] << 4)])
            state['toggle'] ^= 1
            if data[0] & 0x01:
                self._store(*state['key'], bytes(state['data']))
                self._segments = None
            return response
        if command == 4:  # Abort from the client
            self._segments = None
            return None
        return self._abort(header, 0x05040001)

    def _store(self, index: int, subindex: int, value: bytes) -> None:
        self.values[(index, subindex)] = value
        if self.on_write is not None:
            self.on_write(index, subindex, value)
