"""CANopen over EtherCAT (CoE): an EtherCAT master for configuring slaves through their CANopen object dictionary.

The master finds the slaves of a network adapter, changes their EtherCAT states (Init, Pre-Op, Safe-Op) and reads
and writes their objects with CoE SDO transfers: the object dictionary comes from the slave (SDO information) or
from an EDS file, the PDO assignment (0x1C12/0x1C13) and mapping (0x16xx/0x1Axx) from the slave's objects.
Emergency messages of the slaves are collected. Cyclic process data (the Op state) is not exchanged.

The real network needs the optional pysoem package (SOEM; `uv sync --extra ethercat`) and raw access to the
network adapter: root or the CAP_NET_RAW capability on Linux, Npcap on Windows, access to /dev/bpf* on macOS.
`SimulatedBackend` stands in for a network without hardware.

SOEM is not thread safe and blocks, so every call of a backend runs on one worker thread of `EtherCatMaster`.
"""

import asyncio
import importlib.util
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from can_explorer.core.canopen_tools import (
    STANDARD_OBJECTS,
    ObjectDictionary,
    OdEntry,
    OdObject,
    PdoVariable,
    SdoAbort,
    decode_value,
    encode_value,
)

SIMULATED = 'simulated'  # Adapter name of the simulated network

# EtherCAT states (AL status register); STATE_ERROR is set together with the state a slave fell back to
STATE_NONE, STATE_INIT, STATE_PREOP, STATE_BOOT, STATE_SAFEOP, STATE_OP = 0x00, 0x01, 0x02, 0x03, 0x04, 0x08
STATE_ERROR = 0x10
STATE_NAMES = {
    STATE_NONE: 'None',
    STATE_INIT: 'Init',
    STATE_PREOP: 'Pre-Op',
    STATE_BOOT: 'Bootstrap',
    STATE_SAFEOP: 'Safe-Op',
    STATE_OP: 'Op',
}
# The states the master can request: Op needs cyclic process data, which the master does not exchange
REQUESTABLE_STATES = (STATE_INIT, STATE_PREOP, STATE_SAFEOP)
STATE_TIMEOUT_US = 2_000_000

# AL status codes (ETG.1000.6), why a slave refused a state change
AL_STATUS_CODES = {
    0x0000: '',
    0x0001: 'Unspecified error',
    0x0002: 'No memory',
    0x0011: 'Invalid requested state change',
    0x0012: 'Unknown requested state',
    0x0013: 'Bootstrap not supported',
    0x0014: 'No valid firmware',
    0x0015: 'Invalid mailbox configuration (Bootstrap)',
    0x0016: 'Invalid mailbox configuration (Pre-Op)',
    0x0017: 'Invalid sync manager configuration',
    0x0018: 'No valid inputs available',
    0x0019: 'No valid outputs',
    0x001A: 'Synchronization error',
    0x001B: 'Sync manager watchdog',
    0x001D: 'Invalid output configuration',
    0x001E: 'Invalid input configuration',
    0x001F: 'Invalid watchdog configuration',
    0x0020: 'Slave needs cold start',
    0x0021: 'Slave needs Init',
    0x0022: 'Slave needs Pre-Op',
    0x0023: 'Slave needs Safe-Op',
    0x0024: 'Invalid input mapping',
    0x0025: 'Invalid output mapping',
    0x0026: 'Inconsistent settings',
    0x0027: 'Free-run not supported',
    0x0028: 'Synchronization not supported',
    0x002C: 'Fatal sync error',
    0x002D: 'No sync error',
    0x0030: 'Invalid DC sync configuration',
    0x0035: 'Invalid sync cycle time',
    0x0050: 'EEPROM no access',
    0x0051: 'EEPROM error',
}

# PDO assignment of the process data sync managers: SM2 outputs (RxPDOs), SM3 inputs (TxPDOs)
PDO_ASSIGNMENTS = (('RxPDO', 0x1C12), ('TxPDO', 0x1C13))

# ObjAccess of the SDO information service (ETG.1000.6)
ACCESS_READ = 0x07  # Readable in Pre-Op, Safe-Op or Op
ACCESS_WRITE = 0x38  # Writable in Pre-Op, Safe-Op or Op
ACCESS_MAPPABLE = 0xC0  # RxPDO or TxPDO mappable


