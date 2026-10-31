"""SAE J1850 VPW and GM "Class 2" serial data — the 10.4 kbit/s single-wire bus of 1996–2007 GM vehicles.

The P01/P59 powertrain control modules of 1999–2006 GM trucks, vans and F-bodies (and many other GM cars of the
era) talk over J1850 VPW, not CAN. It is a different physical and data-link layer: a variable-pulse-width bus with
a three-byte header (priority, target address, source address), a payload and an SAE J1850 CRC. python-can has no
backend for it, so this is a self-contained protocol subsystem rather than something layered on a `BusSession`.

The service layer on top is SAE J1979 (OBD-II) with GM's Class 2 addressing: mode $01 current data, $03/$04 stored
trouble codes, $09 vehicle information, and so on; the positive response mode is the request mode plus 0x40, and a
negative response is `7F <mode> <code>`. OBD-II PID definitions are shared with `reverse.obd` so values decode the
same way everywhere.

`J1850Client` works over any transport offering `send(message)` and `receive(timeout)` coroutines. A live transport
would wrap an ELM327/OBDLink adapter in VPW mode or a J2534 pass-thru device; `J1850Bus` is an in-memory bus so a
`Class2EcuSimulator` can answer a client without any hardware, which is what the tests and the simulation use.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from can_explorer.core.frames import format_hex
from can_explorer.core.reverse.obd import PIDS_BY_NUMBER

# SAE J1979 diagnostic modes (request mode -> name); the positive response mode is the request plus 0x40.
SERVICES = {
    0x01: 'ShowCurrentData',
    0x02: 'ShowFreezeFrameData',
    0x03: 'ShowStoredDTCs',
    0x04: 'ClearDTCs',
    0x05: 'OxygenSensorTestResults',
    0x06: 'OnBoardMonitoringTestResults',
    0x07: 'ShowPendingDTCs',
    0x08: 'ControlOnBoardComponent',
    0x09: 'RequestVehicleInformation',
    0x0A: 'ShowPermanentDTCs',
}

# Negative response codes (shared with KWP2000, the subset seen on Class 2)
NEGATIVE_RESPONSE_CODES = {
    0x10: 'generalReject',
    0x11: 'serviceNotSupported',
    0x12: 'subFunctionNotSupported',
    0x21: 'busyRepeatRequest',
    0x22: 'conditionsNotCorrect',
    0x31: 'requestOutOfRange',
    0x78: 'requestCorrectlyReceived-ResponsePending',
}

NEGATIVE_RESPONSE = 0x7F
RESPONSE_PENDING = 0x78

P2_TIMEOUT = 1.0
P2_EXTENDED_TIMEOUT = 5.0

# Class 2 addressing. Diagnostic tools use 0xF1; the powertrain control module answers at 0x10; functional
# (all-emissions) requests go to 0x6A. Real vehicles vary, so these are the defaults, not fixed.
TESTER_ADDRESS = 0xF1
PCM_ADDRESS = 0x10
FUNCTIONAL_TARGET = 0x6A
DEFAULT_PRIORITY = 0x68  # Header priority/type byte of a physical diagnostic request


def service_name(mode: int) -> str:
    return SERVICES.get(mode, f'0x{mode:02X}')


def nrc_name(code: int) -> str:
    return NEGATIVE_RESPONSE_CODES.get(code, f'0x{code:02X}')


def j1850_crc(data: bytes) -> int:
    """The SAE J1850 CRC-8 (polynomial 0x1D, initial 0xFF, final XOR 0xFF), over the header and payload bytes."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def format_obd_dtc(code: int) -> str:
    """A two-byte OBD-II trouble code as SAE J2012 text, e.g. 0x0143 -> 'P0143', 0x0171 -> 'P0171'."""
    letter = 'PCBU'[(code >> 14) & 0x3]
    return f'{letter}{(code >> 12) & 0x3}{code & 0x0FFF:03X}'


class J1850Error(Exception):
    pass


class J1850NegativeResponse(J1850Error):
    def __init__(self, mode: int, code: int):
        self.mode, self.code = mode, code
        super().__init__(f'{service_name(mode)}: negative response {nrc_name(code)} (0x{code:02X})')


