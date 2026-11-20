"""A simulated XCP slave backed by a byte array, for trying the XCP page and for tests: memory access (byte, word
or dword address granularity), seed & key, two calibration pages, BUILD_CHECKSUM, GET_ID and dynamic DAQ with
cyclic event channels, plus error injection (ERR_CMD_BUSY, lost responses).

`XcpSlave` is the protocol engine; `XcpSlaveSimulator` puts it on a CAN session and `XcpEthernetSlave`
(`core/xcp_ethernet.py`) on UDP or TCP.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import can

from can_explorer.core.session import BusSession
from can_explorer.core.xcp_protocol import (
    ALLOC_DAQ,
    ALLOC_ODT,
    ALLOC_ODT_ENTRY,
    CHECKSUM_CRC_32,
    CONNECT,
    COPY_CAL_PAGE,
    DAQ_DYNAMIC,
    DAQ_MODE_TIMESTAMP,
    DAQ_PRESCALER_SUPPORTED,
    DAQ_TIMESTAMP_SUPPORTED,
    DOWNLOAD,
    DOWNLOAD_NEXT,
    ERR,
    ERR_ACCESS_LOCKED,
    ERR_CMD_BUSY,
    ERR_CMD_SYNCH,
    ERR_CMD_UNKNOWN,
    ERR_DAQ_CONFIG,
    ERR_MEMORY_OVERFLOW,
    ERR_OUT_OF_RANGE,
    ERR_PAGE_NOT_VALID,
    ERR_SEQUENCE,
    ERR_WRITE_PROTECTED,
    FREE_DAQ,
    ID_FIELD_ABSOLUTE,
    ID_FIELD_RELATIVE_BYTE,
    ID_FIELD_RELATIVE_WORD,
    PAGE_ECU,
    PAGE_XCP,
    RES,
    RESOURCE_CALPAG,
    RESOURCE_DAQ,
    SET_CAL_PAGE,
    SET_DAQ_LIST_MODE,
    SET_DAQ_PTR,
    SET_SEGMENT_MODE,
    START_STOP_DAQ_LIST,
    START_STOP_SYNCH,
    SYNCH,
    WRITE_DAQ,
    checksum,
)

# Commands that need the CAL/PAG or DAQ resource unlocked
CALPAG_COMMANDS = {DOWNLOAD, DOWNLOAD_NEXT, SET_CAL_PAGE, COPY_CAL_PAGE, SET_SEGMENT_MODE}
DAQ_COMMANDS = {FREE_DAQ, ALLOC_DAQ, ALLOC_ODT, ALLOC_ODT_ENTRY, SET_DAQ_PTR, WRITE_DAQ, SET_DAQ_LIST_MODE}
DAQ_COMMANDS |= {START_STOP_DAQ_LIST, START_STOP_SYNCH}

WORKING_PAGE = 0  # RAM, written by calibration
REFERENCE_PAGE = 1  # Flash, read-only


def demo_key(resource: int, seed: bytes) -> bytes:
    """The simulator's demonstration seed & key algorithm: every seed byte XOR 0xFF."""
    return bytes(b ^ 0xFF for b in seed)


@dataclass
class SimulatedEvent:
    channel: int
    name: str
    period: float  # Seconds


@dataclass
class _SimDaqList:
    odts: List[List[Tuple[int, int, int]]] = field(default_factory=list)  # [(address, extension, size bytes)]
    event: int = 0
    mode: int = 0
    prescaler: int = 1
    selected: bool = False
    running: bool = False
    first_pid: int = 0
    counter: int = 0