def ethercat_available() -> bool:
    """Whether pysoem is installed, for the real network (checked without importing it)."""
    return importlib.util.find_spec('pysoem') is not None


def state_name(state: int) -> str:
    name = STATE_NAMES.get(state & 0x0F, f'0x{state & 0x0F:X}')
    return f'{name} + Error' if state & STATE_ERROR else name


def al_status_text(code: int) -> str:
    return AL_STATUS_CODES.get(code, f'AL status 0x{code:04X}')


def access_text(obj_access: int) -> str:
    """ObjAccess bits as an EDS access type: 'ro', 'wo' or 'rw'."""
    readable, writable = bool(obj_access & ACCESS_READ), bool(obj_access & ACCESS_WRITE)
    return 'rw' if readable and writable else 'wo' if writable else 'ro'


@dataclass
class EtherCatSlave:
    position: int  # 1 based, in the order of the network
    name: str
    vendor_id: int
    product_code: int
    revision: int
    state: int = STATE_NONE
    al_status: int = 0
    coe: bool = True  # Has a mailbox with CoE: objects can be read and written

    @property
    def state_name(self) -> str:
        return state_name(self.state)

    @property
    def error(self) -> bool:
        return bool(self.state & STATE_ERROR)


@dataclass
class CoeEmergency:
    timestamp: float
    position: int
    code: int
    register: int
    data: bytes


@dataclass
class CoePdo:
    """A PDO assigned to a process data sync manager, with its mapping."""

    kind: str  # 'RxPDO' (outputs) or 'TxPDO' (inputs)
    index: int  # Mapping object, e.g. 0x1600
    variables: List[PdoVariable] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f'{self.kind} 0x{self.index:04X}'

    @property
    def bits(self) -> int:
        return sum(variable.bits for variable in self.variables)