@dataclass(frozen=True, slots=True)
class J1850Message:
    """A J1850 frame: a three-byte header (priority, target, source) and a payload, carried with an SAE J1850 CRC."""

    priority: int
    target: int
    source: int
    data: bytes = b''

    @property
    def header(self) -> bytes:
        return bytes([self.priority, self.target, self.source])

    def encode(self) -> bytes:
        """The bytes on the wire: header, payload and the trailing CRC."""
        body = self.header + bytes(self.data)
        return body + bytes([j1850_crc(body)])

    @classmethod
    def decode(cls, frame: bytes) -> 'J1850Message':
        """Parse a frame (header, payload and CRC). Raises J1850Error on a short frame or a bad CRC."""
        frame = bytes(frame)
        if len(frame) < 4:
            raise J1850Error(f'J1850 frame too short: {format_hex(frame)}')
        if j1850_crc(frame[:-1]) != frame[-1]:
            raise J1850Error(f'Bad J1850 CRC: {format_hex(frame)}')
        return cls(frame[0], frame[1], frame[2], frame[3:-1])

    @property
    def description(self) -> str:
        return f'[{self.priority:02X} {self.target:02X}←{self.source:02X}] {describe_payload(self.data)}'


def describe_payload(payload: bytes) -> str:
    """Human-readable description of a service payload (a mode byte and its parameters, or a response)."""
    payload = bytes(payload)
    if not payload:
        return '(empty)'
    if payload[0] == NEGATIVE_RESPONSE and len(payload) >= 3:
        return f'Negative response to {service_name(payload[1])}: {nrc_name(payload[2])} (0x{payload[2]:02X})'
    if payload[0] >= 0x40:  # A positive response: mode + 0x40
        mode = payload[0] - 0x40
        if mode == 0x01 and len(payload) >= 2 and payload[1] in PIDS_BY_NUMBER:
            pid = PIDS_BY_NUMBER[payload[1]]
            if len(payload) >= 2 + pid.length:
                return f'{pid.name}: {pid.decode(payload[2 : 2 + pid.length]):g} {pid.unit}'.strip()
        return f'{service_name(mode)} response {format_hex(payload[1:])}'.strip()
    return f'{service_name(payload[0])} {format_hex(payload[1:])}'.strip()


describe_request = describe_payload
describe_response = describe_payload


@dataclass(frozen=True, slots=True)
class J1850Result:
    request: bytes
    response: Optional[bytes]
    error: Optional[str] = None

    @property
    def positive(self) -> bool:
        if not self.response or self.response[0] == NEGATIVE_RESPONSE:
            return False
        return self.response[0] == self.request[0] + 0x40

    @property
    def description(self) -> str:
        if self.error:
            return self.error
        if self.response is None:
            return 'No response'
        return describe_payload(self.response)


class J1850Transport(Protocol):
    async def send(self, message: J1850Message) -> None: ...

    async def receive(self, timeout: Optional[float] = None) -> J1850Message: ...