class XcpSlave:
    """The XCP protocol engine of a simulated slave. `send` delivers its packets; `handle` processes a command."""

    def __init__(
        self,
        byte_order: str = 'little',
        memory: Optional[bytes] = None,
        size: int = 0x10000,
        address_granularity: int = 1,
        max_cto: int = 8,
        max_dto: int = 8,
        protected: int = 0,
        seed: bytes = bytes([0x12, 0x34, 0x56, 0x78]),
        identification: str = 'example',
        checksum_type: int = CHECKSUM_CRC_32,
        identification_field: int = ID_FIELD_ABSOLUTE,
        timestamp_size: int = 4,
        events: Sequence[SimulatedEvent] = (SimulatedEvent(0, '10ms', 0.01), SimulatedEvent(1, '100ms', 0.1)),
        slave_block_mode: bool = False,
        master_block_mode: bool = False,
        auto_events: bool = True,
        compute_key: Callable[[int, bytes], bytes] = demo_key,
    ):
        self.byte_order = byte_order
        self.address_granularity = address_granularity
        self.max_cto = max_cto
        self.max_dto = max_dto
        working = bytearray(memory if memory is not None else bytes(size))
        self.pages: List[bytearray] = [working, bytearray(working)]
        self.ecu_page = WORKING_PAGE
        self.xcp_page = WORKING_PAGE
        self.protected = protected
        self.locked = protected
        self.seed = bytes(seed)
        self.compute_key = compute_key
        self.identification = identification
        self.checksum_type = checksum_type
        self.identification_field = identification_field
        self.timestamp_size = timestamp_size
        self.events = list(events)
        self.slave_block_mode = slave_block_mode
        self.master_block_mode = master_block_mode
        self.auto_events = auto_events
        self.connected = False
        self.mta = 0
        self.mta_extension = 0
        self.commands: List[bytes] = []
        self.busy_count = 0  # Answer this many commands with ERR_CMD_BUSY
        self.drop_count = 0  # Do not answer this many commands (lost responses)
        self.daq_lists: List[_SimDaqList] = []
        self.daq_pointer: Optional[Tuple[int, int, int]] = None
        self.send: Callable[[bytes], None] = lambda data: None
        self._seed_resource = 0
        self._seed_offset = 0
        self._segment_mode = 0
        self._key = bytearray()
        self._block: Optional[int] = None  # Elements left in a master block
        self._pending_upload: Optional[bytes] = None  # GET_ID / event name text, read by UPLOAD
        self._tasks: List[asyncio.Task] = []
        self._start = time.monotonic()

    @property
    def memory(self) -> bytearray:
        """The working (RAM) page."""
        return self.pages[WORKING_PAGE]

    # Helpers

    def _word(self, data: bytes) -> int:
        return int.from_bytes(data, self.byte_order)

    def _res(self, data: bytes = b'') -> None:
        self.send(bytes([RES]) + bytes(data))

    def _err(self, code: int) -> None:
        self.send(bytes([ERR, code]))

    def _range(self, address: int, elements: int) -> Optional[Tuple[int, int]]:
        start = address * self.address_granularity
        end = start + elements * self.address_granularity
        if address < 0 or end > len(self.memory):
            return None
        return start, end

    def _alignment(self) -> bytes:
        return bytes(self.address_granularity - 1)

    # Commands

    def handle(self, data: bytes) -> None:
        if not data:
            return
        data = bytes(data)
        self.commands.append(data)
        command = data[0]
        if self.drop_count:
            self.drop_count -= 1
            return
        if command == SYNCH:
            self._block = None
            self._err(ERR_CMD_SYNCH)
            return
        if self.busy_count:
            self.busy_count -= 1
            self._err(ERR_CMD_BUSY)
            return
        if command == CONNECT:
            self.connected = True
            self.locked = self.protected
            basic = (0x01 if self.byte_order == 'big' else 0) | ({1: 0, 2: 1, 4: 2}[self.address_granularity] << 1)
            basic |= 0x80  # GET_COMM_MODE_INFO is available
            if self.slave_block_mode:
                basic |= 0x40
            resources = RESOURCE_CALPAG | RESOURCE_DAQ
            self._res(bytes([resources, basic, self.max_cto]) + self.max_dto.to_bytes(2, self.byte_order) + b'\x01\x01')
            return
        if not self.connected:
            return  # A slave that is not connected ignores everything but CONNECT
        if command in CALPAG_COMMANDS and self.locked & RESOURCE_CALPAG:
            self._err(ERR_ACCESS_LOCKED)
            return
        if command in DAQ_COMMANDS and self.locked & RESOURCE_DAQ:
            self._err(ERR_ACCESS_LOCKED)
            return
        handler = getattr(self, f'_cmd_{command:02X}', None)
        if handler is None:
            self._err(ERR_CMD_UNKNOWN)
        else:
            handler(data)

    def _cmd_FE(self, data: bytes) -> None:  # DISCONNECT
        self.stop_daq()
        self.connected = False
        self._res()

    def _cmd_FD(self, data: bytes) -> None:  # GET_STATUS
        running = 0x40 if any(d.running for d in self.daq_lists) else 0
        self._res(bytes([running, self.locked, 0, 0, 0]))

    def _cmd_FB(self, data: bytes) -> None:  # GET_COMM_MODE_INFO
        optional = 0x01 if self.master_block_mode else 0
        self._res(bytes([0, optional, 0, 8 if self.master_block_mode else 0, 0, 0, 0x10]))

    def _cmd_FA(self, data: bytes) -> None:  # GET_ID
        text = self.identification.encode('latin-1') if data[1] in (1, 2, 0) else b''
        if len(text) <= self.max_cto - 8:
            self._res(bytes([1, 0, 0]) + len(text).to_bytes(4, self.byte_order) + text)
        else:
            self._pending_upload = text
            self._res(bytes([0, 0, 0]) + len(text).to_bytes(4, self.byte_order))

    def _cmd_F8(self, data: bytes) -> None:  # GET_SEED
        mode, resource = data[1], data[2] if len(data) > 2 else 0
        if mode == 0:
            self._seed_resource = resource
            self._key = bytearray()
            if not self.locked & resource:
                self._res(bytes([0]))
                return
            self._seed_offset = 0
        seed = self.seed[self._seed_offset :]
        part = seed[: self.max_cto - 2]
        self._seed_offset = self._seed_offset + len(part)
        self._res(bytes([len(seed)]) + part)

    def _cmd_F7(self, data: bytes) -> None:  # UNLOCK
        remaining = data[1]
        part = data[2 : 2 + min(remaining, self.max_cto - 2)]
        self._key += part
        if remaining > len(part):
            self._res(bytes([self.locked]))
            return
        if bytes(self._key) == bytes(self.compute_key(self._seed_resource, self.seed)):
            self.locked &= ~self._seed_resource
            self._res(bytes([self.locked]))
        else:
            self.connected = False  # A wrong key ends the session
            self._err(ERR_ACCESS_LOCKED)
        self._key = bytearray()

    def _cmd_F6(self, data: bytes) -> None:  # SET_MTA
        self.mta_extension = data[3]
        self.mta = self._word(data[4:8])
        self._pending_upload = None
        self._res()

    def _upload(self, elements: int) -> None:
        if self._pending_upload is not None:
            size = elements * self.address_granularity
            text, self._pending_upload = self._pending_upload[:size], self._pending_upload[size:] or None
            self._res(self._alignment() + text)
            return
        span = self._range(self.mta, elements)
        if span is None:
            self._err(ERR_OUT_OF_RANGE)
            return
        page = self.pages[self.xcp_page]
        self.mta += elements
        payload = bytes(page[span[0] : span[1]])
        per_packet = self.max_cto - self.address_granularity
        if len(payload) <= per_packet:
            self._res(self._alignment() + payload)
            return
        for offset in range(0, len(payload), per_packet):  # Slave block mode
            self._res(self._alignment() + payload[offset : offset + per_packet])

    def _cmd_F5(self, data: bytes) -> None:  # UPLOAD
        elements = data[1]
        if not self.slave_block_mode and elements * self.address_granularity > self.max_cto - self.address_granularity:
            self._err(ERR_OUT_OF_RANGE)
            return
        self._upload(elements)

    def _cmd_F4(self, data: bytes) -> None:  # SHORT_UPLOAD
        self.mta_extension = data[3]
        self.mta = self._word(data[4:8])
        self._pending_upload = None
        self._upload(data[1])

    def _cmd_F3(self, data: bytes) -> None:  # BUILD_CHECKSUM
        elements = self._word(data[4:8])
        span = self._range(self.mta, elements)
        if span is None:
            self._err(ERR_OUT_OF_RANGE)
            return
        value = checksum(self.checksum_type, self.pages[self.xcp_page][span[0] : span[1]], self.byte_order)
        self.mta += elements
        self._res(bytes([self.checksum_type, 0, 0]) + value.to_bytes(4, self.byte_order))

    def _store(self, elements: int, payload: bytes) -> bool:
        if self.xcp_page == REFERENCE_PAGE:
            self._err(ERR_WRITE_PROTECTED)
            return False
        span = self._range(self.mta, elements)
        if span is None:
            self._err(ERR_OUT_OF_RANGE)
            return False
        self.memory[span[0] : span[1]] = payload[: span[1] - span[0]].ljust(span[1] - span[0], b'\x00')
        self.mta += elements
        return True

    def _download(self, data: bytes, first: bool) -> None:
        elements = data[1]
        header = 2 if self.address_granularity <= 2 else 4
        per_packet = (self.max_cto - header) // self.address_granularity
        if first:
            self._block = None
        elif self._block is None or elements != self._block:
            self._block = None
            self._err(ERR_SEQUENCE)
            return
        count = min(elements, per_packet)
        if elements > per_packet and not self.master_block_mode:
            self._err(ERR_OUT_OF_RANGE)
            return
        if not self._store(count, data[header : header + count * self.address_granularity]):
            self._block = None
            return
        left = elements - count
        if left:
            self._block = left  # More DOWNLOAD_NEXT packets follow, unanswered
        else:
            self._block = None
            self._res()

    def _cmd_F0(self, data: bytes) -> None:  # DOWNLOAD
        self._download(data, True)

    def _cmd_EF(self, data: bytes) -> None:  # DOWNLOAD_NEXT
        self._download(data, False)

    def _cmd_EB(self, data: bytes) -> None:  # SET_CAL_PAGE
        mode, segment, page = data[1], data[2], data[3]
        if segment != 0 or page > REFERENCE_PAGE:
            self._err(ERR_PAGE_NOT_VALID)
            return
        if mode & PAGE_ECU or mode & 0x80:
            self.ecu_page = page
        if mode & PAGE_XCP or mode & 0x80:
            self.xcp_page = page
        self._res()

    def _cmd_EA(self, data: bytes) -> None:  # GET_CAL_PAGE
        mode, segment = data[1], data[2]
        if segment != 0:
            self._err(ERR_PAGE_NOT_VALID)
            return
        self._res(bytes([0, 0, self.ecu_page if mode & PAGE_ECU else self.xcp_page]))

    def _cmd_E9(self, data: bytes) -> None:  # GET_PAG_PROCESSOR_INFO
        self._res(bytes([1, 0]))

    def _cmd_E6(self, data: bytes) -> None:  # SET_SEGMENT_MODE
        self._segment_mode = data[1]
        self._res()

    def _cmd_E5(self, data: bytes) -> None:  # GET_SEGMENT_MODE
        self._res(bytes([0, self._segment_mode]))

    def _cmd_E4(self, data: bytes) -> None:  # COPY_CAL_PAGE
        source, destination = data[2], data[4]
        if data[1] != 0 or data[3] != 0 or source > REFERENCE_PAGE or destination > REFERENCE_PAGE:
            self._err(ERR_PAGE_NOT_VALID)
            return
        if destination == REFERENCE_PAGE:
            self._err(ERR_WRITE_PROTECTED)
            return
        self.pages[destination][:] = self.pages[source]
        self._res()

    # DAQ

    def _cmd_DA(self, data: bytes) -> None:  # GET_DAQ_PROCESSOR_INFO
        properties = DAQ_DYNAMIC | DAQ_PRESCALER_SUPPORTED | (DAQ_TIMESTAMP_SUPPORTED if self.timestamp_size else 0)
        key = self.identification_field << 6
        self._res(
            bytes([properties])
            + (16).to_bytes(2, self.byte_order)
            + len(self.events).to_bytes(2, self.byte_order)
            + bytes([0, key])
        )

    def _cmd_D9(self, data: bytes) -> None:  # GET_DAQ_RESOLUTION_INFO
        mode = self.timestamp_size | (3 << 4)  # Unit 1 µs
        self._res(bytes([1, 0xFF, 1, 0xFF, mode]) + (1).to_bytes(2, self.byte_order))

    def _cmd_D7(self, data: bytes) -> None:  # GET_DAQ_EVENT_INFO
        channel = self._word(data[2:4])
        event = next((e for e in self.events if e.channel == channel), None)
        if event is None:
            self._err(ERR_OUT_OF_RANGE)
            return
        name = event.name.encode('latin-1')
        self._pending_upload = name
        cycle = max(1, round(event.period * 1000))  # In ms (unit 6)
        self._res(bytes([0x04, 0xFF, len(name), min(cycle, 255), 6, 0]))

    def _cmd_D6(self, data: bytes) -> None:  # FREE_DAQ
        self.stop_daq()
        self.daq_lists = []
        self.daq_pointer = None
        self._res()

    def _cmd_D5(self, data: bytes) -> None:  # ALLOC_DAQ
        count = self._word(data[2:4])
        if count > 16:
            self._err(ERR_MEMORY_OVERFLOW)
            return
        self.daq_lists = [_SimDaqList() for _ in range(count)]
        self._res()

    def _list(self, data: bytes) -> Optional[_SimDaqList]:
        number = self._word(data[2:4])
        if number >= len(self.daq_lists):
            self._err(ERR_OUT_OF_RANGE)
            return None
        return self.daq_lists[number]

    def _cmd_D4(self, data: bytes) -> None:  # ALLOC_ODT
        daq = self._list(data)
        if daq is not None:
            daq.odts = [[] for _ in range(data[4])]
            self._res()

    def _cmd_D3(self, data: bytes) -> None:  # ALLOC_ODT_ENTRY
        daq = self._list(data)
        if daq is None:
            return
        if data[4] >= len(daq.odts):
            self._err(ERR_OUT_OF_RANGE)
            return
        daq.odts[data[4]] = [(0, 0, 0)] * data[5]
        self._res()

    def _cmd_E2(self, data: bytes) -> None:  # SET_DAQ_PTR
        daq = self._list(data)
        if daq is None:
            return
        odt, entry = data[4], data[5]
        if odt >= len(daq.odts) or entry >= len(daq.odts[odt]):
            self._err(ERR_OUT_OF_RANGE)
            return
        self.daq_pointer = (self._word(data[2:4]), odt, entry)
        self._res()

    def _cmd_E1(self, data: bytes) -> None:  # WRITE_DAQ
        if self.daq_pointer is None:
            self._err(ERR_DAQ_CONFIG)
            return
        number, odt, entry = self.daq_pointer
        entries = self.daq_lists[number].odts[odt]
        if entry >= len(entries):
            self._err(ERR_DAQ_CONFIG)
            return
        size = data[2] * self.address_granularity
        entries[entry] = (self._word(data[4:8]), data[3], size)
        self.daq_pointer = (number, odt, entry + 1)
        self._res()

    def _cmd_E0(self, data: bytes) -> None:  # SET_DAQ_LIST_MODE
        daq = self._list(data)
        if daq is None:
            return
        daq.mode = data[1]
        daq.event = self._word(data[4:6])
        daq.prescaler = max(1, data[6])
        self._res()

    def _pids(self) -> None:
        pid = 0
        for daq in self.daq_lists:
            daq.first_pid = pid
            pid += len(daq.odts)

    def _cmd_DE(self, data: bytes) -> None:  # START_STOP_DAQ_LIST
        daq = self._list(data)
        if daq is None:
            return
        self._pids()
        mode = data[1]
        if mode == 2:
            daq.selected = True
        else:
            daq.running = mode == 1
            self._schedule()
        self._res(bytes([daq.first_pid if self.identification_field == ID_FIELD_ABSOLUTE else 0]))

    def _cmd_DD(self, data: bytes) -> None:  # START_STOP_SYNCH
        mode = data[1]
        for daq in self.daq_lists:
            if mode == 0:
                daq.running = False
            elif daq.selected:
                daq.running = mode == 1
                daq.selected = False
        self._schedule()
        self._res()

    def _cmd_DC(self, data: bytes) -> None:  # GET_DAQ_CLOCK
        self._res(bytes(3) + self._clock().to_bytes(4, self.byte_order))

    def _clock(self) -> int:
        return int((time.monotonic() - self._start) * 1e6) & 0xFFFFFFFF

    def stop_daq(self) -> None:
        for daq in self.daq_lists:
            daq.running = False
        self._schedule()

    def _schedule(self) -> None:
        """Run the cyclic event channels while a DAQ list is running on them (when `auto_events`)."""
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if not self.auto_events:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        channels = {daq.event for daq in self.daq_lists if daq.running}
        for event in self.events:
            if event.channel in channels and event.period > 0:
                self._tasks.append(loop.create_task(self._run_event(event)))

    async def _run_event(self, event: SimulatedEvent) -> None:
        while True:
            await asyncio.sleep(event.period)
            self.trigger_event(event.channel)

    def trigger_event(self, channel: int) -> None:
        """Sample the running DAQ lists of an event channel and send their DTOs."""
        page = self.pages[self.ecu_page]
        for number, daq in enumerate(self.daq_lists):
            if not daq.running or daq.event != channel:
                continue
            daq.counter += 1
            if daq.counter % daq.prescaler:
                continue
            for odt, entries in enumerate(daq.odts):
                if self.identification_field == ID_FIELD_ABSOLUTE:
                    header = bytes([daq.first_pid + odt])
                elif self.identification_field == ID_FIELD_RELATIVE_BYTE:
                    header = bytes([odt, number])
                elif self.identification_field == ID_FIELD_RELATIVE_WORD:
                    header = bytes([odt]) + number.to_bytes(2, self.byte_order)
                else:
                    header = bytes([odt, 0]) + number.to_bytes(2, self.byte_order)
                if odt == 0 and daq.mode & DAQ_MODE_TIMESTAMP and self.timestamp_size:
                    clock = self._clock() & ((1 << (8 * self.timestamp_size)) - 1)
                    header += clock.to_bytes(self.timestamp_size, self.byte_order)
                payload = bytearray(header)
                for address, _extension, size in entries:
                    start = address * self.address_granularity
                    payload += page[start : start + size]
                self.send(bytes(payload))

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []


