"""XCP on CAN (ASAM MCD-1 XCP): the master commands used for measurement and calibration — reading and writing an
ECU's memory by address — and a simulated slave for trying it out and for the tests.

XCP is a command/response protocol: the master sends a Command Transfer Object (CTO) on the request identifier and
the slave answers on the response identifier. Only the polling commands needed to read a measurement and read or
write a calibration value are implemented (CONNECT, GET_STATUS, SET_MTA, UPLOAD, SHORT_UPLOAD, DOWNLOAD); cyclic
DAQ measurement is not.
"""

from typing import Optional

import can

from can_explorer.core.can_command import CanCommandClient
from can_explorer.core.session import BusSession

# Command codes (master -> slave)
CONNECT = 0xFF
DISCONNECT = 0xFE
GET_STATUS = 0xFD
GET_COMM_MODE_INFO = 0xFB
GET_ID = 0xFA
SET_MTA = 0xF6
UPLOAD = 0xF5
SHORT_UPLOAD = 0xF4
DOWNLOAD = 0xF0

# Response packet identifiers (slave -> master)
RES = 0xFF  # Positive response
ERR = 0xFE  # Error

ERROR_CODES = {
    0x00: 'cmd synchronisation',
    0x10: 'command busy',
    0x11: 'DAQ active',
    0x12: 'program active',
    0x20: 'command unknown',
    0x21: 'command syntax',
    0x22: 'address out of range',
    0x23: 'access denied',
    0x24: 'access locked',
    0x25: 'page not valid',
    0x26: 'mode not valid',
    0x27: 'segment not valid',
    0x28: 'sequence error',
    0x29: 'DAQ configuration',
    0x30: 'memory overflow',
    0x31: 'generic error',
    0x32: 'verify failed',
}


class XcpError(Exception):
    pass


class XcpClient:
    """XCP master over a CAN command transport. `read`/`write` access the slave's memory by address."""

    def __init__(self, transport: CanCommandClient):
        self.transport = transport
        self.connected = False
        self.byte_order = 'little'  # Of addresses and multi-byte fields, from the CONNECT response
        self.max_cto = 8
        self.max_dto = 8

    async def _command(self, data: bytes, timeout: Optional[float] = None) -> bytes:
        response = await self.transport.request(data, timeout)
        if not response:
            raise XcpError('Empty XCP response')
        if response[0] == ERR:
            code = response[1] if len(response) > 1 else 0
            raise XcpError(f'XCP error: {ERROR_CODES.get(code, f"0x{code:02X}")}')
        if response[0] != RES:
            raise XcpError(f'Unexpected XCP response 0x{response[0]:02X}')
        return response[1:]

    async def connect(self) -> bytes:
        response = await self._command(bytes([CONNECT, 0x00]))
        if len(response) >= 2:
            self.byte_order = 'big' if response[1] & 0x01 else 'little'
        if len(response) >= 3:
            self.max_cto = response[2] or 8
        if len(response) >= 5:
            self.max_dto = int.from_bytes(response[3:5], self.byte_order) or 8
        self.connected = True
        return response

    async def disconnect(self) -> None:
        try:
            await self._command(bytes([DISCONNECT]))
        finally:
            self.connected = False

    async def get_status(self) -> bytes:
        return await self._command(bytes([GET_STATUS]))

    async def set_mta(self, address: int, extension: int = 0) -> None:
        await self._command(bytes([SET_MTA, 0, 0, extension]) + address.to_bytes(4, self.byte_order))

    async def upload(self, size: int) -> bytes:
        """Read `size` bytes from the current MTA (which advances), several CTOs if needed."""
        data = bytearray()
        while len(data) < size:
            chunk = min(size - len(data), self.max_cto - 1)
            data += (await self._command(bytes([UPLOAD, chunk])))[:chunk]
        return bytes(data[:size])

    async def short_upload(self, address: int, size: int, extension: int = 0) -> bytes:
        if size <= self.max_cto - 1:
            response = await self._command(
                bytes([SHORT_UPLOAD, size, 0, extension]) + address.to_bytes(4, self.byte_order)
            )
            return response[:size]
        await self.set_mta(address, extension)
        return await self.upload(size)

    async def download(self, address: int, data: bytes, extension: int = 0) -> None:
        await self.set_mta(address, extension)
        offset = 0
        while offset < len(data):
            chunk = min(len(data) - offset, self.max_cto - 2)
            await self._command(bytes([DOWNLOAD, chunk]) + data[offset : offset + chunk])
            offset += chunk

    # The common calibration interface (shared with CcpClient)

    async def read(self, address: int, extension: int, size: int) -> bytes:
        return await self.short_upload(address, size, extension)

    async def write(self, address: int, extension: int, data: bytes) -> None:
        await self.download(address, data, extension)


class XcpSlaveSimulator:
    """A simulated XCP slave on a session, backed by a byte array addressed from 0. For demos and tests."""

    def __init__(
        self,
        session: BusSession,
        request_id: int = 0x7E0,
        response_id: int = 0x7E1,
        extended: bool = False,
        byte_order: str = 'little',
        memory: Optional[bytes] = None,
        size: int = 0x10000,
    ):
        self.session = session
        self.request_id = request_id
        self.response_id = response_id
        self.extended = extended
        self.byte_order = byte_order
        self.memory = bytearray(memory if memory is not None else bytes(size))
        self.connected = False
        self.mta = 0
        self.commands: list = []
        session.add_message_listener(self._on_message)

    def stop(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def _send(self, data: bytes) -> None:
        self.session.send(
            can.Message(
                arbitration_id=self.response_id, is_extended_id=self.extended, data=bytes(data).ljust(8, b'\x00')
            )
        )

    def _error(self, code: int) -> None:
        self._send(bytes([ERR, code]))

    def _read(self, address: int, size: int) -> Optional[bytes]:
        if address < 0 or address + size > len(self.memory):
            return None
        return bytes(self.memory[address : address + size])

    def _on_message(self, msg: can.Message) -> None:
        if (
            not msg.is_rx
            or msg.arbitration_id != self.request_id
            or msg.is_extended_id != self.extended
            or not msg.data
        ):
            return
        data = bytes(msg.data)
        command = data[0]
        self.commands.append(data)
        if command == CONNECT:
            self.connected = True
            self._send(bytes([RES, 0x00, 0x01 if self.byte_order == 'big' else 0x00, 8, 8, 0, 1, 1]))
        elif command == DISCONNECT:
            self.connected = False
            self._send(bytes([RES]))
        elif command == GET_STATUS:
            self._send(bytes([RES, 0, 0, 0, 0, 0]))
        elif command == SET_MTA:
            self.mta = int.from_bytes(data[4:8], self.byte_order)
            self._send(bytes([RES]))
        elif command == UPLOAD:
            chunk = self._read(self.mta, data[1])
            if chunk is None:
                self._error(0x22)
            else:
                self.mta += data[1]
                self._send(bytes([RES]) + chunk)
        elif command == SHORT_UPLOAD:
            address = int.from_bytes(data[4:8], self.byte_order)
            chunk = self._read(address, data[1])
            if chunk is None:
                self._error(0x22)
            else:
                self.mta = address + data[1]
                self._send(bytes([RES]) + chunk)
        elif command == DOWNLOAD:
            size = data[1]
            if self.mta + size > len(self.memory):
                self._error(0x22)
            else:
                self.memory[self.mta : self.mta + size] = data[2 : 2 + size]
                self.mta += size
                self._send(bytes([RES]))
        else:
            self._error(0x20)  # Command unknown
