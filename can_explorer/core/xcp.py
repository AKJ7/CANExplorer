"""XCP (ASAM MCD-1 XCP): the master for measurement and calibration of an ECU — memory access by address, seed &
key, calibration pages, checksums and dynamic DAQ measurement — over CAN, CAN FD (`CanCommandClient`) or Ethernet
(`core/xcp_ethernet.py`), and a simulated slave (`core/xcp_slave.py`) for trying it out and for the tests.

XCP is a command/response protocol: the master sends a Command Transfer Object (CTO) and the slave answers with a
positive response (RES) or an error (ERR). DAQ data (DTOs), events (EV) and service requests (SERV) arrive
unsolicited and are routed away from the command responses.

What is implemented:
- CONNECT (byte order, address granularity, MAX_CTO/MAX_DTO, block modes), GET_STATUS, GET_COMM_MODE_INFO, GET_ID,
  SYNCH, DISCONNECT.
- Seed & key (GET_SEED/UNLOCK, multi-packet), by request or automatically when a command answers ERR_ACCESS_LOCKED.
- SET_MTA, UPLOAD (also in slave block mode), SHORT_UPLOAD, DOWNLOAD (also in master block mode with DOWNLOAD_NEXT),
  for byte, word and dword address granularity.
- Error handling: ERR_CMD_BUSY is repeated, a timeout sends SYNCH and repeats the command (with SET_MTA first for
  commands that advance the MTA).
- BUILD_CHECKSUM (the standard ADD/CRC types are verified locally), calibration pages (GET/SET_CAL_PAGE,
  GET_PAG_PROCESSOR_INFO, COPY_CAL_PAGE, GET/SET_SEGMENT_MODE).
- Dynamic DAQ (FREE_DAQ, ALLOC_*, SET_DAQ_PTR, WRITE_DAQ, SET_DAQ_LIST_MODE, START_STOP_*), with all four
  identification field types and DAQ timestamps. Static (predefined) DAQ lists and STIM are not.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from can_explorer.core.can_command import CanCommandTimeout
from can_explorer.core.xcp_protocol import (  # noqa: F401  (re-exported for scripts)
    ALLOC_DAQ,
    ALLOC_ODT,
    ALLOC_ODT_ENTRY,
    BUILD_CHECKSUM,
    CHECKSUM_NAMES,
    CHECKSUM_USER_DEFINED,
    COMM_ADDRESS_GRANULARITY,
    COMM_BYTE_ORDER_MOTOROLA,
    COMM_MASTER_BLOCK_MODE,
    COMM_OPTIONAL,
    COMM_SLAVE_BLOCK_MODE,
    COMMAND_NAMES,
    CONNECT,
    COPY_CAL_PAGE,
    DAQ_DYNAMIC,
    DAQ_MODE_TIMESTAMP,
    DAQ_SELECT,
    DAQ_TIMESTAMP_SUPPORTED,
    DISCONNECT,
    DOWNLOAD,
    DOWNLOAD_NEXT,
    ERR,
    ERR_ACCESS_LOCKED,
    ERR_CMD_BUSY,
    ERROR_CODES,
    EV,
    EVENT_CODES,
    FREE_DAQ,
    GET_CAL_PAGE,
    GET_COMM_MODE_INFO,
    GET_DAQ_CLOCK,
    GET_DAQ_EVENT_INFO,
    GET_DAQ_PROCESSOR_INFO,
    GET_DAQ_RESOLUTION_INFO,
    GET_ID,
    GET_PAG_PROCESSOR_INFO,
    GET_SEED,
    GET_SEGMENT_MODE,
    GET_STATUS,
    ID_FIELD_ABSOLUTE,
    ID_FIELD_RELATIVE_BYTE,
    ID_FIELD_RELATIVE_WORD,
    ID_FIELD_RELATIVE_WORD_ALIGNED,
    PAGE_ALL,
    PAGE_ECU,
    PAGE_XCP,
    RES,
    RESOURCE_CALPAG,
    RESOURCE_DAQ,
    RESOURCE_PGM,
    RESOURCE_STIM,
    RESOURCES,
    SERV,
    SET_CAL_PAGE,
    SET_DAQ_LIST_MODE,
    SET_DAQ_PTR,
    SET_MTA,
    SET_SEGMENT_MODE,
    SHORT_UPLOAD,
    START_STOP_DAQ_LIST,
    START_STOP_SYNCH,
    SYNCH,
    SYNCH_START_SELECTED,
    SYNCH_STOP_ALL,
    TIMESTAMP_FIXED,
    TIMESTAMP_UNITS,
    UNLOCK,
    UPLOAD,
    WRITE_DAQ,
    checksum,
    granularity_size,
    resource_names,
)

ComputeKey = Callable[[int, bytes], bytes]  # (resource mask, seed) -> key


class XcpError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, data: bytes = b''):
        super().__init__(message)
        self.code = code
        self.data = data


def _command_name(code: int) -> str:
    return COMMAND_NAMES.get(code, f'0x{code:02X}')


@dataclass
class XcpEvent:
    """An EV packet: an event code, e.g. 0x06 DAQ overload, and its data."""

    code: int
    data: bytes
    timestamp: float

    @property
    def name(self) -> str:
        return EVENT_CODES.get(self.code, f'0x{self.code:02X}')


class XcpClient:
    """XCP master over a command transport (a `CanCommandClient` or an `XcpEthernetTransport`). `read`/`write`
    access the slave's memory by address in bytes, whatever its address granularity.

    `compute_key(resource, seed) -> key` is the seed & key algorithm (manufacturer specific, the user's); with it,
    a command answered with ERR_ACCESS_LOCKED unlocks the protected resources and is repeated.
    """

    def __init__(
        self,
        transport,
        compute_key: Optional[ComputeKey] = None,
        retries: int = 2,
        busy_retries: int = 20,
        busy_delay: float = 0.01,
    ):
        self.transport = transport
        self.compute_key = compute_key
        self.retries = retries
        self.busy_retries = busy_retries
        self.busy_delay = busy_delay
        self.connected = False
        self.byte_order = 'little'  # Of addresses and multi-byte fields, from the CONNECT response
        self.address_granularity = 1  # Bytes per address (1, 2 or 4)
        self.max_cto = 8
        self.max_dto = 8
        self.resources = 0  # Resources the slave has (CONNECT)
        self.protection = 0  # Resources still locked (GET_STATUS, UNLOCK)
        self.slave_block_mode = False
        self.master_block_mode = False
        self.max_bs = 1
        self.min_st = 0  # In 100 µs
        self.protocol_version = 0
        self.transport_version = 0
        self.events: List[XcpEvent] = []
        self.on_event: Optional[Callable[[XcpEvent], None]] = None
        self.dto_handler: Optional[Callable[[bytes, float], None]] = None
        self._mta: Optional[Tuple[int, int]] = None  # (address, extension) the MTA should be at
        transport.unsolicited = self._on_packet

    # Transport

    def _on_packet(self, data: bytes, timestamp: float) -> bool:
        """Route a received packet: False for command responses, True for DAQ data, events and service requests."""
        pid = data[0]
        if pid in (RES, ERR):
            return False
        if pid == EV:
            event = XcpEvent(data[1] if len(data) > 1 else 0, bytes(data[2:]), timestamp)
            self.events.append(event)
            del self.events[:-100]
            if self.on_event is not None:
                self.on_event(event)
        elif pid != SERV and self.dto_handler is not None:
            self.dto_handler(data, timestamp)
        return True

    async def synch(self) -> None:
        """Resynchronise after a timeout: SYNCH is answered with ERR_CMD_SYNCH."""
        try:
            await self.transport.request(bytes([SYNCH]))
        except CanCommandTimeout:
            pass

    async def _command(self, data: bytes, timeout: Optional[float] = None, unlock: bool = True) -> bytes:
        """Send a command and return the positive response without its PID. Repeats on ERR_CMD_BUSY and after a
        timeout (SYNCH first, and SET_MTA for a command that advances the MTA); unlocks on ERR_ACCESS_LOCKED."""
        data = bytes(data)
        timeouts = busy = 0
        mta = self._mta if data[0] in (UPLOAD, DOWNLOAD, DOWNLOAD_NEXT, BUILD_CHECKSUM) else None
        while True:
            try:
                response = await self.transport.request(data, timeout)
            except CanCommandTimeout:
                timeouts += 1
                if timeouts > self.retries:
                    raise
                await self.synch()
                if mta is not None:
                    await self.set_mta(*mta)
                continue
            if not response:
                raise XcpError('Empty XCP response')
            if response[0] == RES:
                return bytes(response[1:])
            if response[0] != ERR:
                raise XcpError(f'Unexpected XCP response 0x{response[0]:02X}')
            code = response[1] if len(response) > 1 else 0
            if code == ERR_CMD_BUSY and busy < self.busy_retries:
                busy += 1
                await asyncio.sleep(self.busy_delay)
                continue
            if code == ERR_ACCESS_LOCKED and unlock and self.compute_key is not None:
                await self.unlock_all()
                unlock = False  # Once: a resource that stays locked is reported
                if mta is not None:
                    await self.set_mta(*mta)
                continue
            raise XcpError(
                f'XCP {_command_name(data[0])}: {ERROR_CODES.get(code, f"error 0x{code:02X}")}', code, bytes(response)
            )

    # Session

    async def connect(self, mode: int = 0) -> bytes:
        response = await self._command(bytes([CONNECT, mode]), unlock=False)
        if len(response) >= 1:
            self.resources = response[0]
        if len(response) >= 2:
            basic = response[1]
            self.byte_order = 'big' if basic & COMM_BYTE_ORDER_MOTOROLA else 'little'
            self.address_granularity = granularity_size((basic & COMM_ADDRESS_GRANULARITY) >> 1)
            self.slave_block_mode = bool(basic & COMM_SLAVE_BLOCK_MODE)
        limit = getattr(self.transport, 'max_payload', 8)
        if len(response) >= 3:
            self.max_cto = min(response[2] or 8, limit)
        if len(response) >= 5:
            self.max_dto = min(int.from_bytes(response[3:5], self.byte_order) or 8, limit)
        if len(response) >= 7:
            self.protocol_version, self.transport_version = response[5], response[6]
        self.connected = True
        if len(response) >= 2 and response[1] & COMM_OPTIONAL:
            try:
                await self.get_comm_mode_info()
            except XcpError:
                pass
        try:
            self.protection = (await self.get_status())['protection']
        except XcpError:
            pass
        return response

    async def disconnect(self) -> None:
        try:
            if self.dto_handler is not None:
                try:
                    await self._command(bytes([START_STOP_SYNCH, SYNCH_STOP_ALL]), unlock=False)
                except XcpError:
                    pass
                self.dto_handler = None
            await self._command(bytes([DISCONNECT]), unlock=False)
        finally:
            self.connected = False

    async def get_status(self) -> Dict[str, int]:
        response = await self._command(bytes([GET_STATUS]), unlock=False)
        status = {
            'session': response[0] if response else 0,
            'protection': response[1] if len(response) > 1 else 0,
            'configuration_id': int.from_bytes(response[3:5], self.byte_order) if len(response) >= 5 else 0,
        }
        self.protection = status['protection']
        return status

    async def get_comm_mode_info(self) -> Dict[str, int]:
        response = await self._command(bytes([GET_COMM_MODE_INFO]), unlock=False)
        optional = response[1] if len(response) > 1 else 0
        self.master_block_mode = bool(optional & COMM_MASTER_BLOCK_MODE)
        if len(response) > 4:
            self.max_bs = max(1, response[3])
            self.min_st = response[4]
        return {
            'optional': optional,
            'max_bs': self.max_bs,
            'min_st': self.min_st,
            'queue_size': response[5] if len(response) > 5 else 0,
            'driver_version': response[6] if len(response) > 6 else 0,
        }

    async def get_id(self, kind: int = 1) -> str:
        """The slave's identification, e.g. kind 1: the A2L file name without path and extension."""
        response = await self._command(bytes([GET_ID, kind]), unlock=False)
        mode = response[0] if response else 0
        length = int.from_bytes(response[3:7], self.byte_order) if len(response) >= 7 else 0
        if not length:
            return ''
        if mode & 0x01:  # The identification is in the response itself
            data = response[7 : 7 + length]
        else:  # At the MTA, which the slave set
            self._mta = None
            data = await self.upload(length)
        return bytes(data).rstrip(b'\x00').decode('latin-1')

    # Seed & key

    async def unlock(self, resource: int, compute_key: Optional[ComputeKey] = None) -> int:
        """Unlock one resource (RESOURCE_CALPAG, _DAQ, _STIM, _PGM) with seed & key; returns the protection left."""
        algorithm = compute_key or self.compute_key
        if algorithm is None:
            raise XcpError(f'{RESOURCES.get(resource, resource)} is locked and no seed & key algorithm is set')
        response = await self._command(bytes([GET_SEED, 0, resource]), unlock=False)
        length = response[0] if response else 0
        if length == 0:  # Not protected
            return self.protection & ~resource
        part = self.max_cto - 2
        seed = bytearray(response[1 : 1 + min(length, part)])
        while len(seed) < length:
            response = await self._command(bytes([GET_SEED, 1, resource]), unlock=False)
            remaining = response[0] if response else 0
            if remaining == 0:
                break
            seed += response[1 : 1 + min(remaining, part)]
        key = bytes(algorithm(resource, bytes(seed)))
        if not key:
            raise XcpError('The seed & key algorithm returned no key')
        offset = 0
        response = b''
        while offset < len(key):
            chunk = key[offset : offset + part]
            response = await self._command(bytes([UNLOCK, len(key) - offset]) + chunk, unlock=False)
            offset += len(chunk)
        self.protection = response[0] if response else 0
        if self.protection & resource:
            raise XcpError(f'{RESOURCES.get(resource, resource)} is still locked: the key was not accepted')
        return self.protection

    async def unlock_all(self, compute_key: Optional[ComputeKey] = None) -> int:
        """Unlock every protected resource; returns the protection left (0 when all are unlocked)."""
        protection = (await self.get_status())['protection']
        for resource in RESOURCES:
            if protection & resource:
                protection = await self.unlock(resource, compute_key)
        return protection

    # Memory (sizes in bytes; addresses in the slave's address granularity)

    def _elements(self, size: int) -> int:
        return -(-size // self.address_granularity)

    async def set_mta(self, address: int, extension: int = 0) -> None:
        await self._command(
            bytes([SET_MTA, 0, 0, extension & 0xFF]) + (address & 0xFFFFFFFF).to_bytes(4, self.byte_order)
        )
        self._mta = (address, extension)

    def _advance(self, elements: int) -> None:
        if self._mta is not None:
            self._mta = (self._mta[0] + elements, self._mta[1])

    def _payload(self, response: bytes) -> bytes:
        """The data of an UPLOAD response: after the PID come granularity - 1 alignment bytes."""
        return response[self.address_granularity - 1 :]

    async def upload(self, size: int) -> bytes:
        """Read `size` bytes from the current MTA (which advances), several commands (or one slave block) if needed."""
        granularity = self.address_granularity
        per_packet = (self.max_cto - granularity) // granularity  # Elements per response
        elements = self._elements(size)
        data = bytearray()
        while elements > 0:
            if self.slave_block_mode:
                count = min(elements, 255)
                needed = count * granularity
                response = self._payload(await self._command(bytes([UPLOAD, count])))
                block = bytearray(response[:needed])
                while len(block) < needed:  # The rest of the block arrives as further RES packets
                    packet = await self.transport.receive()
                    if packet[0] == ERR:
                        raise XcpError('XCP UPLOAD: error in a slave block', packet[1] if len(packet) > 1 else None)
                    block += self._payload(packet[1:])[: needed - len(block)]
            else:
                count = min(elements, per_packet)
                block = self._payload(await self._command(bytes([UPLOAD, count])))[: count * granularity]
            self._advance(count)
            data += block
            elements -= count
        return bytes(data[:size])

    async def short_upload(self, address: int, size: int, extension: int = 0) -> bytes:
        granularity = self.address_granularity
        elements = self._elements(size)
        if elements <= (self.max_cto - granularity) // granularity:
            response = await self._command(
                bytes([SHORT_UPLOAD, elements, 0, extension & 0xFF])
                + (address & 0xFFFFFFFF).to_bytes(4, self.byte_order)
            )
            self._mta = (address + elements, extension)
            return self._payload(response)[:size]
        await self.set_mta(address, extension)
        return await self.upload(size)

    async def download(self, address: int, data: bytes, extension: int = 0) -> None:
        """Write `data` at `address` (a whole number of address elements), in master block mode when available."""
        granularity = self.address_granularity
        data = bytes(data)
        if len(data) % granularity:
            raise XcpError(f'{len(data)} bytes is not a whole number of {granularity}-byte address elements')
        await self.set_mta(address, extension)
        header = 2 if granularity <= 2 else 4  # PID, n and, for dword granularity, two alignment bytes
        per_packet = (self.max_cto - header) // granularity
        elements = len(data) // granularity
        offset = 0
        while offset < elements:
            if self.master_block_mode and self.max_bs > 1:
                count = min(elements - offset, per_packet * self.max_bs, 255)
                await self._download_block(data[offset * granularity : (offset + count) * granularity], header)
            else:
                count = min(elements - offset, per_packet)
                chunk = data[offset * granularity : (offset + count) * granularity]
                await self._command(bytes([DOWNLOAD, count]) + bytes(header - 2) + chunk)
                self._advance(count)
            offset += count

    async def _download_block(self, block: bytes, header: int) -> None:
        """One master block: DOWNLOAD with the first part, DOWNLOAD_NEXT with the rest; only the last is answered."""
        granularity = self.address_granularity
        per_packet = (self.max_cto - header) // granularity
        remaining = len(block) // granularity
        mta = self._mta
        for attempt in range(self.retries + 1):
            self.transport.drain()
            offset, first, left = 0, True, remaining
            while left > 0:
                count = min(left, per_packet)
                chunk = block[offset * granularity : (offset + count) * granularity]
                self.transport.send(bytes([DOWNLOAD if first else DOWNLOAD_NEXT, left]) + bytes(header - 2) + chunk)
                first, offset, left = False, offset + count, left - count
                if left and self.min_st:
                    await asyncio.sleep(self.min_st / 10000)
            try:
                response = await self.transport.receive()
            except CanCommandTimeout:
                if attempt == self.retries:
                    raise
                await self.synch()
                if mta is not None:
                    await self.set_mta(*mta)
                continue
            if response[0] == ERR:
                code = response[1] if len(response) > 1 else 0
                raise XcpError(f'XCP DOWNLOAD (block): {ERROR_CODES.get(code, f"error 0x{code:02X}")}', code)
            self._advance(remaining)
            return

    # The common calibration interface (shared with CcpClient)

    async def read(self, address: int, extension: int, size: int) -> bytes:
        return await self.short_upload(address, size, extension)

    async def write(self, address: int, extension: int, data: bytes) -> None:
        granularity = self.address_granularity
        if len(data) % granularity:  # Read-modify-write the partial last element
            tail = await self.read(address + len(data) // granularity, extension, granularity)
            data = bytes(data) + tail[len(data) % granularity :]
        await self.download(address, data, extension)

    # Checksum

    async def build_checksum(self, address: int, size: int, extension: int = 0) -> Tuple[int, int]:
        """(checksum type, value) the slave computes over `size` bytes at `address`."""
        await self.set_mta(address, extension)
        response = await self._command(
            bytes([BUILD_CHECKSUM, 0, 0, 0]) + self._elements(size).to_bytes(4, self.byte_order)
        )
        return response[0], int.from_bytes(response[3:7], self.byte_order)

    async def verify_checksum(self, address: int, data: bytes, extension: int = 0) -> Dict[str, object]:
        """Compare the slave's checksum over len(data) bytes at `address` with the checksum of `data` (e.g. the
        bytes just written). A user-defined checksum type cannot be checked here."""
        kind, value = await self.build_checksum(address, len(data), extension)
        result: Dict[str, object] = {'type': CHECKSUM_NAMES.get(kind, f'0x{kind:02X}'), 'ecu': value}
        if kind == CHECKSUM_USER_DEFINED or kind not in CHECKSUM_NAMES:
            result['match'] = None
        else:
            expected = checksum(kind, data, self.byte_order)
            result.update(expected=expected, match=expected == value)
        return result

    # Calibration pages

    async def get_cal_page(self, segment: int = 0, mode: int = PAGE_XCP) -> int:
        response = await self._command(bytes([GET_CAL_PAGE, mode, segment]))
        return response[2]

    async def set_cal_page(self, page: int, segment: int = 0, mode: int = PAGE_ECU | PAGE_XCP) -> None:
        await self._command(bytes([SET_CAL_PAGE, mode, segment, page]))

    async def get_pag_processor_info(self) -> Dict[str, int]:
        response = await self._command(bytes([GET_PAG_PROCESSOR_INFO]))
        return {'segments': response[0], 'freeze_supported': bool(response[1] & 0x01) if len(response) > 1 else False}

    async def copy_cal_page(self, source_page: int, destination_page: int, segment: int = 0) -> None:
        await self._command(bytes([COPY_CAL_PAGE, segment, source_page, segment, destination_page]))

    async def get_segment_mode(self, segment: int = 0) -> int:
        return (await self._command(bytes([GET_SEGMENT_MODE, 0, segment])))[1]

    async def set_segment_mode(self, mode: int, segment: int = 0) -> None:
        await self._command(bytes([SET_SEGMENT_MODE, mode, segment]))

    # DAQ

    def _word(self, value: int) -> bytes:
        return (value & 0xFFFF).to_bytes(2, self.byte_order)

    async def get_daq_processor_info(self) -> Dict[str, int]:
        response = await self._command(bytes([GET_DAQ_PROCESSOR_INFO]))
        key = response[6] if len(response) > 6 else 0
        return {
            'properties': response[0],
            'dynamic': bool(response[0] & DAQ_DYNAMIC),
            'timestamp_supported': bool(response[0] & DAQ_TIMESTAMP_SUPPORTED),
            'max_daq': int.from_bytes(response[1:3], self.byte_order),
            'max_event_channel': int.from_bytes(response[3:5], self.byte_order),
            'min_daq': response[5],
            'identification_field': key >> 6,
        }

    async def get_daq_resolution_info(self) -> Dict[str, object]:
        response = await self._command(bytes([GET_DAQ_RESOLUTION_INFO]))
        mode = response[4]
        ticks = int.from_bytes(response[5:7], self.byte_order) or 1
        return {
            'granularity': response[0] or 1,
            'max_entry_size': response[1] or 0xFF,
            'timestamp_size': mode & 0x07,
            'timestamp_fixed': bool(mode & TIMESTAMP_FIXED),
            'timestamp_resolution': ticks * TIMESTAMP_UNITS.get(mode >> 4, 1e-6),  # Seconds per timestamp tick
        }

    async def get_daq_event_info(self, channel: int) -> Dict[str, object]:
        response = await self._command(bytes([GET_DAQ_EVENT_INFO, 0]) + self._word(channel))
        name = ''
        if response[2]:
            self._mta = None
            name = (await self.upload(response[2])).decode('latin-1', 'replace')
        unit = TIMESTAMP_UNITS.get(response[4], 0)
        return {
            'channel': channel,
            'name': name,
            'properties': response[0],
            'max_daq_lists': response[1],
            'cycle': response[3] * unit,  # Seconds, 0 for a not cyclic event
            'priority': response[5] if len(response) > 5 else 0,
        }

    async def daq_events(self) -> List[Dict[str, object]]:
        info = await self.get_daq_processor_info()
        events = []
        for channel in range(info['max_event_channel']):
            try:
                events.append(await self.get_daq_event_info(channel))
            except XcpError:
                break
        return events

    async def free_daq(self) -> None:
        await self._command(bytes([FREE_DAQ]))

    async def alloc_daq(self, count: int) -> None:
        await self._command(bytes([ALLOC_DAQ, 0]) + self._word(count))

    async def alloc_odt(self, daq_list: int, count: int) -> None:
        await self._command(bytes([ALLOC_ODT, 0]) + self._word(daq_list) + bytes([count]))

    async def alloc_odt_entry(self, daq_list: int, odt: int, count: int) -> None:
        await self._command(bytes([ALLOC_ODT_ENTRY, 0]) + self._word(daq_list) + bytes([odt, count]))

    async def set_daq_ptr(self, daq_list: int, odt: int, entry: int) -> None:
        await self._command(bytes([SET_DAQ_PTR, 0]) + self._word(daq_list) + bytes([odt, entry]))

    async def write_daq(self, address: int, size: int, extension: int = 0, bit_offset: int = 0xFF) -> None:
        await self._command(
            bytes([WRITE_DAQ, bit_offset, size, extension & 0xFF]) + (address & 0xFFFFFFFF).to_bytes(4, self.byte_order)
        )

    async def set_daq_list_mode(
        self, daq_list: int, event: int, mode: int = 0, prescaler: int = 1, priority: int = 0
    ) -> None:
        await self._command(
            bytes([SET_DAQ_LIST_MODE, mode]) + self._word(daq_list) + self._word(event) + bytes([prescaler, priority])
        )

    async def start_stop_daq_list(self, mode: int, daq_list: int) -> int:
        """START (1), STOP (0) or SELECT (2) a DAQ list; returns its first PID."""
        response = await self._command(bytes([START_STOP_DAQ_LIST, mode]) + self._word(daq_list))
        return response[0] if response else 0

    async def start_stop_synch(self, mode: int) -> None:
        await self._command(bytes([START_STOP_SYNCH, mode]))

    async def get_daq_clock(self) -> int:
        response = await self._command(bytes([GET_DAQ_CLOCK]))
        return int.from_bytes(response[3:7], self.byte_order)

    # The common calibration interface (shared with CcpClient)

    async def start_measurement(
        self,
        signals: Sequence['DaqSignal'],
        on_sample: Callable[['DaqSample'], None],
        prescaler: int = 1,
        timestamps: bool = True,
    ) -> 'XcpMeasurement':
        measurement = XcpMeasurement(self, signals, on_sample, prescaler, timestamps)
        await measurement.start()
        return measurement


# DAQ measurement


@dataclass(frozen=True)
class DaqSignal:
    """A memory range to measure on an event channel. `key` identifies it in the samples (e.g. the A2L name)."""

    key: str
    address: int
    size: int  # Bytes
    extension: int = 0
    event: int = 0


@dataclass
class DaqSample:
    """One sample of a DAQ list: the raw bytes of each of its signals."""

    event: int
    timestamp: float  # Host time of the last DTO
    values: Dict[str, bytes]
    ecu_time: Optional[float] = None  # Seconds, from the DAQ timestamp (unwrapped), when there is one


@dataclass
class _Entry:
    key: str
    offset: int  # Byte offset in the signal
    address: int
    extension: int
    size: int  # Bytes


@dataclass
class _DaqList:
    number: int
    event: int
    odts: List[List[_Entry]] = field(default_factory=list)
    sizes: Dict[str, int] = field(default_factory=dict)
    first_pid: int = 0
    buffer: Optional[Dict[str, bytearray]] = None
    next_odt: int = 0
    ecu_time: Optional[float] = None
    last_raw: Optional[int] = None
    wraps: int = 0


def pack_odts(
    signals: Sequence[Tuple[str, int, int, int]],
    first_capacity: int,
    capacity: int,
    max_entry: int,
    granularity: int = 1,
    address_granularity: int = 1,
) -> List[List[_Entry]]:
    """Pack (key, address, extension, size) memory ranges into ODTs of `capacity` bytes (`first_capacity` for the
    first, which carries the timestamp), splitting a range over entries and ODTs when it does not fit."""
    odts: List[List[_Entry]] = [[]]
    used = 0
    limit = first_capacity
    for key, address, extension, size in signals:
        offset = 0
        while offset < size:
            space = limit - used
            take = min(size - offset, space, max_entry)
            take -= take % max(granularity, address_granularity)
            if take <= 0:
                odts.append([])
                used, limit = 0, capacity
                continue
            odts[-1].append(_Entry(key, offset, address + offset // address_granularity, extension, take))
            offset += take
            used += take
    return [odt for odt in odts if odt]


class XcpMeasurement:
    """Dynamic DAQ: one DAQ list per event channel with the given signals; `on_sample` gets each complete sample."""

    def __init__(
        self,
        client: XcpClient,
        signals: Sequence[DaqSignal],
        on_sample: Callable[[DaqSample], None],
        prescaler: int = 1,
        timestamps: bool = True,
    ):
        self.client = client
        self.signals = list(signals)
        self.on_sample = on_sample
        self.prescaler = max(1, prescaler)
        self.timestamps = timestamps
        self.lists: List[_DaqList] = []
        self.identification = ID_FIELD_ABSOLUTE
        self.timestamp_size = 0
        self.timestamp_resolution = 1e-6
        self.running = False
        self.samples = 0
        self.lost = 0  # Samples dropped because a DTO was missing

    @property
    def id_size(self) -> int:
        return {ID_FIELD_ABSOLUTE: 1, ID_FIELD_RELATIVE_BYTE: 2, ID_FIELD_RELATIVE_WORD: 3}.get(self.identification, 4)

    async def start(self) -> None:
        client = self.client
        if not self.signals:
            raise XcpError('Nothing to measure')
        info = await client.get_daq_processor_info()
        if not info['dynamic']:
            raise XcpError('The slave has static (predefined) DAQ lists only, which are not supported: poll instead')
        resolution = await client.get_daq_resolution_info()
        self.identification = info['identification_field']
        if info['timestamp_supported'] and (self.timestamps or resolution['timestamp_fixed']):
            self.timestamp_size = resolution['timestamp_size']
            self.timestamp_resolution = resolution['timestamp_resolution']
        granularity = client.address_granularity
        max_entry = resolution['max_entry_size'] * resolution['granularity']
        capacity = client.max_dto - self.id_size
        by_event: Dict[int, List[DaqSignal]] = {}
        for signal in self.signals:
            by_event.setdefault(signal.event, []).append(signal)
        if len(by_event) > info['max_daq'] - info['min_daq'] and info['max_daq']:
            raise XcpError(f'{len(by_event)} event channels need more DAQ lists than the slave has')
        self.lists = []
        for index, (event, signals) in enumerate(sorted(by_event.items())):
            daq = _DaqList(info['min_daq'] + index, event)
            daq.odts = pack_odts(
                [(s.key, s.address, s.extension, s.size) for s in signals],
                capacity - self.timestamp_size,
                capacity,
                max_entry,
                resolution['granularity'],
                granularity,
            )
            daq.sizes = {s.key: s.size for s in signals}
            self.lists.append(daq)
        await client.free_daq()
        await client.alloc_daq(len(self.lists))
        for daq in self.lists:
            await client.alloc_odt(daq.number, len(daq.odts))
        for daq in self.lists:
            for odt, entries in enumerate(daq.odts):
                await client.alloc_odt_entry(daq.number, odt, len(entries))
        for daq in self.lists:
            for odt, entries in enumerate(daq.odts):
                await client.set_daq_ptr(daq.number, odt, 0)
                for entry in entries:
                    await client.write_daq(entry.address, entry.size // granularity, entry.extension)
        mode = DAQ_MODE_TIMESTAMP if self.timestamp_size else 0
        for daq in self.lists:
            await client.set_daq_list_mode(daq.number, daq.event, mode, self.prescaler)
            daq.first_pid = await client.start_stop_daq_list(DAQ_SELECT, daq.number)
        client.dto_handler = self._on_dto
        await client.start_stop_synch(SYNCH_START_SELECTED)
        self.running = True

    async def stop(self) -> None:
        self.running = False
        try:
            await self.client.start_stop_synch(SYNCH_STOP_ALL)
        finally:
            if self.client.dto_handler == self._on_dto:
                self.client.dto_handler = None

    def _locate(self, data: bytes) -> Optional[Tuple[_DaqList, int]]:
        if self.identification == ID_FIELD_ABSOLUTE:
            pid = data[0]
            for daq in self.lists:
                if daq.first_pid <= pid < daq.first_pid + len(daq.odts):
                    return daq, pid - daq.first_pid
            return None
        odt = data[0]
        if self.identification == ID_FIELD_RELATIVE_BYTE:
            number = data[1]
        elif self.identification == ID_FIELD_RELATIVE_WORD:
            number = int.from_bytes(data[1:3], self.client.byte_order)
        else:
            number = int.from_bytes(data[2:4], self.client.byte_order)
        daq = next((d for d in self.lists if d.number == number), None)
        return (daq, odt) if daq is not None and odt < len(daq.odts) else None

    def _on_dto(self, data: bytes, timestamp: float) -> None:
        if len(data) < self.id_size:
            return
        located = self._locate(data)
        if located is None:
            return
        daq, odt = located
        position = self.id_size
        if odt == 0:
            if daq.buffer is not None and daq.next_odt:
                self.lost += 1
            daq.buffer = {key: bytearray(size) for key, size in daq.sizes.items()}
            daq.next_odt = 0
            daq.ecu_time = None
            if self.timestamp_size:
                raw = int.from_bytes(data[position : position + self.timestamp_size], self.client.byte_order)
                if daq.last_raw is not None and raw < daq.last_raw:
                    daq.wraps += 1
                daq.last_raw = raw
                daq.ecu_time = (raw + daq.wraps * (1 << (8 * self.timestamp_size))) * self.timestamp_resolution
                position += self.timestamp_size
        if daq.buffer is None or odt != daq.next_odt:
            if daq.buffer is not None:
                self.lost += 1
            daq.buffer, daq.next_odt = None, 0
            return
        for entry in daq.odts[odt]:
            daq.buffer[entry.key][entry.offset : entry.offset + entry.size] = data[position : position + entry.size]
            position += entry.size
        daq.next_odt += 1
        if daq.next_odt == len(daq.odts):
            values = {key: bytes(value) for key, value in daq.buffer.items()}
            daq.buffer, daq.next_odt = None, 0
            self.samples += 1
            self.on_sample(DaqSample(daq.event, timestamp, values, daq.ecu_time))


from can_explorer.core.xcp_slave import XcpSlave, XcpSlaveSimulator  # noqa: E402,F401  (re-exported)