class XcpSlaveSimulator:
    """A simulated XCP slave on a CAN session (commands on `request_id`, responses and DTOs on `response_id`).
    Keyword options configure the `XcpSlave` engine (address_granularity, protected, events, …)."""

    def __init__(
        self,
        session: BusSession,
        request_id: int = 0x7E0,
        response_id: int = 0x7E1,
        extended: bool = False,
        byte_order: str = 'little',
        memory: Optional[bytes] = None,
        size: int = 0x10000,
        fd: bool = False,
        **options,
    ):
        self.session = session
        self.request_id = request_id
        self.response_id = response_id
        self.extended = extended
        self.fd = fd
        if fd:
            options.setdefault('max_cto', 64)
            options.setdefault('max_dto', 64)
        self.slave = XcpSlave(byte_order=byte_order, memory=memory, size=size, **options)
        self.slave.send = self._send
        session.add_message_listener(self._on_message)

    def __getattr__(self, name: str):
        return getattr(self.__dict__['slave'], name)

    def __setattr__(self, name: str, value) -> None:
        slave = self.__dict__.get('slave')
        if slave is not None and hasattr(slave, name) and name not in self.__dict__:
            setattr(slave, name, value)  # E.g. busy_count, drop_count, protected
        else:
            super().__setattr__(name, value)

    @property
    def memory(self) -> bytearray:
        return self.slave.memory

    @property
    def connected(self) -> bool:
        return self.slave.connected

    @property
    def commands(self) -> List[bytes]:
        return self.slave.commands

    def stop(self) -> None:
        self.slave.close()
        self.session.remove_message_listener(self._on_message)

    def _send(self, data: bytes) -> None:
        from can_explorer.core.can_command import fd_length

        payload = bytes(data)
        payload = payload.ljust(fd_length(len(payload)) if self.fd else 8, b'\x00')
        self.session.send(
            can.Message(
                arbitration_id=self.response_id,
                is_extended_id=self.extended,
                is_fd=self.fd,
                bitrate_switch=self.fd,
                data=payload,
            )
        )

    def _on_message(self, msg: can.Message) -> None:
        if (
            not msg.is_rx
            or msg.arbitration_id != self.request_id
            or msg.is_extended_id != self.extended
            or not msg.data
        ):
            return
        self.slave.handle(bytes(msg.data))


