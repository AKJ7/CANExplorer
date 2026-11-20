"""XCP (ASAM MCD-1 XCP): the master for measurement, calibration and programming of an ECU — memory access by
address, seed & key, calibration pages, checksums, DAQ measurement, STIM stimulation and flash programming (PGM) —
over CAN, CAN FD (`CanCommandClient`) or Ethernet (`core/xcp_ethernet.py`), and a simulated slave
(`core/xcp_slave.py`) for trying it out and for the tests.

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
- DAQ: dynamic (FREE_DAQ, ALLOC_*) and static (the slave's fixed lists: GET_DAQ_LIST_INFO, CLEAR_DAQ_LIST)
  configuration with SET_DAQ_PTR, WRITE_DAQ, SET_DAQ_LIST_MODE and START_STOP_*, all four identification field
  types and DAQ timestamps. Predefined lists (content fixed by the slave) are left alone.
- STIM: DAQ lists in the STIM direction; the master sends their DTOs and the slave writes them to its memory at
  the list's event.
- PGM: PROGRAM_START, PROGRAM_CLEAR (absolute mode), PROGRAM / PROGRAM_NEXT (also in master block mode), PROGRAM
  with no data to end the sequence, PROGRAM_RESET, GET_PGM_PROCESSOR_INFO and GET_SECTOR_INFO; the sequence for an
  image is `core/xcp_flash.py`. PROGRAM_PREPARE, PROGRAM_FORMAT, PROGRAM_MAX, PROGRAM_VERIFY and functional
  clearing are not sent.

PGM follows the open-source OpenBLT bootloader and pyxcp: PROGRAM with 0 elements ends the programming sequence,
and PROGRAM_CLEAR's range is a length from the MTA, sent here in address elements like the other lengths (bytes
for the byte-granularity slaves that bootloaders are).
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
    CLEAR_ABSOLUTE,
    CLEAR_DAQ_LIST,
    CLEAR_FUNCTIONAL,
    COMM_ADDRESS_GRANULARITY,
    COMM_BYTE_ORDER_MOTOROLA,
    COMM_MASTER_BLOCK_MODE,
    COMM_OPTIONAL,
    COMM_SLAVE_BLOCK_MODE,
    COMMAND_NAMES,
    CONNECT,
    COPY_CAL_PAGE,
    DAQ_BIT_STIM_SUPPORTED,
    DAQ_DYNAMIC,
    DAQ_LIST_DAQ,
    DAQ_LIST_EVENT_FIXED,
    DAQ_LIST_PREDEFINED,
    DAQ_LIST_STIM,
    DAQ_MODE_DIRECTION_STIM,
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
    EV_STIM_TIMEOUT,
    EVENT_CODES,
    EVENT_DAQ,
    EVENT_STIM,
    FREE_DAQ,
    GET_CAL_PAGE,
    GET_COMM_MODE_INFO,
    GET_DAQ_CLOCK,
    GET_DAQ_EVENT_INFO,
    GET_DAQ_LIST_INFO,
    GET_DAQ_PROCESSOR_INFO,
    GET_DAQ_RESOLUTION_INFO,
    GET_ID,
    GET_PAG_PROCESSOR_INFO,
    GET_PGM_PROCESSOR_INFO,
    GET_SECTOR_INFO,
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
    PGM_ABSOLUTE_MODE,
    PGM_COMPRESSION_REQUIRED,
    PGM_COMPRESSION_SUPPORTED,
    PGM_ENCRYPTION_REQUIRED,
    PGM_ENCRYPTION_SUPPORTED,
    PGM_FUNCTIONAL_MODE,
    PGM_MASTER_BLOCK_MODE,
    PGM_SLAVE_BLOCK_MODE,
    PROGRAM,
    PROGRAM_CLEAR,
    PROGRAM_FORMAT,
    PROGRAM_MAX,
    PROGRAM_NEXT,
    PROGRAM_PREPARE,
    PROGRAM_RESET,
    PROGRAM_START,
    PROGRAM_VERIFY,
    READ_DAQ,
    RES,
    RESOURCE_CALPAG,
    RESOURCE_DAQ,
    RESOURCE_PGM,
    RESOURCE_STIM,
    RESOURCES,
    SECTOR_LENGTH,
    SECTOR_NAME_LENGTH,
    SECTOR_START,
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


# Commands that use the MTA: after a timeout, SET_MTA goes before the repetition
_MTA_COMMANDS = {UPLOAD, DOWNLOAD, DOWNLOAD_NEXT, BUILD_CHECKSUM, PROGRAM_CLEAR, PROGRAM, PROGRAM_NEXT, PROGRAM_MAX}


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
        # Programming parameters from PROGRAM_START (they may differ from the normal ones)
        self.max_cto_pgm = 8
        self.pgm_master_block_mode = False
        self.pgm_slave_block_mode = False
        self.max_bs_pgm = 1
        self.min_st_pgm = 0  # In 100 µs
        self.events: List[XcpEvent] = []
        self.on_event: Optional[Callable[[XcpEvent], None]] = None
        self.event_listeners: List[Callable[[XcpEvent], None]] = []  # E.g. a measurement's STIM timeout handling
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
            for listener in list(self.event_listeners):
                listener(event)
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
        mta = self._mta if data[0] in _MTA_COMMANDS else None
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
        await self._transfer(
            DOWNLOAD, DOWNLOAD_NEXT, data, self.max_cto, self.master_block_mode, self.max_bs, self.min_st
        )

    async def _transfer(
        self,
        first: int,
        following: int,
        data: bytes,
        max_cto: int,
        block_mode: bool,
        max_bs: int,
        min_st: int,
        progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Send `data` from the MTA with DOWNLOAD/DOWNLOAD_NEXT or PROGRAM/PROGRAM_NEXT, in master blocks when the
        slave takes them. `progress` gets the bytes sent so far."""
        granularity = self.address_granularity
        header = 2 if granularity <= 2 else 4  # PID, n and, for dword granularity, two alignment bytes
        per_packet = (max_cto - header) // granularity
        if per_packet < 1:
            raise XcpError(f'MAX_CTO {max_cto} leaves no room for data')
        elements = len(data) // granularity
        offset = 0
        while offset < elements:
            if block_mode and max_bs > 1:
                count = min(elements - offset, per_packet * max_bs, 255)
                block = data[offset * granularity : (offset + count) * granularity]
                await self._send_block(first, following, block, header, per_packet, min_st)
            else:
                count = min(elements - offset, per_packet)
                chunk = data[offset * granularity : (offset + count) * granularity]
                await self._command(bytes([first, count]) + bytes(header - 2) + chunk)
                self._advance(count)
            offset += count
            if progress is not None:
                progress(offset * granularity)

    async def _send_block(
        self, first_command: int, following: int, block: bytes, header: int, per_packet: int, min_st: int
    ) -> None:
        """One master block: the first command with the first part, the `following` command with the rest; only
        the last packet is answered."""
        granularity = self.address_granularity
        remaining = len(block) // granularity
        mta = self._mta
        for attempt in range(self.retries + 1):
            self.transport.drain()
            offset, first, left = 0, True, remaining
            while left > 0:
                count = min(left, per_packet)
                chunk = block[offset * granularity : (offset + count) * granularity]
                self.transport.send(bytes([first_command if first else following, left]) + bytes(header - 2) + chunk)
                first, offset, left = False, offset + count, left - count
                if left and min_st:
                    await asyncio.sleep(min_st / 10000)
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
                name = _command_name(first_command)
                raise XcpError(f'XCP {name} (block): {ERROR_CODES.get(code, f"error 0x{code:02X}")}', code)
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
            'bit_stim_supported': bool(response[0] & DAQ_BIT_STIM_SUPPORTED),
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
            'stim_granularity': response[2] or 1,
            'max_stim_entry_size': response[3] or 0xFF,
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
            'daq': bool(response[0] & EVENT_DAQ),
            'stim': bool(response[0] & EVENT_STIM),
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

    async def get_daq_list_info(self, daq_list: int) -> Dict[str, object]:
        """What a DAQ list can do: static lists have a fixed number of ODTs and entries; predefined ones also a
        fixed content."""
        response = await self._command(bytes([GET_DAQ_LIST_INFO, 0]) + self._word(daq_list))
        properties = response[0]
        return {
            'number': daq_list,
            'properties': properties,
            'predefined': bool(properties & DAQ_LIST_PREDEFINED),
            'event_fixed': bool(properties & DAQ_LIST_EVENT_FIXED),
            'daq': bool(properties & DAQ_LIST_DAQ),
            'stim': bool(properties & DAQ_LIST_STIM),
            'max_odt': response[1],
            'max_odt_entries': response[2],
            'fixed_event': int.from_bytes(response[3:5], self.byte_order) if len(response) >= 5 else 0,
        }

    async def clear_daq_list(self, daq_list: int) -> None:
        """Clear the entries of a static DAQ list (also stops it)."""
        await self._command(bytes([CLEAR_DAQ_LIST, 0]) + self._word(daq_list))

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

    async def read_daq(self) -> Tuple[int, int, int, int]:
        """The ODT entry at the DAQ pointer: (address, extension, size in bytes, bit offset; 0xFF: whole elements)."""
        response = await self._command(bytes([READ_DAQ]))
        size = response[1] * self.address_granularity
        return int.from_bytes(response[3:7], self.byte_order), response[2], size, response[0]

    async def read_daq_list(
        self, daq_list: int, max_odt: int, max_odt_entries: int
    ) -> List[List[Tuple[int, int, int, int]]]:
        """The entries of a DAQ list (e.g. a predefined one), per ODT: (address, extension, size in bytes, bit
        offset), unused entries left out. The pointer is set before each READ_DAQ, whether or not the slave advances
        it."""
        content = []
        for odt in range(max_odt):
            entries = []
            for entry in range(max_odt_entries):
                await self.set_daq_ptr(daq_list, odt, entry)
                address, extension, size, bit = await self.read_daq()
                if size:
                    entries.append((address, extension, size, bit))
            content.append(entries)
        return content

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

    # Programming (PGM)

    async def program_start(self) -> Dict[str, object]:
        """Begin a programming sequence; the slave answers with its programming communication parameters."""
        response = await self._command(bytes([PROGRAM_START]))
        comm = response[1] if len(response) > 1 else 0
        limit = getattr(self.transport, 'max_payload', 8)
        self.max_cto_pgm = min(response[2] or self.max_cto, limit) if len(response) > 2 else self.max_cto
        self.max_bs_pgm = max(1, response[3]) if len(response) > 3 else 1
        self.min_st_pgm = response[4] if len(response) > 4 else 0
        self.pgm_master_block_mode = bool(comm & PGM_MASTER_BLOCK_MODE)
        self.pgm_slave_block_mode = bool(comm & PGM_SLAVE_BLOCK_MODE)
        return {
            'max_cto_pgm': self.max_cto_pgm,
            'master_block_mode': self.pgm_master_block_mode,
            'max_bs_pgm': self.max_bs_pgm,
            'min_st_pgm': self.min_st_pgm,
            'queue_size_pgm': response[5] if len(response) > 5 else 0,
        }

    async def program_prepare(self, address: int, size: int, extension: int = 0) -> None:
        """Before PROGRAM_START: announce `size` bytes of code (e.g. a flash kernel) that will be downloaded to
        volatile memory at `address`."""
        elements = self._elements(size)
        if elements > 0xFFFF:
            raise XcpError(f'{size} bytes of code do not fit PROGRAM_PREPARE (at most 65535 elements)')
        await self.set_mta(address, extension)
        await self._command(bytes([PROGRAM_PREPARE, 0]) + self._word(elements))

    async def program_clear(self, address: int, size: int, extension: int = 0, timeout: float = 10.0) -> None:
        """Erase `size` bytes of non-volatile memory from `address` (absolute mode). Erasing can take seconds."""
        await self.set_mta(address, extension)
        await self._command(
            bytes([PROGRAM_CLEAR, CLEAR_ABSOLUTE, 0, 0]) + self._elements(size).to_bytes(4, self.byte_order), timeout
        )

    async def program_clear_functional(self, areas: int, timeout: float = 30.0) -> None:
        """Erase functional areas (functional mode, no MTA): a bit mask of CLEAR_CALIBRATION_AREAS,
        CLEAR_CODE_AREAS, CLEAR_NVRAM_AREAS, or user-defined bits from 0x100."""
        await self._command(
            bytes([PROGRAM_CLEAR, CLEAR_FUNCTIONAL, 0, 0]) + (areas & 0xFFFFFFFF).to_bytes(4, self.byte_order), timeout
        )

    async def program_format(
        self, compression: int = 0, encryption: int = 0, programming: int = 0, access: int = 0
    ) -> None:
        """The format of the data of the following PROGRAM commands: the compression and encryption method the
        image was prepared with (0: none; 0x80-0xFF manufacturer defined), the programming method (0: sequential)
        and the access method (0: absolute addresses)."""
        await self._command(bytes([PROGRAM_FORMAT, compression, encryption, programming, access]))

    async def program_max(self, data: bytes) -> None:
        """One PROGRAM_MAX: exactly MAX_CTO_PGM - granularity bytes at the MTA (which advances)."""
        granularity = self.address_granularity
        size = self.max_cto_pgm - granularity
        if len(data) != size:
            raise XcpError(f'PROGRAM_MAX takes exactly {size} bytes')
        await self._command(bytes([PROGRAM_MAX]) + bytes(granularity - 1) + bytes(data))
        self._advance(size // granularity)

    async def program_verify(self, mode: int, kind: int = 0, value: int = 0, timeout: float = 10.0) -> None:
        """Ask the slave to check the new flash content: VERIFY_INTERNAL runs its own routine; VERIFY_VALUE sends
        `value` for it to compare. `kind` is a bit mask of VERIFY_CALIBRATION_AREAS, VERIFY_CODE_AREAS,
        VERIFY_COMPLETE_FLASH, or user-defined bits from 0x100."""
        await self._command(
            bytes([PROGRAM_VERIFY, mode]) + self._word(kind) + (value & 0xFFFFFFFF).to_bytes(4, self.byte_order),
            timeout,
        )

    async def program(
        self,
        address: int,
        data: bytes,
        extension: int = 0,
        progress: Optional[Callable[[int], None]] = None,
        use_max: bool = False,
    ) -> None:
        """Program `data` (a whole number of address elements) at `address`, in master blocks when available. With
        `use_max`, full packets go as PROGRAM_MAX and only the rest as PROGRAM."""
        data = bytes(data)
        if len(data) % self.address_granularity:
            raise XcpError(f'{len(data)} bytes is not a whole number of {self.address_granularity}-byte elements')
        await self.set_mta(address, extension)
        if use_max:
            size = self.max_cto_pgm - self.address_granularity
            full = len(data) - len(data) % size
            for offset in range(0, full, size):
                await self.program_max(data[offset : offset + size])
                if progress is not None:
                    progress(offset + size)
            data = data[full:]
            if not data:
                return
            done = full
            if progress is not None:
                outer = progress
                progress = lambda sent: outer(done + sent)  # noqa: E731
        await self._transfer(
            PROGRAM,
            PROGRAM_NEXT,
            data,
            self.max_cto_pgm,
            self.pgm_master_block_mode,
            self.max_bs_pgm,
            self.min_st_pgm,
            progress,
        )

    async def program_end(self) -> None:
        """PROGRAM with no data: the end of the memory segment programmed (the slave finishes writing it)."""
        await self._command(bytes([PROGRAM, 0]), timeout=10.0)

    async def program_reset(self, timeout: float = 0.5) -> None:
        """End programming and reset the slave. The slave may reset before it answers, so no answer is fine; the
        session ends either way."""
        try:
            response = await self.transport.request(bytes([PROGRAM_RESET]), timeout)
        except CanCommandTimeout:
            response = b''
        finally:
            self.connected = False
            self.dto_handler = None
        if response and response[0] == ERR:
            code = response[1] if len(response) > 1 else 0
            raise XcpError(f'XCP PROGRAM_RESET: {ERROR_CODES.get(code, f"error 0x{code:02X}")}', code)

    async def get_pgm_processor_info(self) -> Dict[str, object]:
        response = await self._command(bytes([GET_PGM_PROCESSOR_INFO]))
        properties = response[0] if response else 0
        return {
            'properties': properties,
            'absolute_mode': bool(properties & PGM_ABSOLUTE_MODE),
            'functional_mode': bool(properties & PGM_FUNCTIONAL_MODE),
            'compression_supported': bool(properties & PGM_COMPRESSION_SUPPORTED),
            'compression_required': bool(properties & PGM_COMPRESSION_REQUIRED),
            'encryption_supported': bool(properties & PGM_ENCRYPTION_SUPPORTED),
            'encryption_required': bool(properties & PGM_ENCRYPTION_REQUIRED),
            'max_sector': response[1] if len(response) > 1 else 0,
        }

    async def get_sector_info(self, sector: int) -> Dict[str, object]:
        """Start, length, clear and program sequence numbers, programming method and name of a flash sector."""
        start = await self._command(bytes([GET_SECTOR_INFO, SECTOR_START, sector]))
        length = await self._command(bytes([GET_SECTOR_INFO, SECTOR_LENGTH, sector]))
        name = ''
        try:
            response = await self._command(bytes([GET_SECTOR_INFO, SECTOR_NAME_LENGTH, sector]))
            if response and response[0]:
                self._mta = None
                name = (await self.upload(response[0])).decode('latin-1', 'replace')
        except XcpError:
            pass  # The name is optional
        return {
            'sector': sector,
            'start': int.from_bytes(start[3:7], self.byte_order),  # An address, like SET_MTA's
            'length': int.from_bytes(length[3:7], self.byte_order),  # As the slave reports it (bytes on byte slaves)
            'clear_sequence': start[0],
            'program_sequence': start[1],
            'method': start[2],
            'name': name,
        }

    async def sectors(self) -> List[Dict[str, object]]:
        """The flash sectors the slave describes (none when it does not support GET_SECTOR_INFO)."""
        info = await self.get_pgm_processor_info()
        sectors = []
        for sector in range(info['max_sector']):
            sectors.append(await self.get_sector_info(sector))
        return sectors

    # The common calibration interface (shared with CcpClient)

    async def start_measurement(
        self,
        signals: Sequence['DaqSignal'],
        on_sample: Optional[Callable[['DaqSample'], None]],
        prescaler: int = 1,
        timestamps: bool = True,
        stimuli: Sequence['DaqSignal'] = (),
        stim_period: float = 0.0,
        predefined: Optional[Dict[int, Sequence[Sequence[Tuple[int, ...]]]]] = None,
        on_stim_timeout: Optional[Callable[[XcpEvent], None]] = None,
    ) -> 'XcpMeasurement':
        """Measure `signals` with DAQ and, with `stimuli`, stimulate those memory ranges with STIM
        (`XcpMeasurement.stimulate`); both share one DAQ configuration. `predefined` is the content of the slave's
        predefined DAQ lists, when known (e.g. `A2lDocument.predefined_daq_lists`)."""
        measurement = XcpMeasurement(
            self, signals, on_sample, prescaler, timestamps, stimuli, stim_period, predefined, on_stim_timeout
        )
        await measurement.start()
        return measurement

    async def start_stimulation(self, stimuli: Sequence['DaqSignal'], period: float = 0.0) -> 'XcpMeasurement':
        """STIM alone: the returned measurement's `stimulate(values)` sends new values to the slave."""
        return await self.start_measurement([], None, stimuli=stimuli, stim_period=period)


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
    stim: bool = False  # Direction: the master sends this list's DTOs
    first_pid: int = 0
    buffer: Optional[Dict[str, bytearray]] = None
    next_odt: int = 0
    ecu_time: Optional[float] = None
    last_raw: Optional[int] = None
    wraps: int = 0
    values: Dict[str, bytearray] = field(default_factory=dict)  # STIM: the values sent
    predefined: bool = False  # The slave's fixed content: `views` picks the signals out of its entries
    views: Optional[Dict[str, List[Tuple[str, int]]]] = None  # Signal -> (entry key, byte in it) per byte


def pack_odts(
    signals: Sequence[Tuple[str, int, int, int]],
    first_capacity: int,
    capacity: int,
    max_entry: int,
    granularity: int = 1,
    address_granularity: int = 1,
    max_entries: int = 0,
) -> List[List[_Entry]]:
    """Pack (key, address, extension, size) memory ranges into ODTs of `capacity` bytes (`first_capacity` for the
    first, which carries the timestamp) and at most `max_entries` entries (0: no limit), splitting a range over
    entries and ODTs when it does not fit."""
    odts: List[List[_Entry]] = [[]]
    used = 0
    limit = first_capacity
    for key, address, extension, size in signals:
        offset = 0
        while offset < size:
            space = limit - used
            take = min(size - offset, space, max_entry)
            take -= take % max(granularity, address_granularity)
            if take <= 0 or (max_entries and len(odts[-1]) >= max_entries):
                if take <= 0 and not odts[-1] and limit == capacity:
                    raise XcpError(f'{key}: an ODT of {capacity} bytes cannot hold an entry')
                odts.append([])
                used, limit = 0, capacity
                continue
            odts[-1].append(_Entry(key, offset, address + offset // address_granularity, extension, take))
            offset += take
            used += take
    return [odt for odt in odts if odt]


class XcpMeasurement:
    """DAQ and STIM: one DAQ list per event channel and direction. `on_sample` gets each complete DAQ sample;
    `stimulate` sends new values for the `stimuli`.

    A slave with dynamic DAQ gets lists allocated for exactly these signals. A slave with static DAQ has a fixed
    set of lists, each with a fixed number of ODTs and entries (GET_DAQ_LIST_INFO): each event and direction takes
    the first free list that is not predefined, can run in that direction and on that event, and is large enough.

    Predefined lists (0 … MIN_DAQ - 1, either configuration) have a content the slave fixes. Their content comes
    from `predefined` (list number -> ODTs of (address, extension, size in bytes[, bit offset]) entries, e.g. from
    the A2L's DAQ_LIST PREDEFINED) or is read from the slave with READ_DAQ; a DAQ signal whose every byte such a
    list carries, on its event, is measured with it instead of a list configured for it.

    When the slave reports a STIM timeout (EV_STIM_TIMEOUT), the STIM data are sent again at once.
    """

    def __init__(
        self,
        client: XcpClient,
        signals: Sequence[DaqSignal],
        on_sample: Optional[Callable[[DaqSample], None]],
        prescaler: int = 1,
        timestamps: bool = True,
        stimuli: Sequence[DaqSignal] = (),
        stim_period: float = 0.0,
        predefined: Optional[Dict[int, Sequence[Sequence[Tuple[int, ...]]]]] = None,
        on_stim_timeout: Optional[Callable[[XcpEvent], None]] = None,
    ):
        self.client = client
        self.signals = list(signals)
        self.stimuli = list(stimuli)
        self.on_sample = on_sample
        self.prescaler = max(1, prescaler)
        self.timestamps = timestamps
        self.stim_period = stim_period
        self.lists: List[_DaqList] = []
        self.static = False  # The slave's DAQ configuration is static
        self.identification = ID_FIELD_ABSOLUTE
        self.timestamp_size = 0
        self.timestamp_resolution = 1e-6
        self.running = False
        self.samples = 0
        self.lost = 0  # Samples dropped because a DTO was missing
        self.stimulations = 0  # STIM DTO sets sent
        self.stim_timeouts = 0  # EV_STIM_TIMEOUT events received
        self.predefined = dict(predefined or {})
        self.on_stim_timeout = on_stim_timeout
        self._repeat: Optional[asyncio.Task] = None

    @property
    def id_size(self) -> int:
        return {ID_FIELD_ABSOLUTE: 1, ID_FIELD_RELATIVE_BYTE: 2, ID_FIELD_RELATIVE_WORD: 3}.get(self.identification, 4)

    def _plan(self, daq_signals: Sequence[DaqSignal]) -> List[Tuple[bool, int, List[DaqSignal]]]:
        """(STIM, event, signals) of each DAQ list needed: one per event channel and direction."""
        plan = []
        for stim, signals in ((False, daq_signals), (True, self.stimuli)):
            by_event: Dict[int, List[DaqSignal]] = {}
            for signal in signals:
                by_event.setdefault(signal.event, []).append(signal)
            plan += [(stim, event, group) for event, group in sorted(by_event.items())]
        return plan

    def _pack(self, stim: bool, signals: Sequence[DaqSignal], resolution, max_entries: int = 0) -> List[List[_Entry]]:
        capacity = self.client.max_dto - self.id_size
        if stim:  # The master sends no timestamps
            first, granularity = capacity, resolution['stim_granularity']
            max_entry = resolution['max_stim_entry_size'] * granularity
        else:
            first, granularity = capacity - self.timestamp_size, resolution['granularity']
            max_entry = resolution['max_entry_size'] * granularity
        return pack_odts(
            [(s.key, s.address, s.extension, s.size) for s in signals],
            first,
            capacity,
            max_entry,
            granularity,
            self.client.address_granularity,
            max_entries,
        )

    async def start(self) -> None:
        client = self.client
        if not self.signals and not self.stimuli:
            raise XcpError('Nothing to measure or stimulate')
        if self.stimuli and client.resources and not client.resources & RESOURCE_STIM:
            raise XcpError('The slave does not offer STIM (CONNECT resources)')
        keys = [s.key for s in self.signals + self.stimuli]
        if len(set(keys)) != len(keys):
            raise XcpError('Each measured or stimulated signal needs its own key')
        info = await client.get_daq_processor_info()
        resolution = await client.get_daq_resolution_info()
        self.identification = info['identification_field']
        self.static = not info['dynamic']
        if self.signals and info['timestamp_supported'] and (self.timestamps or resolution['timestamp_fixed']):
            self.timestamp_size = resolution['timestamp_size']
            self.timestamp_resolution = resolution['timestamp_resolution']
        self.lists = []
        remaining = await self._use_predefined(info) if info['min_daq'] and self.signals else list(self.signals)
        plan = self._plan(remaining)
        if plan and self.static:
            await self._configure_static(info, resolution, plan)
        elif plan:
            await self._configure_dynamic(info, resolution, plan)
        for daq in self.lists:
            if daq.stim:  # Start from the slave's current values, so a partial update keeps the others
                for signal in self.stimuli:
                    if signal.event == daq.event:
                        data = await client.read(signal.address, signal.extension, signal.size)
                        daq.values[signal.key] = bytearray(data)
        for daq in self.lists:
            mode = DAQ_MODE_DIRECTION_STIM if daq.stim else (DAQ_MODE_TIMESTAMP if self.timestamp_size else 0)
            await client.set_daq_list_mode(daq.number, daq.event, mode, 1 if daq.stim else self.prescaler)
            daq.first_pid = await client.start_stop_daq_list(DAQ_SELECT, daq.number)
        if self.signals:
            client.dto_handler = self._on_dto
        if self.stimuli:
            client.event_listeners.append(self._on_event)
        await client.start_stop_synch(SYNCH_START_SELECTED)
        self.running = True
        for daq in self.lists:
            if daq.stim:
                self._send_stim(daq)
        if self.stimuli and self.stim_period > 0:
            self._repeat = asyncio.get_running_loop().create_task(self._run_repeat(), name='XCP STIM')

    async def _configure_dynamic(self, info, resolution, plan) -> None:
        client = self.client
        if info['max_daq'] and len(plan) > info['max_daq'] - info['min_daq']:
            raise XcpError(f'{len(plan)} event channels and directions need more DAQ lists than the slave has')
        allocated = []
        for index, (stim, event, signals) in enumerate(plan):
            daq = _DaqList(info['min_daq'] + index, event, stim=stim)
            daq.odts = self._pack(stim, signals, resolution)
            daq.sizes = {s.key: s.size for s in signals}
            allocated.append(daq)
        await client.free_daq()  # Frees the dynamic lists only; predefined ones stay
        await client.alloc_daq(len(allocated))
        for daq in allocated:
            await client.alloc_odt(daq.number, len(daq.odts))
        for daq in allocated:
            for odt, entries in enumerate(daq.odts):
                await client.alloc_odt_entry(daq.number, odt, len(entries))
        self.lists += allocated
        await self._write_entries(allocated)

    async def _configure_static(self, info, resolution, plan) -> None:
        client = self.client
        free = []
        for number in range(info['min_daq'], info['max_daq']):  # The predefined lists are 0 … MIN_DAQ - 1
            list_info = await client.get_daq_list_info(number)
            if not list_info['predefined']:
                free.append(list_info)
        configured = []
        for stim, event, signals in plan:
            direction = 'stim' if stim else 'daq'
            chosen = None
            for candidate in free:
                if not candidate[direction] or (candidate['event_fixed'] and candidate['fixed_event'] != event):
                    continue
                odts = self._pack(stim, signals, resolution, candidate['max_odt_entries'])
                if len(odts) <= candidate['max_odt']:
                    chosen = candidate
                    break
            if chosen is None:
                names = ', '.join(s.key for s in signals)
                raise XcpError(
                    f'No free static DAQ list can take {direction.upper()} on event {event} ({names}): the slave '
                    f'has too few lists, or none large enough, for this direction and event'
                )
            free.remove(chosen)
            configured.append(_DaqList(chosen['number'], event, odts, {s.key: s.size for s in signals}, stim=stim))
        for daq in configured:
            await client.clear_daq_list(daq.number)
        self.lists += configured
        await self._write_entries(configured)

    async def _use_predefined(self, info) -> List[DaqSignal]:
        """Measure what the predefined lists carry with them; returns the DAQ signals they do not carry."""
        client = self.client
        granularity = client.address_granularity
        remaining = list(self.signals)
        for number in range(info['min_daq']):
            try:
                list_info = await client.get_daq_list_info(number)
            except XcpError:
                break  # GET_DAQ_LIST_INFO is optional: without it, nothing is known about the lists
            if not list_info['daq']:
                continue
            content = self._predefined_content(number) or await self._read_content(number, list_info)
            odts: List[List[_Entry]] = []
            carried: Dict[Tuple[int, int], Tuple[str, int]] = {}  # (extension, byte address) -> (entry, byte)
            for odt, entries in enumerate(content):
                row = []
                for index, (address, extension, size, bit) in enumerate(entries):
                    key = f'#{number}.{odt}.{index}'
                    row.append(_Entry(key, 0, address, extension, size))
                    if bit == 0xFF:  # A bit entry carries a bit's value, not the bytes at its address
                        for byte in range(size):
                            carried[(extension, address * granularity + byte)] = (key, byte)
                odts.append(row)
            while odts and not odts[-1]:
                odts.pop()
            if not carried:
                continue
            fixed = list_info['fixed_event'] if list_info['event_fixed'] else None
            views: Dict[str, List[Tuple[str, int]]] = {}
            event = fixed
            for signal in remaining:
                if event is not None and signal.event != event:
                    continue
                view = [carried.get((signal.extension, signal.address * granularity + b)) for b in range(signal.size)]
                if all(view):
                    views[signal.key] = view
                    event = signal.event
            if not views:
                continue
            daq = _DaqList(number, event, odts, predefined=True, views=views)
            daq.sizes = {entry.key: entry.size for row in odts for entry in row}
            self.lists.append(daq)
            remaining = [s for s in remaining if s.key not in views]
        return remaining

    def _predefined_content(self, number: int) -> List[List[Tuple[int, int, int, int]]]:
        content = self.predefined.get(number)
        if not content:
            return []
        return [[(e[0], e[1], e[2], e[3] if len(e) > 3 else 0xFF) for e in odt] for odt in content]

    async def _read_content(self, number: int, list_info) -> List[List[Tuple[int, int, int, int]]]:
        try:
            return await self.client.read_daq_list(number, list_info['max_odt'], list_info['max_odt_entries'])
        except XcpError:
            return []  # READ_DAQ is optional

    async def _write_entries(self, lists: Sequence[_DaqList]) -> None:
        client = self.client
        granularity = client.address_granularity
        for daq in lists:
            for odt, entries in enumerate(daq.odts):
                await client.set_daq_ptr(daq.number, odt, 0)
                for entry in entries:
                    await client.write_daq(entry.address, entry.size // granularity, entry.extension)

    async def stop(self) -> None:
        self.running = False
        if self._on_event in self.client.event_listeners:
            self.client.event_listeners.remove(self._on_event)
        if self._repeat is not None:
            self._repeat.cancel()
            self._repeat = None
        try:
            await self.client.start_stop_synch(SYNCH_STOP_ALL)
        finally:
            if self.client.dto_handler == self._on_dto:
                self.client.dto_handler = None

    # STIM

    def _header(self, daq: _DaqList, odt: int) -> bytes:
        """The identification field of a DTO of `daq`'s ODT `odt`."""
        order = self.client.byte_order
        if self.identification == ID_FIELD_ABSOLUTE:
            return bytes([daq.first_pid + odt])
        if self.identification == ID_FIELD_RELATIVE_BYTE:
            return bytes([odt, daq.number & 0xFF])
        if self.identification == ID_FIELD_RELATIVE_WORD:
            return bytes([odt]) + daq.number.to_bytes(2, order)
        return bytes([odt, 0]) + daq.number.to_bytes(2, order)

    def _send_stim(self, daq: _DaqList) -> None:
        for odt, entries in enumerate(daq.odts):
            payload = self._header(daq, odt)
            for entry in entries:
                payload += bytes(daq.values[entry.key][entry.offset : entry.offset + entry.size])
            self.client.transport.send(payload)
        self.stimulations += 1

    @property
    def stimuli_keys(self) -> List[str]:
        return [s.key for s in self.stimuli]

    def stimulus(self, key: str) -> bytes:
        """The raw value last sent for a stimulated signal (at the start: the slave's value)."""
        daq = next((d for d in self.lists if d.stim and key in d.values), None)
        if daq is None:
            raise XcpError(f'{key} is not stimulated')
        return bytes(daq.values[key])

    def stimulate(self, values: Dict[str, bytes]) -> None:
        """Send new raw values for stimulated signals (by key, each exactly its size); the slave writes them to its
        memory at the list's next event. The other signals of the same list keep their last value."""
        if not self.running:
            raise XcpError('STIM is not running')
        touched = []
        for key, value in values.items():
            daq = next((d for d in self.lists if d.stim and key in d.sizes), None)
            if daq is None:
                raise XcpError(f'{key} is not stimulated')
            value = bytes(value)
            if len(value) != daq.sizes[key]:
                raise XcpError(f'{key} takes {daq.sizes[key]} bytes, not {len(value)}')
            daq.values[key][:] = value
            if daq not in touched:
                touched.append(daq)
        for daq in touched:
            self._send_stim(daq)

    def _on_event(self, event: XcpEvent) -> None:
        """A STIM timeout: the slave missed STIM data at an event; send all of them again at once. (Which list or
        event the timeout is for is in its data, whose layout is not public; it is not needed for this.)"""
        if event.code != EV_STIM_TIMEOUT or not self.running:
            return
        self.stim_timeouts += 1
        for daq in self.lists:
            if daq.stim:
                self._send_stim(daq)
        if self.on_stim_timeout is not None:
            self.on_stim_timeout(event)

    async def _run_repeat(self) -> None:
        """Send every STIM list again each `stim_period`, for slaves that expect data before each event."""
        while True:
            await asyncio.sleep(self.stim_period)
            for daq in self.lists:
                if daq.stim:
                    self._send_stim(daq)

    def _locate(self, data: bytes) -> Optional[Tuple[_DaqList, int]]:
        if self.identification == ID_FIELD_ABSOLUTE:
            pid = data[0]
            for daq in self.lists:
                if not daq.stim and daq.first_pid <= pid < daq.first_pid + len(daq.odts):
                    return daq, pid - daq.first_pid
            return None
        odt = data[0]
        if self.identification == ID_FIELD_RELATIVE_BYTE:
            number = data[1]
        elif self.identification == ID_FIELD_RELATIVE_WORD:
            number = int.from_bytes(data[1:3], self.client.byte_order)
        else:
            number = int.from_bytes(data[2:4], self.client.byte_order)
        daq = next((d for d in self.lists if d.number == number and not d.stim), None)
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
        while daq.next_odt < len(daq.odts) and not daq.odts[daq.next_odt]:
            daq.next_odt += 1  # An empty ODT of a predefined list, which the slave need not send
        if daq.next_odt == len(daq.odts):
            if daq.views is not None:
                buffer = daq.buffer
                values = {key: bytes(buffer[e][b] for e, b in view) for key, view in daq.views.items()}
            else:
                values = {key: bytes(value) for key, value in daq.buffer.items()}
            daq.buffer, daq.next_odt = None, 0
            self.samples += 1
            if self.on_sample is not None:
                self.on_sample(DaqSample(daq.event, timestamp, values, daq.ecu_time))


from can_explorer.core.xcp_slave import XcpSlave, XcpSlaveSimulator  # noqa: E402,F401  (re-exported)