class J1850Client:
    """An SAE J1979 / GM Class 2 diagnostic client over a J1850 transport (an ELM327/J2534 adapter, or the
    in-memory bus). Requests are addressed to `target`; responses addressed back to `source` are collected."""

    def __init__(
        self,
        transport: J1850Transport,
        source: int = TESTER_ADDRESS,
        target: int = PCM_ADDRESS,
        priority: int = DEFAULT_PRIORITY,
        timeout: float = P2_TIMEOUT,
    ):
        self.transport = transport
        self.source, self.target, self.priority = source, target, priority
        self.timeout = timeout

    async def request(self, payload: bytes, functional: bool = False) -> J1850Result:
        """Send a request and wait for the response, skipping 'response pending' answers. Never raises."""
        payload = bytes(payload)
        mode = payload[0] if payload else 0
        target = FUNCTIONAL_TARGET if functional else self.target
        message = J1850Message(self.priority, target, self.source, payload)
        try:
            await self.transport.send(message)
            response = await self._response(self.timeout, mode)
            while len(response) >= 3 and response[0] == NEGATIVE_RESPONSE and response[2] == RESPONSE_PENDING:
                response = await self._response(P2_EXTENDED_TIMEOUT, mode)
        except Exception as e:  # Transport errors (timeouts, bus errors)
            return J1850Result(payload, None, str(e))
        return J1850Result(payload, response)

    async def _response(self, timeout: float, request_mode: int) -> bytes:
        """The payload of the response to our request, within the timeout: a positive response (mode + 0x40) or a
        negative response to this mode. Our own transmissions and unrelated bus traffic (when the transport is a
        monitored session carrying the whole bus) are skipped, so the right answer is matched by its content."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError('No J1850 response')
            message = await self.transport.receive(remaining)
            if message.source == self.source:
                continue  # Our own transmission, echoed back
            payload = bytes(message.data)
            if not payload:
                continue
            if payload[0] == (request_mode + 0x40) & 0xFF:
                return payload
            if payload[0] == NEGATIVE_RESPONSE and len(payload) >= 2 and payload[1] == request_mode:
                return payload
            # Unrelated bus traffic (monitoring): keep waiting for our response

    async def call(self, payload: bytes, functional: bool = False) -> bytes:
        """Like request(), but returns the positive response and raises J1850Error otherwise."""
        result = await self.request(payload, functional)
        if result.error:
            raise J1850Error(result.error)
        response = result.response or b''
        if response and response[0] == NEGATIVE_RESPONSE and len(response) >= 3:
            raise J1850NegativeResponse(response[1], response[2])
        if not response or response[0] != payload[0] + 0x40:
            raise J1850Error(f'Unexpected response {format_hex(response)} to {describe_payload(payload)}')
        return response

    async def request_pid(self, pid: int, functional: bool = True) -> bytes:
        """ShowCurrentData (mode 0x01) for a PID; returns the value bytes of the answer."""
        response = await self.call(bytes([0x01, pid & 0xFF]), functional)
        if len(response) < 2 or response[1] != (pid & 0xFF):
            raise J1850Error(f'Response for PID 0x{response[1] if len(response) > 1 else 0:02X}, expected 0x{pid:02X}')
        return response[2:]

    async def current_data(self, pid: int) -> float:
        """A mode 0x01 PID decoded to its physical value through the shared OBD-II PID table."""
        if pid not in PIDS_BY_NUMBER:
            raise J1850Error(f'Unknown OBD-II PID 0x{pid:02X}')
        definition = PIDS_BY_NUMBER[pid]
        data = await self.request_pid(pid)
        if len(data) < definition.length:
            raise J1850Error(f'Short data for {definition.name}')
        return float(definition.decode(data[: definition.length]))

    async def read_dtcs(self, mode: int = 0x03) -> List[str]:
        """Stored (0x03), pending (0x07) or permanent (0x0A) trouble codes as SAE J2012 text."""
        response = await self.call(bytes([mode]), functional=True)
        codes = response[1:]
        dtcs = []
        for i in range(0, len(codes) - 1, 2):
            code = (codes[i] << 8) | codes[i + 1]
            if code:
                dtcs.append(format_obd_dtc(code))
        return dtcs

    async def clear_dtcs(self) -> None:
        """ClearDTCs (mode 0x04): clear stored codes and turn off the MIL."""
        await self.call(bytes([0x04]), functional=True)

    async def vehicle_info(self, pid: int) -> bytes:
        """RequestVehicleInformation (mode 0x09), e.g. PID 0x02 for the VIN."""
        return (await self.call(bytes([0x09, pid & 0xFF]), functional=True))[2:]


async def j1850_request(transport: J1850Transport, payload: bytes) -> J1850Result:
    return await J1850Client(transport).request(payload)


class J1850Decoder:
    """Annotates J1850 frames (for scripts and monitoring): a request if it comes from the tester, else a response."""

    def __init__(self, tester: int = TESTER_ADDRESS):
        self.tester = tester

    def describe(self, message: J1850Message) -> str:
        arrow = 'J1850 →' if message.source == self.tester else 'J1850 ←'
        return f'{arrow} {describe_payload(message.data)}'


# --- In-memory bus and a simulated Class 2 ECU, for running without hardware -------------------------------------


class J1850Bus:
    """A shared in-memory J1850 bus: every node sees the frames the others send, as on the real single-wire bus."""

    def __init__(self) -> None:
        self._nodes: List['J1850Node'] = []

    def attach(self) -> 'J1850Node':
        node = J1850Node(self)
        self._nodes.append(node)
        return node

    def _detach(self, node: 'J1850Node') -> None:
        if node in self._nodes:
            self._nodes.remove(node)

    def _broadcast(self, sender: 'J1850Node', message: J1850Message) -> None:
        for node in self._nodes:
            if node is not sender:
                node._inbox.put_nowait(message)


class J1850Node:
    """A node on a `J1850Bus`; a `J1850Transport` for a client or a simulator."""

    def __init__(self, bus: J1850Bus) -> None:
        self._bus = bus
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, message: J1850Message) -> None:
        self._bus._broadcast(self, message)

    async def receive(self, timeout: Optional[float] = None) -> J1850Message:
        if timeout is None:
            return await self._inbox.get()
        return await asyncio.wait_for(self._inbox.get(), timeout)

    def close(self) -> None:
        self._bus._detach(self)


@dataclass
class Class2Server:
    """GM Class 2 / OBD-II server logic. handle() returns the response payloads to send for a request payload."""

    # Mode 0x01 current data: PID -> value bytes (decoded by the shared OBD-II PID table)
    data: Dict[int, bytes] = field(
        default_factory=lambda: {0x05: b'\x5a', 0x0C: b'\x0c\x6b', 0x0D: b'\x00', 0x11: b'\x40'}
    )
    dtcs: List[int] = field(default_factory=lambda: [0x0143, 0x0171])  # Two-byte OBD-II codes
    vehicle_info: Dict[int, bytes] = field(default_factory=lambda: {0x02: b'1GCEK14V5XZ100001'})
    requests: List[bytes] = field(default_factory=list)

    @staticmethod
    def _negative(mode: int, code: int) -> List[bytes]:
        return [bytes([NEGATIVE_RESPONSE, mode, code])]

    def handle(self, payload: bytes) -> List[bytes]:
        payload = bytes(payload)
        self.requests.append(payload)
        if not payload:
            return []
        mode = payload[0]
        handler = {
            0x01: self._current_data,
            0x03: self._read_dtcs,
            0x04: self._clear_dtcs,
            0x09: self._vehicle_info,
        }.get(mode)
        if handler is None:
            return self._negative(mode, 0x11)
        try:
            return handler(payload)
        except (IndexError, ValueError):
            return self._negative(mode, 0x31)  # A truncated/malformed request, not a crash

    def _current_data(self, payload: bytes) -> List[bytes]:
        pid = payload[1]
        if pid not in self.data:
            return self._negative(0x01, 0x31)
        return [bytes([0x41, pid]) + self.data[pid]]

    def _read_dtcs(self, payload: bytes) -> List[bytes]:
        records = b''.join(bytes([code >> 8, code & 0xFF]) for code in self.dtcs)
        return [bytes([0x43]) + records]

    def _clear_dtcs(self, payload: bytes) -> List[bytes]:
        self.dtcs = []
        return [bytes([0x44])]

    def _vehicle_info(self, payload: bytes) -> List[bytes]:
        pid = payload[1]
        if pid not in self.vehicle_info:
            return self._negative(0x09, 0x31)
        return [bytes([0x49, pid]) + self.vehicle_info[pid]]


class Class2EcuSimulator:
    """A `Class2Server` answering on a `J1850Bus`, standing in for a GM P01/P59 PCM without any hardware."""

    def __init__(
        self,
        bus: J1850Bus,
        address: int = PCM_ADDRESS,
        priority: int = DEFAULT_PRIORITY,
        server: Optional[Class2Server] = None,
    ):
        self.address, self.priority = address, priority
        self.server = server or Class2Server()
        self._node = bus.attach()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name='Class 2 ECU simulator')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._node.close()

    async def _run(self) -> None:
        while True:
            message = await self._node.receive()
            if message.target not in (self.address, FUNCTIONAL_TARGET):
                continue
            for response in self.server.handle(message.data):
                await self._node.send(J1850Message(self.priority, message.source, self.address, response))
