"""CCP, the CAN Calibration Protocol (ASAM MCD-1 CCP 2.1): the master for reading and writing an ECU's memory by
address, seed & key, calibration pages, checksums and DAQ measurement, and a simulated slave.

CCP is a command/response protocol like XCP but older: the master sends a Command Receive Object (CRO) with a
command code and a rolling command counter, and the slave answers with a Command Return Message (CRM, packet id
0xFF) that repeats the counter. Event messages (0xFE) and DAQ data (DTOs, packet id = ODT number) share the
slave's identifier and are routed away from the responses; a response with another counter (a late answer) is
skipped.

Multi-byte parameters are in the ECU's byte order (the `byte_order` option), except the station address, which is
always Intel. The checksum algorithm of BUILD_CHKSUM is ECU specific, so the checksum is reported, not verified.
An ODT element number of SET_DAQ_PTR is used as the byte position in the ODT (0-6), with elements of 1, 2 or 4 bytes.
"""

import asyncio
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import can

from can_explorer.core.can_command import CanCommandClient, CanCommandTimeout
from can_explorer.core.session import BusSession
from can_explorer.core.xcp import DaqSample, DaqSignal, pack_odts

# Command codes
CONNECT = 0x01
SET_MTA = 0x02
DNLOAD = 0x03
UPLOAD = 0x04
TEST = 0x05
START_STOP = 0x06
DISCONNECT = 0x07
START_STOP_ALL = 0x08
GET_ACTIVE_CAL_PAGE = 0x09
SET_S_STATUS = 0x0C
GET_S_STATUS = 0x0D
BUILD_CHKSUM = 0x0E
SHORT_UP = 0x0F
CLEAR_MEMORY = 0x10
SELECT_CAL_PAGE = 0x11
GET_SEED = 0x12
UNLOCK = 0x13
GET_DAQ_SIZE = 0x14
SET_DAQ_PTR = 0x15
WRITE_DAQ = 0x16
EXCHANGE_ID = 0x17
PROGRAM = 0x18
MOVE = 0x19
GET_CCP_VERSION = 0x1B
DIAG_SERVICE = 0x20
ACTION_SERVICE = 0x21
PROGRAM_6 = 0x22
DNLOAD_6 = 0x23

COMMAND_NAMES = {
    value: name
    for name, value in dict(globals()).items()
    if name.isupper() and isinstance(value, int) and value <= 0x23 and not name.startswith('_')
}

CRM = 0xFF  # Command return message packet id
EVENT = 0xFE  # Event message packet id
ACKNOWLEDGE = 0x00
MAX_DATA = 5  # Data bytes that fit in a CRM after the header
ODT_SIZE = 7  # Data bytes of a DTO

# Resources (GET_SEED, UNLOCK privilege status, EXCHANGE_ID)
RESOURCE_CAL = 0x01
RESOURCE_DAQ = 0x02
RESOURCE_PGM = 0x40
RESOURCES = {RESOURCE_CAL: 'CAL', RESOURCE_DAQ: 'DAQ', RESOURCE_PGM: 'PGM'}

BUSY = (0x10, 0x11)
ACCESS_LOCKED = 0x35

RETURN_CODES = {
    0x00: 'acknowledge',
    0x01: 'DAQ processor overload',
    0x10: 'command processor busy',
    0x11: 'DAQ processor busy',
    0x12: 'internal timeout',
    0x18: 'key request',
    0x19: 'session status request',
    0x20: 'cold start request',
    0x21: 'cal data init request',
    0x22: 'DAQ list init request',
    0x23: 'code update request',
    0x30: 'command unknown',
    0x31: 'command syntax',
    0x32: 'parameter out of range',
    0x33: 'access denied',
    0x34: 'overload',
    0x35: 'access locked (seed & key required)',
    0x36: 'resource/function not available',
}

ComputeKey = Callable[[int, bytes], bytes]


class CcpError(Exception):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


@dataclass
class CcpEvent:
    code: int
    data: bytes
    timestamp: float

    @property
    def name(self) -> str:
        return RETURN_CODES.get(self.code, f'0x{self.code:02X}')