def example_memory(size: int = 0x10000) -> bytearray:
    """Memory (little endian) holding plausible values for every variable of `examples/calibration/example.a2l`,
    for a simulated slave that demonstrates the XCP/CCP page."""
    from struct import pack

    memory = bytearray(size)

    def put(address: int, data: bytes) -> None:
        memory[address : address + len(data)] = data

    def words(values, fmt='<H') -> bytes:
        return b''.join(pack(fmt, v) for v in values)

    put(0x1000, words([6000]))  # EngineSpeed 1500 rpm
    put(0x1002, bytes([125]))  # CoolantTemp 85 degC
    put(0x1004, words([13800]))  # BatteryVoltage 13.8 V
    put(0x1006, bytes([0x01]))  # BrakePressed
    put(0x1010, words([5000, 5010, 4990, 5005]))  # WheelSpeeds ~50 km/h
    put(0x2000, words([3200]))  # IdleTarget 800 rpm
    put(0x2004, pack('<f', 1.0))  # InjectorScale
    put(0x2008, bytes([1]))  # FanMode Auto
    put(0x2100, bytes([5]))  # IgnitionCurve: 5 points, axis (word aligned) and values
    put(0x2102, words([4000, 8000, 12000, 16000, 24000]))
    put(0x210C, words([50, 100, 150, 200, 250], '<h'))
    put(0x2200, bytes([4]))  # FuelMap: 4 speed points × the 3 load points of LoadAxis
    put(0x2202, words([4000, 8000, 16000, 24000]))
    put(0x220A, bytes([20, 30, 40, 50, 40, 60, 80, 100, 60, 90, 120, 150]))
    put(0x2300, bytes([3, 20, 50, 80]))  # LoadAxis
    put(0x2400, b''.join(pack('<f', v) for v in (3.5, 2.1, 1.4, 1.0, 0.8, 0.65)))  # GearRatios
    put(0x2500, b'EU5-1234')  # VariantCode
    put(0x3000, b'EX-1.0')  # EPK
    return memory
