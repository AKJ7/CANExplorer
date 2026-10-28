"""CCP, the CAN Calibration Protocol (ASAM MCD-1 CCP): the master commands for reading and writing an ECU's memory
by address, and a simulated slave.

CCP is a command/response protocol like XCP but older: the master sends a Command Receive Object (CRO) with a
command code and a rolling command counter, and the slave answers with a Command Return Message (CRM). Only the
commands needed for polling measurement and calibration are implemented (CONNECT, GET_CCP_VERSION, SET_MTA, UPLOAD,
SHORT_UP, DNLOAD, DISCONNECT).
"""

from typing import Optional

import can

from can_explorer.core.can_command import CanCommandClient
from can_explorer.core.session import BusSession

# Command codes
CONNECT = 0x01
SET_MTA = 0x02
DNLOAD = 0x03
UPLOAD = 0x04
DISCONNECT = 0x07
SHORT_UP = 0x0F
GET_CCP_VERSION = 0x1B
EXCHANGE_ID = 0x17

CRM = 0xFF  # Command return message packet id
ACKNOWLEDGE = 0x00
MAX_DATA = 5  # Data bytes that fit in a CRO/CRM after the header

RETURN_CODES = {
    0x00: 'acknowledge',
    0x10: 'command busy',
    0x11: 'DAQ busy',
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
    0x35: 'access locked',
    0x36: 'resource/function not available',
}


class CcpError(Exception):
    pass


class CcpClient:
    """CCP master over a CAN command transport. `read`/`write` access the slave's memory by address."""

    def __init__(self, transport: CanCommandClient, station_address: int = 0, byte_order: str = 'big'):
        self.transport = transport
        self.station_address = station_address
        self.byte_order = byte_order  # Of addresses and multi-byte fields in the ECU
        self.connected = False
        self._counter = 0

    def _next_counter(self) -> int:
        self._counter = (self._counter + 1) & 0xFF
        return self._counter

    async def _command(self, code: int, params: bytes = b'', timeout: Optional[float] = None) -> bytes:
        counter = self._next_counter()
        response = await self.transport.request(bytes([code, counter]) + bytes(params), timeout)
        if len(response) < 3:
            raise CcpError('Short CCP response')
        if response[0] != CRM:
            raise CcpError(f'Unexpected CCP packet 0x{response[0]:02X}')
        if response[1] != ACKNOWLEDGE:
            raise CcpError(f'CCP error: {RETURN_CODES.get(response[1], f"0x{response[1]:02X}")}')
        return response[3:]

    async def connect(self) -> None:
        await self._command(CONNECT, self.station_address.to_bytes(2, 'little'))
        self.connected = True

    async def disconnect(self, permanent: bool = False) -> None:
        try:
            await self._command(
                DISCONNECT, bytes([0x01 if permanent else 0x00, 0]) + self.station_address.to_bytes(2, 'little')
            )
        finally:
            self.connected = False

    async def get_version(self, major: int = 2, minor: int = 1) -> bytes:
        return await self._command(GET_CCP_VERSION, bytes([major, minor]))

    async def set_mta(self, address: int, extension: int = 0, mta: int = 0) -> None:
        await self._command(SET_MTA, bytes([mta, extension]) + address.to_bytes(4, self.byte_order))

    async def upload(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = min(size - len(data), MAX_DATA)
            data += (await self._command(UPLOAD, bytes([chunk])))[:chunk]
        return bytes(data[:size])

    async def short_up(self, address: int, size: int, extension: int = 0) -> bytes:
        if size <= MAX_DATA:
            response = await self._command(SHORT_UP, bytes([size, extension]) + address.to_bytes(4, self.byte_order))
            return response[:size]
        await self.set_mta(address, extension)
        return await self.upload(size)

    async def dnload(self, address: int, data: bytes, extension: int = 0) -> None:
        await self.set_mta(address, extension)
        offset = 0
        while offset < len(data):
            chunk = min(len(data) - offset, MAX_DATA)
            await self._command(DNLOAD, bytes([chunk]) + data[offset : offset + chunk])
            offset += chunk

    # The common calibration interface (shared with XcpClient)

    async def read(self, address: int, extension: int, size: int) -> bytes:
        return await self.short_up(address, size, extension)

    async def write(self, address: int, extension: int, data: bytes) -> None:
        await self.dnload(address, data, extension)


class CcpSlaveSimulator:
    """A simulated CCP slave on a session, backed by a byte array addressed from 0."""

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
    ):
        self.session = session
        self.request_id = request_id
        self.response_id = response_id
        self.extended = extended
        self.station_address = station_address
        self.byte_order = byte_order
        self.memory = bytearray(memory if memory is not None else bytes(size))
        self.connected = False
        self.mta = 0
        session.add_message_listener(self._on_message)

    def stop(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def _crm(self, counter: int, data: bytes = b'', code: int = ACKNOWLEDGE) -> None:
        self.session.send(
            can.Message(
                arbitration_id=self.response_id,
                is_extended_id=self.extended,
                data=(bytes([CRM, code, counter]) + bytes(data)).ljust(8, b'\x00'),
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
        data = bytes(msg.data)
        if len(data) < 2:
            return
        command, counter = data[0], data[1]
        if command == CONNECT:
            if int.from_bytes(data[2:4], 'little') == self.station_address:
                self.connected = True
                self._crm(counter)
            # A CONNECT to another station address is ignored (shared bus)
        elif command == DISCONNECT:
            self.connected = False
            self._crm(counter)
        elif command == GET_CCP_VERSION:
            self._crm(counter, bytes([2, 1]))
        elif command == SET_MTA:
            self.mta = int.from_bytes(data[4:8], self.byte_order)
            self._crm(counter, bytes([data[3]]) + data[4:8])
        elif command == UPLOAD:
            size = data[2]
            if self.mta + size > len(self.memory):
                self._crm(counter, code=0x32)
            else:
                chunk = bytes(self.memory[self.mta : self.mta + size])
                self.mta += size
                self._crm(counter, chunk)
        elif command == SHORT_UP:
            size = data[2]
            address = int.from_bytes(data[4:8], self.byte_order)
            if address + size > len(self.memory):
                self._crm(counter, code=0x32)
            else:
                self.mta = address
                self._crm(counter, bytes(self.memory[address : address + size]))
        elif command == DNLOAD:
            size = data[2]
            if self.mta + size > len(self.memory):
                self._crm(counter, code=0x32)
            else:
                self.memory[self.mta : self.mta + size] = data[3 : 3 + size]
                self.mta += size
                self._crm(counter, bytes([0]) + self.mta.to_bytes(4, self.byte_order))
        else:
            self._crm(counter, code=0x30)  # Command unknown