class CcpClient:
    """CCP master over a CAN command transport. `read`/`write` access the slave's memory by address."""

    def __init__(
        self,
        transport: CanCommandClient,
        station_address: int = 0,
        byte_order: str = 'big',
        compute_key: Optional[ComputeKey] = None,
        retries: int = 2,
        busy_retries: int = 20,
        busy_delay: float = 0.01,
    ):
        self.transport = transport
        self.station_address = station_address
        self.byte_order = byte_order  # Of addresses and multi-byte fields in the ECU
        self.compute_key = compute_key
        self.retries = retries
        self.busy_retries = busy_retries
        self.busy_delay = busy_delay
        self.connected = False
        self.address_granularity = 1
        self.privileges = 0  # Resources unlocked by UNLOCK
        self.events: List[CcpEvent] = []
        self.dto_handler: Optional[Callable[[bytes, float], None]] = None
        self._counter = 0
        self._mta: Optional[Tuple[int, int]] = None
        transport.unsolicited = self._on_packet

    def _next_counter(self) -> int:
        self._counter = (self._counter + 1) & 0xFF
        return self._counter

    def _on_packet(self, data: bytes, timestamp: float) -> bool:
        if data[0] == CRM:
            return False
        if data[0] == EVENT:
            self.events.append(CcpEvent(data[1] if len(data) > 1 else 0, bytes(data[2:]), timestamp))
            del self.events[:-100]
        elif self.dto_handler is not None:
            self.dto_handler(data, timestamp)
        return True

    async def _exchange(self, code: int, params: bytes, timeout: Optional[float]) -> bytes:
        counter = self._next_counter()
        self.transport.drain()
        self.transport.send(bytes([code, counter]) + bytes(params))
        while True:
            response = await self.transport.receive(timeout)
            if len(response) < 3:
                raise CcpError('Short CCP response')
            if response[2] == counter:
                return response
            # A late response to an earlier command: not ours

    async def _command(self, code: int, params: bytes = b'', timeout: Optional[float] = None, unlock=True) -> bytes:
        timeouts = busy = 0
        mta = self._mta if code in (UPLOAD, DNLOAD, DNLOAD_6, BUILD_CHKSUM) else None
        while True:
            try:
                response = await self._exchange(code, params, timeout)
            except CanCommandTimeout:
                timeouts += 1
                if timeouts > self.retries or code == CONNECT:
                    raise
                if mta is not None:
                    await self.set_mta(*mta)
                continue
            error = response[1]
            if error == ACKNOWLEDGE:
                return bytes(response[3:])
            if error in BUSY and busy < self.busy_retries:
                busy += 1
                await asyncio.sleep(self.busy_delay)
                continue
            if error == ACCESS_LOCKED and unlock and self.compute_key is not None:
                await self.unlock_all()
                unlock = False
                if mta is not None:
                    await self.set_mta(*mta)
                continue
            name = COMMAND_NAMES.get(code, f'0x{code:02X}')
            raise CcpError(f'CCP {name}: {RETURN_CODES.get(error, f"error 0x{error:02X}")}', error)

    def _long(self, value: int) -> bytes:
        return (value & 0xFFFFFFFF).to_bytes(4, self.byte_order)

    # Session

    async def connect(self) -> None:
        await self._command(CONNECT, self.station_address.to_bytes(2, 'little'), unlock=False)
        self.connected = True

    async def disconnect(self, permanent: bool = False) -> None:
        try:
            if self.dto_handler is not None:
                try:
                    await self._command(START_STOP_ALL, bytes([0]), unlock=False)
                except CcpError:
                    pass
                self.dto_handler = None
            await self._command(
                DISCONNECT,
                bytes([0x01 if permanent else 0x00, 0]) + self.station_address.to_bytes(2, 'little'),
                unlock=False,
            )
        finally:
            self.connected = False

    async def get_version(self, major: int = 2, minor: int = 1) -> bytes:
        return await self._command(GET_CCP_VERSION, bytes([major, minor]), unlock=False)

    async def exchange_id(self, master_id: bytes = b'') -> Dict[str, object]:
        """The slave's identification (e.g. its A2L name) and the resources it has and protects."""
        response = await self._command(EXCHANGE_ID, bytes(master_id[:6]), unlock=False)
        length = response[0]
        self._mta = None
        identification = (await self.upload(length)).decode('latin-1', 'replace') if length else ''
        return {
            'id': identification.rstrip('\x00'),
            'data_type': response[1],
            'resources': response[2],
            'protection': response[3],
        }

    async def get_session_status(self) -> int:
        return (await self._command(GET_S_STATUS))[0]

    async def set_session_status(self, status: int) -> None:
        await self._command(SET_S_STATUS, bytes([status]))

    # Seed & key

    async def unlock(self, resource: int, compute_key: Optional[ComputeKey] = None) -> int:
        """Unlock a resource (RESOURCE_CAL, _DAQ, _PGM) with seed & key; returns the privileges now granted."""
        algorithm = compute_key or self.compute_key
        response = await self._command(GET_SEED, bytes([resource]), unlock=False)
        if not response[0]:  # Not protected
            self.privileges |= resource
            return self.privileges
        if algorithm is None:
            raise CcpError(f'{RESOURCES.get(resource, resource)} is locked and no seed & key algorithm is set')
        key = bytes(algorithm(resource, bytes(response[1:5])))
        if not 0 < len(key) <= 6:
            raise CcpError(f'A CCP key has 1 to 6 bytes, the algorithm returned {len(key)}')
        response = await self._command(UNLOCK, key, unlock=False)
        self.privileges = response[0]
        if not self.privileges & resource:
            raise CcpError(f'{RESOURCES.get(resource, resource)} is still locked: the key was not accepted')
        return self.privileges

    async def unlock_all(self, compute_key: Optional[ComputeKey] = None) -> int:
        for resource in (RESOURCE_CAL, RESOURCE_DAQ):
            await self.unlock(resource, compute_key)
        return self.privileges

    # Memory

    async def set_mta(self, address: int, extension: int = 0, mta: int = 0) -> None:
        await self._command(SET_MTA, bytes([mta, extension & 0xFF]) + self._long(address))
        if mta == 0:
            self._mta = (address, extension)

    def _advance(self, size: int) -> None:
        if self._mta is not None:
            self._mta = (self._mta[0] + size, self._mta[1])

    async def upload(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = min(size - len(data), MAX_DATA)
            data += (await self._command(UPLOAD, bytes([chunk])))[:chunk]
            self._advance(chunk)
        return bytes(data[:size])

    async def short_up(self, address: int, size: int, extension: int = 0) -> bytes:
        if size <= MAX_DATA:
            response = await self._command(SHORT_UP, bytes([size, extension & 0xFF]) + self._long(address))
            return response[:size]
        await self.set_mta(address, extension)
        return await self.upload(size)

    async def dnload(self, address: int, data: bytes, extension: int = 0) -> None:
        """Write `data`: DNLOAD_6 for whole 6-byte parts, DNLOAD for the rest."""
        await self.set_mta(address, extension)
        offset = 0
        data = bytes(data)
        while offset < len(data):
            if len(data) - offset >= 6:
                await self._command(DNLOAD_6, data[offset : offset + 6])
                chunk = 6
            else:
                chunk = min(len(data) - offset, MAX_DATA)
                await self._command(DNLOAD, bytes([chunk]) + data[offset : offset + chunk])
            self._advance(chunk)
            offset += chunk

    # The common calibration interface (shared with XcpClient)

    async def read(self, address: int, extension: int, size: int) -> bytes:
        return await self.short_up(address, size, extension)

    async def write(self, address: int, extension: int, data: bytes) -> None:
        await self.dnload(address, data, extension)

    # Checksum and pages

    async def build_checksum(self, address: int, size: int, extension: int = 0) -> bytes:
        """The slave's checksum over `size` bytes at `address` (its algorithm is ECU specific)."""
        await self.set_mta(address, extension)
        response = await self._command(BUILD_CHKSUM, self._long(size))
        return bytes(response[1 : 1 + min(response[0], 4)])

    async def select_cal_page(self, address: int, extension: int = 0) -> None:
        """Activate the calibration page that starts at `address`."""
        await self.set_mta(address, extension)
        await self._command(SELECT_CAL_PAGE)

    async def get_active_cal_page(self) -> Tuple[int, int]:
        """(address extension, address) of the active calibration page."""
        response = await self._command(GET_ACTIVE_CAL_PAGE)
        return response[0], int.from_bytes(response[1:5], self.byte_order)

    # DAQ

    async def get_daq_size(self, daq_list: int, dto_id: int = 0) -> Tuple[int, int]:
        """(number of ODTs, first PID) of a DAQ list, which is cleared."""
        response = await self._command(GET_DAQ_SIZE, bytes([daq_list, 0]) + self._long(dto_id))
        return response[0], response[1]

    async def set_daq_ptr(self, daq_list: int, odt: int, element: int) -> None:
        await self._command(SET_DAQ_PTR, bytes([daq_list, odt, element]))

    async def write_daq(self, address: int, size: int, extension: int = 0) -> None:
        await self._command(WRITE_DAQ, bytes([size, extension & 0xFF]) + self._long(address))

    async def start_stop(self, mode: int, daq_list: int, last_odt: int, event: int, prescaler: int = 1) -> None:
        """Mode 0 stops, 1 starts, 2 prepares the DAQ list for START_STOP_ALL."""
        params = bytes([mode, daq_list, last_odt, event]) + (prescaler & 0xFFFF).to_bytes(2, self.byte_order)
        await self._command(START_STOP, params)

    async def start_stop_all(self, start: bool) -> None:
        await self._command(START_STOP_ALL, bytes([1 if start else 0]))

    async def start_measurement(
        self,
        signals: Sequence[DaqSignal],
        on_sample: Callable[[DaqSample], None],
        prescaler: int = 1,
        timestamps: bool = True,
    ) -> 'CcpMeasurement':
        measurement = CcpMeasurement(self, signals, on_sample, prescaler)
        await measurement.start()
        return measurement


def _ccp_elements(odts):
    """Split ODT entries into CCP elements of 1, 2 or 4 bytes."""
    result = []
    for odt in odts:
        entries = []
        for entry in odt:
            offset = 0
            while offset < entry.size:
                size = next(s for s in (4, 2, 1) if s <= entry.size - offset)
                entries.append(
                    type(entry)(entry.key, entry.offset + offset, entry.address + offset, entry.extension, size)
                )
                offset += size
        result.append(entries)
    return result


@dataclass
class _CcpList:
    number: int
    event: int
    odts: list
    sizes: Dict[str, int]
    first_pid: int = 0
    buffer: Optional[Dict[str, bytearray]] = None
    next_odt: int = 0


class CcpMeasurement:
    """CCP DAQ: one DAQ list per event channel (lists numbered from 0), ODTs of 7 bytes."""

    def __init__(self, client: CcpClient, signals, on_sample, prescaler: int = 1):
        self.client = client
        self.signals = list(signals)
        self.on_sample = on_sample
        self.prescaler = max(1, prescaler)
        self.lists: List[_CcpList] = []
        self.running = False
        self.samples = 0
        self.lost = 0

    async def start(self) -> None:
        client = self.client
        if not self.signals:
            raise CcpError('Nothing to measure')
        by_event: Dict[int, list] = {}
        for signal in self.signals:
            by_event.setdefault(signal.event, []).append(signal)
        self.lists = []
        for number, (event, signals) in enumerate(sorted(by_event.items())):
            odts = _ccp_elements(
                pack_odts([(s.key, s.address, s.extension, s.size) for s in signals], ODT_SIZE, ODT_SIZE, 4)
            )
            available, first_pid = await client.get_daq_size(number, client.transport.response_id)
            if len(odts) > available:
                raise CcpError(f'DAQ list {number} has {available} ODTs, {len(odts)} are needed')
            self.lists.append(_CcpList(number, event, odts, {s.key: s.size for s in signals}, first_pid))
        for daq in self.lists:
            for odt, entries in enumerate(daq.odts):
                position = 0
                for entry in entries:
                    await client.set_daq_ptr(daq.number, odt, position)
                    await client.write_daq(entry.address, entry.size, entry.extension)
                    position += entry.size
        client.dto_handler = self._on_dto
        for daq in self.lists:
            await client.start_stop(2, daq.number, len(daq.odts) - 1, daq.event, self.prescaler)
        await client.start_stop_all(True)
        self.running = True

    async def stop(self) -> None:
        self.running = False
        try:
            await self.client.start_stop_all(False)
        finally:
            if self.client.dto_handler == self._on_dto:
                self.client.dto_handler = None

    def _on_dto(self, data: bytes, timestamp: float) -> None:
        pid = data[0]
        daq = next((d for d in self.lists if d.first_pid <= pid < d.first_pid + len(d.odts)), None)
        if daq is None:
            return
        odt = pid - daq.first_pid
        if odt == 0:
            if daq.buffer is not None and daq.next_odt:
                self.lost += 1
            daq.buffer = {key: bytearray(size) for key, size in daq.sizes.items()}
            daq.next_odt = 0
        if daq.buffer is None or odt != daq.next_odt:
            daq.buffer, daq.next_odt = None, 0
            self.lost += 1
            return
        position = 1
        for entry in daq.odts[odt]:
            daq.buffer[entry.key][entry.offset : entry.offset + entry.size] = data[position : position + entry.size]
            position += entry.size
        daq.next_odt += 1
        if daq.next_odt == len(daq.odts):
            values = {key: bytes(value) for key, value in daq.buffer.items()}
            daq.buffer, daq.next_odt = None, 0
            self.samples += 1
            self.on_sample(DaqSample(daq.event, timestamp, values))


# Simulated slave


def demo_key(resource: int, seed: bytes) -> bytes:
    """The simulator's demonstration seed & key algorithm: every seed byte XOR 0xFF."""
    return bytes(b ^ 0xFF for b in seed)


REFERENCE_PAGE_ADDRESS = 0x00800000  # The simulator's reference (flash) page; the working page is at 0


@dataclass
class _SimList:
    odts: List[List[Tuple[int, int, int]]] = field(default_factory=list)  # [(position, address, size)]
    event: int = 0
    prescaler: int = 1
    last_odt: int = 0
    prepared: bool = False
    running: bool = False
    counter: int = 0


class CcpSlaveSimulator:
    """A simulated CCP slave on a session, backed by a byte array addressed from 0: memory access, seed & key,
    two calibration pages, BUILD_CHKSUM (CRC-32), EXCHANGE_ID and DAQ (4 lists of 8 ODTs, first PIDs 0, 8, 16, 24)
    with cyclic event channels, plus error injection (`busy_count`, `drop_count`)."""

    DAQ_LISTS = 4
    ODTS_PER_LIST = 8

    def __init__(
        self,
        session: BusSession,
        request_id: int = 0x7E0,
        response_id: int = 0x7E1,
        extended: bool = False,
        station_address: int = 0x0001,
        byte_order: str = 'big',
        memory: Optional[bytes] = None,
        size: int = 0x10000,
        protected: int = 0,
        seed: bytes = bytes([0x12, 0x34, 0x56, 0x78]),
        identification: str = 'example',
        event_periods: Optional[Dict[int, float]] = None,
        auto_events: bool = True,
        compute_key: ComputeKey = demo_key,
    ):
        self.session = session
        self.request_id = request_id
        self.response_id = response_id
        self.extended = extended
        self.station_address = station_address
        self.byte_order = byte_order
        working = bytearray(memory if memory is not None else bytes(size))
        self.pages = [working, bytearray(working)]
        self.page = 0
        self.protected = protected
        self.locked = protected
        self.seed = bytes(seed)
        self.compute_key = compute_key
        self.identification = identification
        self.event_periods = event_periods if event_periods is not None else {0: 0.01, 1: 0.1}
        self.auto_events = auto_events
        self.connected = False
        self.mta = 0
        self.mta1 = 0
        self.session_status = 0
        self.commands: List[bytes] = []
        self.busy_count = 0
        self.drop_count = 0
        self.daq_lists = [_SimList() for _ in range(self.DAQ_LISTS)]
        self.daq_pointer: Optional[Tuple[int, int, int]] = None
        self._seed_resource = 0
        self._pending_upload: Optional[bytes] = None
        self._tasks: List[asyncio.Task] = []
        session.add_message_listener(self._on_message)

    @property
    def memory(self) -> bytearray:
        """The working (RAM) page."""
        return self.pages[0]

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        self.session.remove_message_listener(self._on_message)

    def _send(self, data: bytes) -> None:
        self.session.send(
            can.Message(arbitration_id=self.response_id, is_extended_id=self.extended, data=bytes(data).ljust(8, b'\0'))
        )

    def _crm(self, counter: int, data: bytes = b'', code: int = ACKNOWLEDGE) -> None:
        self._send(bytes([CRM, code, counter]) + bytes(data))

    def _long(self, data: bytes) -> int:
        return int.from_bytes(data, self.byte_order)

    def _on_message(self, msg: can.Message) -> None:
        if (
            not msg.is_rx
            or msg.arbitration_id != self.request_id
            or msg.is_extended_id != self.extended
            or len(msg.data) < 2
        ):
            return
        data = bytes(msg.data)
        self.commands.append(data)
        command, counter = data[0], data[1]
        if self.drop_count:
            self.drop_count -= 1
            return
        if command == CONNECT:
            if int.from_bytes(data[2:4], 'little') == self.station_address:
                self.connected = True
                self.locked = self.protected
                self._crm(counter)
            return  # A CONNECT to another station address is ignored (shared bus)
        if not self.connected:
            return
        if self.busy_count:
            self.busy_count -= 1
            self._crm(counter, code=0x10)
            return
        if command in (DNLOAD, DNLOAD_6, SELECT_CAL_PAGE) and self.locked & RESOURCE_CAL:
            self._crm(counter, code=ACCESS_LOCKED)
            return
        if command in (GET_DAQ_SIZE, SET_DAQ_PTR, WRITE_DAQ, START_STOP, START_STOP_ALL) and self.locked & RESOURCE_DAQ:
            self._crm(counter, code=ACCESS_LOCKED)
            return
        handler = getattr(self, f'_cmd_{command:02X}', None)
        if handler is None:
            self._crm(counter, code=0x30)  # Command unknown
        else:
            handler(counter, data)

    def _page(self) -> bytearray:
        return self.pages[self.page]

    def _cmd_07(self, counter: int, data: bytes) -> None:  # DISCONNECT
        self._stop_daq()
        self.connected = False
        self._crm(counter)

    def _cmd_1B(self, counter: int, data: bytes) -> None:  # GET_CCP_VERSION
        self._crm(counter, bytes([2, 1]))

    def _cmd_17(self, counter: int, data: bytes) -> None:  # EXCHANGE_ID
        text = self.identification.encode('latin-1')
        self._pending_upload = text
        self._crm(counter, bytes([len(text), 0, RESOURCE_CAL | RESOURCE_DAQ, self.locked]))

    def _cmd_0D(self, counter: int, data: bytes) -> None:  # GET_S_STATUS
        self._crm(counter, bytes([self.session_status, 0]))

    def _cmd_0C(self, counter: int, data: bytes) -> None:  # SET_S_STATUS
        self.session_status = data[2]
        self._crm(counter)

    def _cmd_12(self, counter: int, data: bytes) -> None:  # GET_SEED
        self._seed_resource = data[2]
        protected = bool(self.locked & data[2])
        self._crm(counter, bytes([1 if protected else 0]) + (self.seed if protected else bytes(4)))

    def _cmd_13(self, counter: int, data: bytes) -> None:  # UNLOCK
        key = bytes(self.compute_key(self._seed_resource, self.seed))
        if data[2 : 2 + len(key)] == key:
            self.locked &= ~self._seed_resource
            self._crm(counter, bytes([(RESOURCE_CAL | RESOURCE_DAQ) & ~self.locked]))
        else:
            self._crm(counter, code=ACCESS_LOCKED)

    def _cmd_02(self, counter: int, data: bytes) -> None:  # SET_MTA
        address = self._long(data[4:8])
        if data[2] == 1:
            self.mta1 = address
        else:
            self.mta = address
            self._pending_upload = None
        self._crm(counter)

    def _upload(self, counter: int, address: int, size: int) -> bool:
        if self._pending_upload is not None:
            text, self._pending_upload = self._pending_upload[:size], self._pending_upload[size:] or None
            self._crm(counter, text)
            return True
        if address + size > len(self.memory):
            self._crm(counter, code=0x32)
            return False
        self._crm(counter, bytes(self._page()[address : address + size]))
        return True

    def _cmd_04(self, counter: int, data: bytes) -> None:  # UPLOAD
        size = data[2]
        if self._upload(counter, self.mta, size):
            self.mta += size

    def _cmd_0F(self, counter: int, data: bytes) -> None:  # SHORT_UP
        size, address = data[2], self._long(data[4:8])
        self._pending_upload = None
        if self._upload(counter, address, size):
            self.mta = address + size

    def _store(self, counter: int, payload: bytes) -> None:
        if self.page != 0:
            self._crm(counter, code=0x33)  # The reference page is read-only
            return
        if self.mta + len(payload) > len(self.memory):
            self._crm(counter, code=0x32)
            return
        self.memory[self.mta : self.mta + len(payload)] = payload
        self.mta += len(payload)
        self._crm(counter, bytes([0]) + self.mta.to_bytes(4, self.byte_order))

    def _cmd_03(self, counter: int, data: bytes) -> None:  # DNLOAD
        self._store(counter, data[3 : 3 + data[2]])

    def _cmd_23(self, counter: int, data: bytes) -> None:  # DNLOAD_6
        self._store(counter, data[2:8])

    def _cmd_0E(self, counter: int, data: bytes) -> None:  # BUILD_CHKSUM
        size = self._long(data[2:6])
        if self.mta + size > len(self.memory):
            self._crm(counter, code=0x32)
            return
        value = zlib.crc32(bytes(self._page()[self.mta : self.mta + size])) & 0xFFFFFFFF
        self._crm(counter, bytes([4]) + value.to_bytes(4, self.byte_order))

    def _cmd_11(self, counter: int, data: bytes) -> None:  # SELECT_CAL_PAGE
        if self.mta not in (0, REFERENCE_PAGE_ADDRESS):
            self._crm(counter, code=0x32)
            return
        self.page = 0 if self.mta == 0 else 1
        self._crm(counter)

    def _cmd_09(self, counter: int, data: bytes) -> None:  # GET_ACTIVE_CAL_PAGE
        address = 0 if self.page == 0 else REFERENCE_PAGE_ADDRESS
        self._crm(counter, bytes([0]) + address.to_bytes(4, self.byte_order))

    # DAQ

    def _cmd_14(self, counter: int, data: bytes) -> None:  # GET_DAQ_SIZE
        number = data[2]
        if number >= self.DAQ_LISTS:
            self._crm(counter, code=0x32)
            return
        self.daq_lists[number] = _SimList(odts=[[] for _ in range(self.ODTS_PER_LIST)])
        self._crm(counter, bytes([self.ODTS_PER_LIST, number * self.ODTS_PER_LIST]))

    def _cmd_15(self, counter: int, data: bytes) -> None:  # SET_DAQ_PTR
        number, odt, element = data[2], data[3], data[4]
        if number >= self.DAQ_LISTS or odt >= self.ODTS_PER_LIST or element >= ODT_SIZE:
            self._crm(counter, code=0x32)
            return
        self.daq_pointer = (number, odt, element)
        self._crm(counter)

    def _cmd_16(self, counter: int, data: bytes) -> None:  # WRITE_DAQ
        if self.daq_pointer is None or data[2] not in (1, 2, 4):
            self._crm(counter, code=0x32)
            return
        number, odt, element = self.daq_pointer
        if element + data[2] > ODT_SIZE:
            self._crm(counter, code=0x32)
            return
        self.daq_lists[number].odts[odt].append((element, self._long(data[4:8]), data[2]))
        self._crm(counter)

    def _cmd_06(self, counter: int, data: bytes) -> None:  # START_STOP
        mode, number, last_odt, event = data[2], data[3], data[4], data[5]
        if number >= self.DAQ_LISTS:
            self._crm(counter, code=0x32)
            return
        daq = self.daq_lists[number]
        daq.last_odt, daq.event = last_odt, event
        daq.prescaler = max(1, int.from_bytes(data[6:8], self.byte_order))
        if mode == 2:
            daq.prepared = True
        else:
            daq.running = mode == 1
            self._schedule()
        self._crm(counter)

    def _cmd_08(self, counter: int, data: bytes) -> None:  # START_STOP_ALL
        for daq in self.daq_lists:
            if data[2] == 1:
                if daq.prepared:
                    daq.running, daq.prepared = True, False
            else:
                daq.running = False
        self._schedule()
        self._crm(counter)

    def _stop_daq(self) -> None:
        for daq in self.daq_lists:
            daq.running = False
        self._schedule()

    def _schedule(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if not self.auto_events:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for channel in {daq.event for daq in self.daq_lists if daq.running}:
            period = self.event_periods.get(channel)
            if period:
                self._tasks.append(loop.create_task(self._run_event(channel, period)))

    async def _run_event(self, channel: int, period: float) -> None:
        while True:
            await asyncio.sleep(period)
            self.trigger_event(channel)

    def trigger_event(self, channel: int) -> None:
        """Sample the running DAQ lists of an event channel and send their DTOs."""
        for number, daq in enumerate(self.daq_lists):
            if not daq.running or daq.event != channel:
                continue
            daq.counter += 1
            if daq.counter % daq.prescaler:
                continue
            for odt in range(daq.last_odt + 1):
                payload = bytearray(ODT_SIZE)
                for position, address, size in daq.odts[odt]:
                    payload[position : position + size] = self.memory[address : address + size]
                self._send(bytes([number * self.ODTS_PER_LIST + odt]) + bytes(payload))