class EtherCatBackend:
    """The network the master works on. Every method blocks; `EtherCatMaster` calls them on its worker thread."""

    def adapters(self) -> List[Tuple[str, str]]:
        """(name, description) of the network adapters."""
        raise NotImplementedError

    def open(self, adapter: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def scan(self) -> List[EtherCatSlave]:
        """Find the slaves and bring them to Pre-Op (mailboxes configured)."""
        raise NotImplementedError

    def read_states(self) -> List[Tuple[int, int]]:
        """(state, AL status code) of every slave, in network order."""
        raise NotImplementedError

    def request_state(self, position: int, state: int) -> None:
        """Request a state of one slave, or of all with position 0, and wait until it is reached."""
        raise NotImplementedError

    def sdo_read(self, position: int, index: int, subindex: int) -> bytes:
        raise NotImplementedError

    def sdo_write(self, position: int, index: int, subindex: int, data: bytes) -> None:
        raise NotImplementedError

    def object_dictionary(self, position: int) -> ObjectDictionary:
        """The objects of a slave, from its SDO information service."""
        raise NotImplementedError

    def set_emergency_handler(self, handler: Callable[[CoeEmergency], None]) -> None:
        self._emergency_handler = handler

    def _emergency(self, emergency: CoeEmergency) -> None:
        handler = getattr(self, '_emergency_handler', None)
        if handler is not None:
            handler(emergency)


class SoemBackend(EtherCatBackend):
    """A real EtherCAT network through SOEM (pysoem)."""

    def __init__(self):
        import pysoem  # The optional dependency, imported only for a real network

        self._pysoem = pysoem
        self._master = None
        self._mapped = False

    def adapters(self) -> List[Tuple[str, str]]:
        adapters = []
        for adapter in self._pysoem.find_adapters():
            description = adapter.desc  # Bytes: the name on Linux, the driver's description on Windows
            if isinstance(description, bytes):
                description = description.decode('utf-8', errors='replace')
            adapters.append((adapter.name, description or adapter.name))
        return adapters

    def open(self, adapter: str) -> None:
        master = self._pysoem.Master()
        try:
            master.open(adapter)
        except ConnectionError:
            raise ConnectionError(
                f'Could not open {adapter}: it does not exist or needs raw network access '
                '(Linux: root or CAP_NET_RAW, Windows: Npcap, macOS: access to /dev/bpf*)'
            ) from None
        self._master, self._mapped = master, False

    def close(self) -> None:
        if self._master is not None:
            try:
                self._master.close()
            finally:
                self._master = None

    def _slave(self, position: int):
        if self._master is None:
            raise ConnectionError('The network is not open')
        if not 1 <= position <= len(self._master.slaves):
            raise ValueError(f'There is no slave {position}')
        return self._master.slaves[position - 1]

    def scan(self) -> List[EtherCatSlave]:
        if self._master is None:
            raise ConnectionError('The network is not open')
        self._mapped = False
        if self._master.config_init() <= 0:
            return []
        slaves = []
        for number, slave in enumerate(self._master.slaves, start=1):
            slave.add_emergency_callback(lambda emcy, position=number: self._on_emergency(position, emcy))
            slaves.append(EtherCatSlave(number, slave.name, slave.man, slave.id, slave.rev))
        self._master.read_state()
        for item, slave in zip(slaves, self._master.slaves, strict=True):
            item.state, item.al_status = slave.state, slave.al_status
        return slaves

    def _on_emergency(self, position: int, emcy) -> None:
        data = bytes([emcy.b1]) + emcy.w1.to_bytes(2, 'little') + emcy.w2.to_bytes(2, 'little')
        self._emergency(CoeEmergency(time.time(), position, emcy.error_code, emcy.error_reg, data))

    def read_states(self) -> List[Tuple[int, int]]:
        if self._master is None:
            return []
        self._master.read_state()
        return [(slave.state, slave.al_status) for slave in self._master.slaves]

    def request_state(self, position: int, state: int) -> None:
        if self._master is None:
            raise ConnectionError('The network is not open')
        if state == STATE_SAFEOP and not self._mapped:
            self._master.config_map()  # Safe-Op needs the process data sync managers and FMMUs configured
            self._mapped = True
        targets = self._master.slaves if position == 0 else [self._slave(position)]
        for slave in targets:
            slave.state = state
            slave.write_state()
        for slave in targets:
            slave.state_check(state, STATE_TIMEOUT_US)
        self._master.read_state()

    def _mailbox(self, position: int, call):
        pysoem = self._pysoem
        try:
            return call(self._slave(position))
        except pysoem.SdoError as e:
            raise SdoAbort(e.abort_code) from None
        except pysoem.WkcError:
            raise ConnectionError(f'Slave {position} did not answer (no CoE mailbox, or not in Pre-Op)') from None
        except pysoem.MailboxError as e:
            raise ConnectionError(f'Mailbox error of slave {position}: {e.desc}') from None
        except pysoem.PacketError as e:
            raise ConnectionError(f'Mailbox packet error {e.error_code} of slave {position}') from None

    def sdo_read(self, position: int, index: int, subindex: int) -> bytes:
        return bytes(self._mailbox(position, lambda slave: slave.sdo_read(index, subindex)))

    def sdo_write(self, position: int, index: int, subindex: int, data: bytes) -> None:
        self._mailbox(position, lambda slave: slave.sdo_write(index, subindex, bytes(data)))

    def object_dictionary(self, position: int) -> ObjectDictionary:
        try:
            objects = self._mailbox(position, lambda slave: slave.od)
        except self._pysoem.SdoInfoError:
            raise ConnectionError(
                f'Slave {position} has no SDO information service: load its EDS file instead'
            ) from None
        od = ObjectDictionary()
        for obj in objects:
            item = OdObject(obj.index, obj.name or f'Object 0x{obj.index:04X}', obj.object_code)
            if obj.object_code == 7 or not obj.entries:
                item.entries[0] = OdEntry(
                    obj.index,
                    0,
                    item.name,
                    obj.data_type,
                    access_text(obj.obj_access),
                    pdo_mappable=bool(obj.obj_access & ACCESS_MAPPABLE),
                )
            else:
                for subindex, entry in enumerate(obj.entries):
                    if not entry.data_type and not entry.bit_length:  # A gap in the sub-indices
                        continue
                    item.entries[subindex] = OdEntry(
                        obj.index,
                        subindex,
                        entry.name or f'Sub {subindex}',
                        entry.data_type,
                        access_text(entry.obj_access),
                        pdo_mappable=bool(entry.obj_access & ACCESS_MAPPABLE),
                        parent=item.name,
                    )
            od.objects[obj.index] = item
        return od


# A simulated network


def _record(index: int, name: str, entries: List[Tuple[str, int, str, str, bool]]) -> OdObject:
    """A RECORD or ARRAY: sub-index 0 holds the number of entries; entries are (name, type, access, value, mappable)."""
    obj = OdObject(index, name, 9)
    obj.entries[0] = OdEntry(index, 0, 'Number of entries', 0x05, 'ro', str(len(entries)), parent=name)
    for subindex, (sub_name, data_type, access, value, mappable) in enumerate(entries, start=1):
        obj.entries[subindex] = OdEntry(index, subindex, sub_name, data_type, access, value, '', mappable, parent=name)
    return obj


def _var(index: int, name: str, data_type: int, access: str, value: str) -> OdObject:
    obj = OdObject(index, name, 7)
    obj.entries[0] = OdEntry(index, 0, name, data_type, access, value)
    return obj


def _mapped(index: int, subindices: int, bits: int) -> List[Tuple[str, int, str, str, bool]]:
    """The entries of a PDO mapping object: index, sub-index and length of each mapped object."""
    return [
        (f'Subindex {sub:03}', 0x07, 'ro', f'0x{(index << 16) | (sub << 8) | bits:08X}', False)
        for sub in range(1, subindices + 1)
    ]


def _assigned(mapping_index: Optional[int]) -> List[Tuple[str, int, str, str, bool]]:
    return [('Subindex 001', 0x06, 'rw', f'0x{mapping_index:04X}', False)] if mapping_index else []


def simulated_dictionary(name: str, product: int, inputs: int, outputs: int) -> ObjectDictionary:
    """The object dictionary of a simulated I/O terminal with 16 bit analogue channels."""
    od = ObjectDictionary()
    od.device_info = {'VendorName': 'CANExplorer', 'ProductName': name}
    for obj in (
        _var(0x1000, 'Device type', 0x07, 'ro', '0x00001389'),
        _var(0x1008, 'Device name', 0x09, 'ro', name),
        _var(0x1009, 'Hardware version', 0x09, 'ro', '01'),
        _var(0x100A, 'Software version', 0x09, 'ro', '1.0.0'),
        _record(
            0x1018,
            'Identity',
            [
                ('Vendor ID', 0x07, 'ro', '0x00000CAE', False),
                ('Product code', 0x07, 'ro', f'0x{product:08X}', False),
                ('Revision', 0x07, 'ro', '0x00010000', False),
                ('Serial number', 0x07, 'ro', '0x00000000', False),
            ],
        ),
    ):
        od.objects[obj.index] = obj
    if outputs:
        od.objects[0x1600] = _record(0x1600, 'RxPDO mapping', _mapped(0x7000, outputs, 16))
        od.objects[0x7000] = _record(
            0x7000, 'Outputs', [(f'Output {i}', 0x03, 'rw', '0', True) for i in range(1, outputs + 1)]
        )
    if inputs:
        od.objects[0x1A00] = _record(0x1A00, 'TxPDO mapping', _mapped(0x6000, inputs, 16))
        od.objects[0x6000] = _record(
            0x6000, 'Inputs', [(f'Input {i}', 0x03, 'ro', str(1000 * i), True) for i in range(1, inputs + 1)]
        )
    od.objects[0x1C12] = _record(0x1C12, 'RxPDO assign', _assigned(0x1600 if outputs else None))
    od.objects[0x1C13] = _record(0x1C13, 'TxPDO assign', _assigned(0x1A00 if inputs else None))
    od.objects[0x8000] = _record(
        0x8000,
        'Settings',
        [('Filter', 0x01, 'rw', '1', False), ('Filter time ms', 0x06, 'rw', '10', False)],
    )
    return od


class SimulatedBackend(EtherCatBackend):
    """A network of a coupler without mailbox and two I/O terminals, for trying the master without hardware."""

    def __init__(self):
        self._open = False
        self._slaves: List[EtherCatSlave] = []
        self._dictionaries: Dict[int, ObjectDictionary] = {}
        self._values: Dict[Tuple[int, int, int], bytes] = {}

    def adapters(self) -> List[Tuple[str, str]]:
        return [(SIMULATED, 'Simulated network')]

    def open(self, adapter: str) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def scan(self) -> List[EtherCatSlave]:
        if not self._open:
            raise ConnectionError('The network is not open')
        self._slaves = [
            EtherCatSlave(1, 'SIM-Coupler', 0xCAE, 0x0001, 0x00010000, STATE_PREOP, coe=False),
            EtherCatSlave(2, 'SIM-AI4', 0xCAE, 0x0002, 0x00010000, STATE_PREOP),
            EtherCatSlave(3, 'SIM-AO2', 0xCAE, 0x0003, 0x00010000, STATE_PREOP),
        ]
        self._dictionaries = {
            2: simulated_dictionary('SIM-AI4', 0x0002, inputs=4, outputs=0),
            3: simulated_dictionary('SIM-AO2', 0x0003, inputs=0, outputs=2),
        }
        self._values.clear()
        return [EtherCatSlave(**vars(slave)) for slave in self._slaves]

    def read_states(self) -> List[Tuple[int, int]]:
        return [(slave.state, slave.al_status) for slave in self._slaves]

    def request_state(self, position: int, state: int) -> None:
        if state not in REQUESTABLE_STATES:
            raise ValueError(f'The master cannot request {state_name(state)}')
        for slave in self._slaves if position == 0 else [self._slave(position)]:
            slave.state, slave.al_status = state, 0

    def _slave(self, position: int) -> EtherCatSlave:
        if not 1 <= position <= len(self._slaves):
            raise ValueError(f'There is no slave {position}')
        return self._slaves[position - 1]

    def _entry(self, position: int, index: int, subindex: int) -> OdEntry:
        slave = self._slave(position)
        if not slave.coe or slave.state == STATE_INIT:
            raise ConnectionError(f'Slave {position} did not answer (no CoE mailbox, or not in Pre-Op)')
        entry = self._dictionaries[position].entry(index, subindex)
        if entry is None:
            raise SdoAbort(0x06020000 if index not in self._dictionaries[position].objects else 0x06090011)
        return entry

    def sdo_read(self, position: int, index: int, subindex: int) -> bytes:
        entry = self._entry(position, index, subindex)
        if not entry.readable:
            raise SdoAbort(0x06010001)
        if (position, index, subindex) in self._values:
            return self._values[(position, index, subindex)]
        if index == 0x1018 and subindex == 4:
            return (0x1000 + position).to_bytes(4, 'little')  # A serial number per slave
        return encode_value(entry.data_type, entry.default)

    def sdo_write(self, position: int, index: int, subindex: int, data: bytes) -> None:
        entry = self._entry(position, index, subindex)
        if not entry.writable:
            raise SdoAbort(0x06010002)
        try:
            decode_value(entry.data_type, data)
        except ValueError:
            raise SdoAbort(0x06070010) from None
        self._values[(position, index, subindex)] = bytes(data)
        if index == 0x8000 and subindex == 2 and int.from_bytes(data, 'little') > 1000:
            # Out of range for the terminal: it reports an emergency (as real slaves do for invalid settings)
            self._emergency(CoeEmergency(time.time(), position, 0x6320, 0x01, bytes(5)))

    def object_dictionary(self, position: int) -> ObjectDictionary:
        self._entry(position, 0x1000, 0)  # Fails as a real slave without a mailbox does
        source = self._dictionaries[position]
        od = ObjectDictionary()
        od.device_info = dict(source.device_info)
        od.objects = {index: obj for index, obj in source.objects.items()}
        return od


# The master


class EtherCatMaster:
    """Asynchronous access to an EtherCAT network: the backend's blocking calls run on one worker thread."""

    MAX_EMERGENCIES = 200

    def __init__(self, backend: EtherCatBackend):
        self.backend = backend
        self.adapter = ''
        self.slaves: List[EtherCatSlave] = []
        self.emergencies: Deque[CoeEmergency] = deque(maxlen=self.MAX_EMERGENCIES)
        backend.set_emergency_handler(self.emergencies.append)  # Called on the worker thread; append is atomic
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ethercat')

    @property
    def is_open(self) -> bool:
        return bool(self.adapter)

    async def _call(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    async def adapters(self) -> List[Tuple[str, str]]:
        return await self._call(self.backend.adapters)

    async def open(self, adapter: str) -> List[EtherCatSlave]:
        """Open the adapter and find the slaves."""
        await self._call(self.backend.open, adapter)
        self.adapter = adapter
        try:
            return await self.scan()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self.adapter:
            self.adapter = ''
            self.slaves = []
            await self._call(self.backend.close)

    def shutdown(self) -> None:
        """Close the network and stop the worker thread (without an event loop, e.g. when quitting)."""
        if self.adapter:
            self.adapter = ''
            self.slaves = []
            self._executor.submit(self.backend.close)
        self._executor.shutdown(wait=True)

    async def scan(self) -> List[EtherCatSlave]:
        self.slaves = await self._call(self.backend.scan)
        return self.slaves

    async def refresh_states(self) -> bool:
        """Read the slaves' states; True when one changed."""
        states = await self._call(self.backend.read_states)
        changed = False
        # Not strict: a poll may finish after a close or scan changed the slaves
        for slave, (state, al_status) in zip(self.slaves, states, strict=False):
            if (slave.state, slave.al_status) != (state, al_status):
                slave.state, slave.al_status = state, al_status
                changed = True
        return changed

    async def request_state(self, position: int, state: int) -> None:
        """Bring one slave (or all with position 0) to a state; fails with the AL status of slaves that refused."""
        if state not in REQUESTABLE_STATES:
            raise ValueError(f'The master cannot request {state_name(state)}')
        await self._call(self.backend.request_state, position, state)
        await self.refresh_states()
        refused = [
            slave
            for slave in self.slaves
            if (position in (0, slave.position)) and (slave.state & 0x0F != state or slave.error)
        ]
        if refused:
            raise ConnectionError(
                '; '.join(
                    f'Slave {slave.position} is in {slave.state_name}'
                    + (f': {al_status_text(slave.al_status)}' if slave.al_status else '')
                    for slave in refused
                )
            )

    async def sdo_read(self, position: int, index: int, subindex: int) -> bytes:
        return await self._call(self.backend.sdo_read, position, index, subindex)

    async def sdo_write(self, position: int, index: int, subindex: int, data: bytes) -> None:
        await self._call(self.backend.sdo_write, position, index, subindex, bytes(data))

    async def object_dictionary(self, position: int) -> ObjectDictionary:
        return await self._call(self.backend.object_dictionary, position)

    async def pdo_assignment(self, position: int, od: Optional[ObjectDictionary] = None) -> List[CoePdo]:
        """The PDOs assigned to the process data sync managers and their mapping, read from the slave."""
        pdos = []
        for kind, assign_index in PDO_ASSIGNMENTS:
            try:
                count = (await self.sdo_read(position, assign_index, 0))[0]
            except SdoAbort:
                continue  # No sync manager of this direction
            for subindex in range(1, count + 1):
                mapping_index = int.from_bytes(await self.sdo_read(position, assign_index, subindex), 'little')
                pdo = CoePdo(kind, mapping_index)
                entries = (await self.sdo_read(position, mapping_index, 0))[0]
                for entry in range(1, entries + 1):
                    mapping = int.from_bytes(await self.sdo_read(position, mapping_index, entry), 'little')
                    index, sub, bits = mapping >> 16, (mapping >> 8) & 0xFF, mapping & 0xFF
                    od_entry = od.entry(index, sub) if od is not None else None
                    if index == 0:
                        name = 'Gap'
                    elif od is not None:
                        name = od.name(index, sub)
                    else:
                        name = STANDARD_OBJECTS.get(index, f'0x{index:04X}') + (f' sub {sub}' if sub else '')
                    pdo.variables.append(
                        PdoVariable(index, sub, bits, name, od_entry.data_type if od_entry is not None else 0x07)
                    )
                pdos.append(pdo)
        return pdos
