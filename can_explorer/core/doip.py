"""Diagnostics over IP (ISO 13400-2): vehicle discovery, a tester with routing activation, a UDS transport and a
simulated DoIP entity.

- discover(): vehicle identification requests over UDP, answered by the DoIP entities (gateways, nodes) in reach.
- entity_status(), power_mode(): DoIP entity status and diagnostic power mode over UDP.
- DoipClient: the tester's TCP connection to an entity, with routing activation and answers to alive checks.
- DoipTransport: UDS over a DoipClient to one target address (`UdsClient(client.transport(0x1001))`).
- DoipEntitySimulator: a DoIP entity with UdsServers behind its target addresses, for trying out without a vehicle.
"""

import asyncio
import socket
import ssl
import struct
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

PORT = 13400  # UDP_DISCOVERY and TCP_DATA
TLS_PORT = 3496  # TCP_DATA with TLS (ISO 13400-2:2019)
BROADCAST = '255.255.255.255'

PROTOCOL_2012 = 0x02  # ISO 13400-2:2012
PROTOCOL_2019 = 0x03  # ISO 13400-2:2019
PROTOCOL_DEFAULT = 0xFF  # Vehicle identification requests: any version
SUPPORTED_VERSIONS = (0x01, 0x02, 0x03, 0x04)

DEFAULT_TESTER_ADDRESS = 0x0E00  # External test equipment uses 0x0E00 to 0x0FFF
TESTER_ADDRESSES = range(0x0E00, 0x1000)

HEADER = struct.Struct('>BBHI')
MAX_PAYLOAD = 16 * 1024 * 1024

# A_DoIP_Ctrl: answers to control messages (routing activation, UDP requests)
CONTROL_TIMEOUT = 2.0
# A_DoIP_Diagnostic_Message: the acknowledgement of a diagnostic message
DIAGNOSTIC_ACK_TIMEOUT = 2.0
DISCOVERY_TIMEOUT = 2.0

# Payload types
GENERIC_NACK = 0x0000
VEHICLE_ID_REQUEST = 0x0001
VEHICLE_ID_REQUEST_EID = 0x0002
VEHICLE_ID_REQUEST_VIN = 0x0003
VEHICLE_ANNOUNCEMENT = 0x0004  # Also the vehicle identification response
ROUTING_ACTIVATION_REQUEST = 0x0005
ROUTING_ACTIVATION_RESPONSE = 0x0006
ALIVE_CHECK_REQUEST = 0x0007
ALIVE_CHECK_RESPONSE = 0x0008
ENTITY_STATUS_REQUEST = 0x4001
ENTITY_STATUS_RESPONSE = 0x4002
POWER_MODE_REQUEST = 0x4003
POWER_MODE_RESPONSE = 0x4004
DIAGNOSTIC_MESSAGE = 0x8001
DIAGNOSTIC_ACK = 0x8002
DIAGNOSTIC_NACK = 0x8003

GENERIC_NACK_CODES = {
    0x00: 'incorrect pattern format',
    0x01: 'unknown payload type',
    0x02: 'message too large',
    0x03: 'out of memory',
    0x04: 'invalid payload length',
}

ACTIVATION_TYPES = {0x00: 'default', 0x01: 'WWH-OBD', 0xE0: 'central security'}

ROUTING_ACTIVATED = 0x10
ROUTING_CONFIRMATION_REQUIRED = 0x11
ROUTING_ACTIVATION_CODES = {
    0x00: 'unknown source address',
    0x01: 'all TCP sockets are registered and active',
    0x02: 'the source address differs from the one already activated on this socket',
    0x03: 'the source address is already active on another socket',
    0x04: 'missing authentication',
    0x05: 'rejected confirmation',
    0x06: 'unsupported routing activation type',
    0x07: 'a TLS connection is required',
    ROUTING_ACTIVATED: 'routing activated',
    ROUTING_CONFIRMATION_REQUIRED: 'routing will be activated, confirmation required',
}

DIAGNOSTIC_NACK_CODES = {
    0x02: 'invalid source address',
    0x03: 'unknown target address',
    0x04: 'diagnostic message too large',
    0x05: 'out of memory',
    0x06: 'target unreachable',
    0x07: 'unknown network',
    0x08: 'transport protocol error',
}

FURTHER_ACTIONS = {0x00: 'none', 0x10: 'routing activation required for central security'}
SYNC_STATUS = {0x00: 'synchronised', 0x10: 'not synchronised'}
POWER_MODES = {0x00: 'not ready', 0x01: 'ready', 0x02: 'not supported'}
NODE_TYPES = {0x00: 'gateway', 0x01: 'node'}


class DoipError(Exception):
    pass


def _code_name(names: Dict[int, str], code: int) -> str:
    return names.get(code, f'0x{code:02X}')


def encode_message(payload_type: int, payload: bytes = b'', version: int = PROTOCOL_2012) -> bytes:
    """A DoIP message: generic header (version, inverse version, payload type, length) and payload."""
    return HEADER.pack(version, version ^ 0xFF, payload_type, len(payload)) + bytes(payload)


def decode_header(data: bytes) -> Tuple[int, int, int]:
    """(protocol version, payload type, payload length) of a generic header; DoipError if it is malformed."""
    if len(data) < HEADER.size:
        raise DoipError('Message shorter than the DoIP header')
    version, inverse, payload_type, length = HEADER.unpack_from(data)
    if version ^ 0xFF != inverse or version not in SUPPORTED_VERSIONS + (PROTOCOL_DEFAULT,):
        raise DoipError(f'Incorrect DoIP header pattern ({version:02X} {inverse:02X})')
    if length > MAX_PAYLOAD:
        raise DoipError(f'DoIP message of {length} bytes is too large')
    return version, payload_type, length


def decode_message(data: bytes) -> Tuple[int, int, bytes]:
    """(protocol version, payload type, payload) of a complete message, e.g. a UDP datagram."""
    version, payload_type, length = decode_header(data)
    payload = bytes(data[HEADER.size : HEADER.size + length])
    if len(payload) != length:
        raise DoipError('DoIP message shorter than its length')
    return version, payload_type, payload


async def read_message(reader: asyncio.StreamReader) -> Tuple[int, int, bytes]:
    """Read one message from a TCP stream. Raises asyncio.IncompleteReadError when the stream ends."""
    header = await reader.readexactly(HEADER.size)
    version, payload_type, length = decode_header(header)
    return version, payload_type, await reader.readexactly(length)


def format_eid(eid: bytes) -> str:
    """An entity or group identification (usually a MAC address) as 00:1A:2B:3C:4D:5E."""
    return ':'.join(f'{byte:02X}' for byte in eid)


def parse_eid(text: str) -> bytes:
    digits = text.replace(':', '').replace('-', '').replace(' ', '')
    try:
        eid = bytes.fromhex(digits)
    except ValueError:
        raise ValueError(f'Invalid EID {text!r}') from None
    if len(eid) != 6:
        raise ValueError(f'An EID has 6 bytes, {text!r} has {len(eid)}')
    return eid


# Payloads


@dataclass(frozen=True, slots=True)
class DoipEntity:
    """A DoIP entity that answered a vehicle identification request (or announced itself)."""

    host: str
    vin: str
    logical_address: int
    eid: bytes
    gid: bytes
    further_action: int = 0
    sync_status: Optional[int] = None
    port: int = PORT

    @property
    def eid_text(self) -> str:
        return format_eid(self.eid)

    @property
    def gid_text(self) -> str:
        return format_eid(self.gid)

    @property
    def further_action_text(self) -> str:
        return _code_name(FURTHER_ACTIONS, self.further_action)

    def payload(self) -> bytes:
        payload = (
            self.vin.encode('ascii', 'replace')[:17].ljust(17, b'\x00')
            + self.logical_address.to_bytes(2, 'big')
            + bytes(self.eid)
            + bytes(self.gid)
            + bytes([self.further_action])
        )
        return payload + (bytes([self.sync_status]) if self.sync_status is not None else b'')

    @classmethod
    def from_payload(cls, host: str, payload: bytes, port: int = PORT) -> 'DoipEntity':
        if len(payload) not in (32, 33):
            raise DoipError(f'Vehicle announcement of {len(payload)} bytes, expected 32 or 33')
        vin = payload[:17].rstrip(b'\x00\xff').decode('ascii', 'replace')
        return cls(
            host=host,
            vin=vin,
            logical_address=int.from_bytes(payload[17:19], 'big'),
            eid=bytes(payload[19:25]),
            gid=bytes(payload[25:31]),
            further_action=payload[31],
            sync_status=payload[32] if len(payload) == 33 else None,
            port=port,
        )


@dataclass(frozen=True, slots=True)
class RoutingActivation:
    """The answer to a routing activation request."""

    tester_address: int
    entity_address: int
    code: int

    @property
    def activated(self) -> bool:
        return self.code == ROUTING_ACTIVATED

    @property
    def text(self) -> str:
        return _code_name(ROUTING_ACTIVATION_CODES, self.code)


@dataclass(frozen=True, slots=True)
class EntityStatus:
    node_type: int
    max_sockets: int
    open_sockets: int
    max_data_size: Optional[int] = None

    @property
    def text(self) -> str:
        text = f'{_code_name(NODE_TYPES, self.node_type)}, {self.open_sockets} of {self.max_sockets} TCP sockets open'
        if self.max_data_size is not None:
            text += f', diagnostic messages up to {self.max_data_size} bytes'
        return text

    def payload(self) -> bytes:
        payload = bytes([self.node_type, self.max_sockets, self.open_sockets])
        return payload + (self.max_data_size.to_bytes(4, 'big') if self.max_data_size is not None else b'')

    @classmethod
    def from_payload(cls, payload: bytes) -> 'EntityStatus':
        if len(payload) not in (3, 7):
            raise DoipError(f'Entity status response of {len(payload)} bytes, expected 3 or 7')
        size = int.from_bytes(payload[3:7], 'big') if len(payload) == 7 else None
        return cls(payload[0], payload[1], payload[2], size)


def _addresses(payload: bytes) -> Tuple[int, int]:
    return int.from_bytes(payload[0:2], 'big'), int.from_bytes(payload[2:4], 'big')


def _address_bytes(source: int, target: int) -> bytes:
    return source.to_bytes(2, 'big') + target.to_bytes(2, 'big')


# UDP requests


class _UdpCollector(asyncio.DatagramProtocol):
    def __init__(self, accepted_types: Tuple[int, ...]):
        self.accepted_types = accepted_types
        self.messages: asyncio.Queue[Tuple[str, int, int, bytes]] = asyncio.Queue()
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, address):
        try:
            _, payload_type, payload = decode_message(data)
        except DoipError:
            return
        if payload_type in self.accepted_types:
            self.messages.put_nowait((address[0], address[1], payload_type, payload))


async def _udp_exchange(
    host: str, port: int, request: bytes, accepted_types: Tuple[int, ...], timeout: float, first_only: bool
) -> List[Tuple[str, int, int, bytes]]:
    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    # Bind before anything is sent: Windows' proactor loop starts receiving as soon as the transport exists, and
    # receiving on a socket that is not bound yet fails there, losing every answer
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _UdpCollector(accepted_types),
        local_addr=('::' if family == socket.AF_INET6 else '0.0.0.0', 0),
        family=family,
        allow_broadcast=family == socket.AF_INET,
    )
    answers = []
    try:
        transport.sendto(request, (host, port))
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                answers.append(await asyncio.wait_for(protocol.messages.get(), remaining))
            except asyncio.TimeoutError:
                break
            if first_only:
                break
    finally:
        transport.close()
    return answers


async def discover(
    address: str = BROADCAST,
    port: int = PORT,
    timeout: float = DISCOVERY_TIMEOUT,
    vin: Optional[str] = None,
    eid: Optional[bytes] = None,
) -> List[DoipEntity]:
    """Send a vehicle identification request and collect the entities answering within `timeout` seconds.

    `address` is the limited broadcast by default; a directed broadcast (e.g. 192.168.0.255) picks a network, an
    entity's address asks only that entity. `vin` or `eid` ask only the entities of that vehicle or with that EID.
    """
    if vin:
        request = encode_message(VEHICLE_ID_REQUEST_VIN, vin.encode('ascii')[:17].ljust(17, b'\x00'), PROTOCOL_DEFAULT)
    elif eid:
        request = encode_message(VEHICLE_ID_REQUEST_EID, bytes(eid), PROTOCOL_DEFAULT)
    else:
        request = encode_message(VEHICLE_ID_REQUEST, b'', PROTOCOL_DEFAULT)
    answers = await _udp_exchange(address, port, request, (VEHICLE_ANNOUNCEMENT,), timeout, first_only=False)
    entities: Dict[Tuple[str, int], DoipEntity] = {}
    for host, _, _, payload in answers:
        try:
            entity = DoipEntity.from_payload(host, payload, port)
        except DoipError:
            continue
        entities.setdefault((host, entity.logical_address), entity)
    return list(entities.values())


async def _udp_request(host: str, port: int, request_type: int, answer_type: int, timeout: float) -> bytes:
    answers = await _udp_exchange(
        host, port, encode_message(request_type), (answer_type, GENERIC_NACK), timeout, first_only=True
    )
    if not answers:
        raise DoipError(f'No answer from {host}')
    _, _, payload_type, payload = answers[0]
    if payload_type == GENERIC_NACK:
        raise DoipError(f'Negative acknowledgement: {_code_name(GENERIC_NACK_CODES, payload[0] if payload else -1)}')
    return payload


async def entity_status(host: str, port: int = PORT, timeout: float = CONTROL_TIMEOUT) -> EntityStatus:
    """The node type and TCP sockets of an entity."""
    return EntityStatus.from_payload(
        await _udp_request(host, port, ENTITY_STATUS_REQUEST, ENTITY_STATUS_RESPONSE, timeout)
    )


async def power_mode(host: str, port: int = PORT, timeout: float = CONTROL_TIMEOUT) -> int:
    """The diagnostic power mode of an entity (POWER_MODES): whether its ECUs are ready for diagnostics."""
    payload = await _udp_request(host, port, POWER_MODE_REQUEST, POWER_MODE_RESPONSE, timeout)
    if len(payload) != 1:
        raise DoipError(f'Power mode response of {len(payload)} bytes, expected 1')
    return payload[0]


# Tester


class DoipClient:
    """The tester's TCP connection to a DoIP entity.

    connect() opens the connection and activates routing; afterwards diagnostic messages reach the entity's
    targets (the gateway itself or the ECUs behind it), e.g. through `UdsClient(client.transport(0x1001))`.
    Alive checks of the entity are answered automatically.

    Examples:
        ```python
        async with DoipClient('192.168.0.10') as client:
            uds = UdsClient(client.transport(0x1001))
            vin = await uds.read_data_by_identifier(0xF190)
        ```
    """

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        source_address: int = DEFAULT_TESTER_ADDRESS,
        activation_type: int = 0x00,
        protocol_version: int = PROTOCOL_2012,
        tls: bool | ssl.SSLContext = False,
        timeout: float = CONTROL_TIMEOUT,
        oem_data: bytes = b'',
    ):
        """
        Args:
            host: Address of the DoIP entity.
            port: TCP port; 13400, or 3496 with TLS.
            source_address: The tester's logical address (0x0E00 to 0x0FFF for external test equipment).
            activation_type: Routing activation type (ACTIVATION_TYPES).
            protocol_version: 0x02 (ISO 13400-2:2012) or 0x03 (ISO 13400-2:2019).
            tls: Use TLS: True without verifying the entity's certificate, or an SSLContext.
            timeout: Seconds to wait for the routing activation response.
            oem_data: 4 OEM specific bytes of the routing activation request (e.g. an authentication).
        """
        if tls is True:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            tls = context
        self.host = host
        self.tls: Optional[ssl.SSLContext] = tls or None
        self.port = port if port is not None else (TLS_PORT if self.tls else PORT)
        self.source_address = source_address
        self.activation_type = activation_type
        self.protocol_version = protocol_version
        self.timeout = timeout
        self.oem_data = bytes(oem_data)
        self.entity_address: Optional[int] = None
        self.activation: Optional[RoutingActivation] = None
        self.alive_checks = 0
        self.close_reason = ''
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._activation: Optional[asyncio.Future] = None
        # Acknowledgements (positive, source, target, code), None when the connection ended
        self._acks: asyncio.Queue[Optional[Tuple[bool, int, int, int]]] = asyncio.Queue()
        self._receivers: Dict[int, List[asyncio.Queue]] = {}
        self._close_listeners: List[Callable[[str], None]] = []
        self._closed = asyncio.Event()

    @property
    def is_open(self) -> bool:
        return self._task is not None and not self._task.done() and not self._closed.is_set()

    def add_close_listener(self, listener: Callable[[str], None]) -> None:
        """Called with the reason when the connection ends (also when closed by the tester: reason '')."""
        self._close_listeners.append(listener)

    async def __aenter__(self) -> 'DoipClient':
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def connect(self) -> RoutingActivation:
        """Open the connection and activate routing. Raises DoipError if routing is not activated."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self.tls), self.timeout
            )
        except asyncio.TimeoutError:
            raise DoipError(f'No connection to {self.host}:{self.port}') from None
        except (OSError, ssl.SSLError) as e:
            raise DoipError(f'Could not connect to {self.host}:{self.port}: {e.strerror or e}') from None
        self._closed.clear()
        self._task = asyncio.get_running_loop().create_task(self._read_loop(), name='DoIP client')
        try:
            activation = await self.activate_routing()
        except BaseException:
            await self.close()
            raise
        if not activation.activated:
            await self.close()
            raise DoipError(f'Routing activation refused: {activation.text}')
        return activation

    async def activate_routing(self) -> RoutingActivation:
        """Send a routing activation request on the open connection and wait for the answer."""
        self._activation = asyncio.get_running_loop().create_future()
        payload = bytes([*self.source_address.to_bytes(2, 'big'), self.activation_type, 0, 0, 0, 0]) + self.oem_data
        await self._write(ROUTING_ACTIVATION_REQUEST, payload)
        try:
            activation = await asyncio.wait_for(asyncio.shield(self._activation), self.timeout)
        except asyncio.TimeoutError:
            raise DoipError('No routing activation response') from None
        finally:
            self._activation = None
        self.activation = activation
        if activation.activated:
            self.entity_address = activation.entity_address
        return activation

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_writer()

    async def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except (OSError, ssl.SSLError, asyncio.TimeoutError):
                pass

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _write(self, payload_type: int, payload: bytes) -> None:
        if self._writer is None or self._closed.is_set():
            raise DoipError('Not connected')
        async with self._write_lock:
            try:
                self._writer.write(encode_message(payload_type, payload, self.protocol_version))
                await self._writer.drain()
            except (OSError, ssl.SSLError) as e:
                raise DoipError(f'Connection lost: {e}') from None

    # Diagnostic messages

    async def send_diagnostic(self, target: int, data: bytes) -> None:
        """Send a diagnostic message to a target address and wait for its acknowledgement."""
        if not data:
            raise DoipError('Cannot send an empty diagnostic message')
        async with self._send_lock:
            while not self._acks.empty():
                self._acks.get_nowait()
            await self._write(DIAGNOSTIC_MESSAGE, _address_bytes(self.source_address, target) + bytes(data))
            try:
                ack = await asyncio.wait_for(self._acks.get(), DIAGNOSTIC_ACK_TIMEOUT)
            except asyncio.TimeoutError:
                raise DoipError(f'No acknowledgement of the diagnostic message to 0x{target:04X}') from None
        if ack is None:
            raise DoipError(self.close_reason or 'Connection closed')
        positive, _source, _target, code = ack
        if not positive:
            raise DoipError(f'Diagnostic message to 0x{target:04X} refused: {_code_name(DIAGNOSTIC_NACK_CODES, code)}')

    def add_receiver(self, source: int, queue: asyncio.Queue) -> None:
        self._receivers.setdefault(source, []).append(queue)

    def remove_receiver(self, source: int, queue: asyncio.Queue) -> None:
        queues = self._receivers.get(source, [])
        if queue in queues:
            queues.remove(queue)

    def transport(self, target: int) -> 'DoipTransport':
        """A UDS transport to a target address; close it when done."""
        return DoipTransport(self, target)

    # Reception

    async def _read_loop(self) -> None:
        reason = ''
        try:
            while True:
                _, payload_type, payload = await read_message(self._reader)
                await self._handle(payload_type, payload)
        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError:
            reason = 'The DoIP entity closed the connection'
        except DoipError as e:
            reason = str(e)
            try:
                await self._write(GENERIC_NACK, bytes([0x00]))
            except DoipError:
                pass
        except (OSError, ssl.SSLError) as e:
            reason = f'Connection lost: {e}'
        finally:
            self.close_reason = reason
            self._closed.set()
            if self._activation is not None and not self._activation.done():
                self._activation.set_exception(DoipError(reason or 'Connection closed'))
            # Wake up the senders and receivers waiting for the entity
            self._acks.put_nowait(None)
            for queues in self._receivers.values():
                for queue in queues:
                    queue.put_nowait(None)
            for listener in list(self._close_listeners):
                listener(reason)
            if reason:
                await self._close_writer()

    async def _handle(self, payload_type: int, payload: bytes) -> None:
        if payload_type == DIAGNOSTIC_MESSAGE and len(payload) > 4:
            source, target = _addresses(payload)
            if target == self.source_address:
                for queue in self._receivers.get(source, []):
                    queue.put_nowait(bytes(payload[4:]))
        elif payload_type in (DIAGNOSTIC_ACK, DIAGNOSTIC_NACK) and len(payload) >= 5:
            source, target = _addresses(payload)
            self._acks.put_nowait((payload_type == DIAGNOSTIC_ACK, source, target, payload[4]))
        elif payload_type == ROUTING_ACTIVATION_RESPONSE and len(payload) >= 5:
            tester, entity = _addresses(payload)
            if self._activation is not None and not self._activation.done():
                self._activation.set_result(RoutingActivation(tester, entity, payload[4]))
        elif payload_type == ALIVE_CHECK_REQUEST:
            self.alive_checks += 1
            await self._write(ALIVE_CHECK_RESPONSE, self.source_address.to_bytes(2, 'big'))
        elif payload_type == GENERIC_NACK:
            code = payload[0] if payload else -1
            error = DoipError(f'Negative acknowledgement: {_code_name(GENERIC_NACK_CODES, code)}')
            if self._activation is not None and not self._activation.done():
                self._activation.set_exception(error)


class DoipTransport:
    """UDS transport over a DoipClient: diagnostic messages to and from one target address."""

    def __init__(self, client: DoipClient, target: int):
        self.client = client
        self.target = target
        self._received: asyncio.Queue[Optional[bytes]] = asyncio.Queue()  # None: the connection ended
        client.add_receiver(target, self._received)

    def close(self) -> None:
        self.client.remove_receiver(self.target, self._received)

    def __enter__(self) -> 'DoipTransport':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    async def send(self, payload: bytes) -> None:
        while not self._received.empty():
            self._received.get_nowait()
        await self.client.send_diagnostic(self.target, payload)

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        if not self.client.is_open:
            raise DoipError(self.client.close_reason or 'Not connected')
        try:
            payload = await asyncio.wait_for(
                self._received.get(), DIAGNOSTIC_ACK_TIMEOUT if timeout is None else timeout
            )
        except asyncio.TimeoutError:
            raise DoipError('Timeout waiting for a response') from None
        if payload is None:
            raise DoipError(self.client.close_reason or 'Connection closed')
        return payload

    async def request(self, payload: bytes, timeout: Optional[float] = None) -> bytes:
        await self.send(payload)
        return await self.receive(timeout)


# Simulated entity


class _SimulatorUdp(asyncio.DatagramProtocol):
    def __init__(self, simulator: 'DoipEntitySimulator'):
        self.simulator = simulator
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, address):
        answer = self.simulator._udp_answer(data)
        if answer is not None and self.transport is not None:
            self.transport.sendto(answer, address)


class DoipEntitySimulator:
    """A DoIP entity (a gateway) on this computer with UDS servers behind its target addresses.

    It answers vehicle identification, entity status and power mode requests over UDP, and routing activation and
    diagnostic messages over TCP on the same port.

    Examples:
        ```python
        simulator = DoipEntitySimulator(targets={0x1001: UdsServer()})
        await simulator.start()        # 127.0.0.1, on a free port: simulator.port
        ...
        await simulator.stop()
        ```
    """

    def __init__(
        self,
        targets: Optional[Dict[int, object]] = None,
        logical_address: int = 0x1000,
        vin: str = 'WDB1234567890ABCD',
        eid: bytes = bytes.fromhex('02005E000001'),
        gid: bytes = bytes.fromhex('02005E000000'),
        host: str = '127.0.0.1',
        port: int = 0,
        max_sockets: int = 2,
        activation_types: Tuple[int, ...] = (0x00, 0x01),
        response_delay: float = 0.05,
    ):
        """
        Args:
            targets: UdsServer (`can_explorer.core.simulation`) per target address; by default one ECU at 0x1001.
            logical_address: The entity's own logical address.
            host: Address to listen on.
            port: TCP and UDP port; 0 picks a free one.
            max_sockets: TCP connections served at the same time.
            activation_types: Routing activation types accepted.
            response_delay: Seconds between a 'response pending' and the next response.
        """
        if targets is None:
            from can_explorer.core.simulation import UdsServer

            targets = {0x1001: UdsServer()}
        self.targets = targets
        self.entity = DoipEntity(host, vin, logical_address, bytes(eid), bytes(gid), 0x00, None)
        self.host, self.port = host, port
        self.max_sockets = max_sockets
        self.activation_types = activation_types
        self.response_delay = response_delay
        self.power_mode = 0x01
        self.connections = 0
        self.diagnostic_messages: List[Tuple[int, int, bytes]] = []
        self._server: Optional[asyncio.Server] = None
        self._udp: Optional[asyncio.DatagramTransport] = None
        self._writers: List[asyncio.StreamWriter] = []
        self._active_testers: Dict[int, asyncio.StreamWriter] = {}
        self._alive: Dict[int, bool] = {}  # Testers that answered the last alive check

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: _SimulatorUdp(self), local_addr=(self.host, self.port)
        )
        self.entity = DoipEntity(
            self.host,
            self.entity.vin,
            self.entity.logical_address,
            self.entity.eid,
            self.entity.gid,
            port=self.port,
        )

    async def stop(self) -> None:
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        server, self._server = self._server, None
        if server is not None:
            server.close()
            for writer in list(self._writers):
                writer.close()
            await server.wait_closed()

    async def alive_check(self, timeout: float = CONTROL_TIMEOUT) -> int:
        """Send an alive check request to every tester with activated routing; returns how many answered."""
        self._alive.clear()
        testers = list(self._active_testers.items())
        for _, writer in testers:
            writer.write(encode_message(ALIVE_CHECK_REQUEST))
            await writer.drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self._alive) < len(testers) and loop.time() < deadline:
            await asyncio.sleep(0.01)
        return sum(1 for tester, _ in testers if self._alive.get(tester))

    def _udp_answer(self, data: bytes) -> Optional[bytes]:
        try:
            _, payload_type, payload = decode_message(data)
        except DoipError:
            return encode_message(GENERIC_NACK, bytes([0x00]))
        entity = self.entity
        if payload_type == VEHICLE_ID_REQUEST:
            pass
        elif payload_type == VEHICLE_ID_REQUEST_EID:
            if payload != entity.eid:
                return None
        elif payload_type == VEHICLE_ID_REQUEST_VIN:
            if payload.rstrip(b'\x00').decode('ascii', 'replace') != entity.vin:
                return None
        elif payload_type == ENTITY_STATUS_REQUEST:
            status = EntityStatus(0x00, self.max_sockets, len(self._writers), 0xFFFF)
            return encode_message(ENTITY_STATUS_RESPONSE, status.payload())
        elif payload_type == POWER_MODE_REQUEST:
            return encode_message(POWER_MODE_RESPONSE, bytes([self.power_mode]))
        else:
            return encode_message(GENERIC_NACK, bytes([0x01]))
        return encode_message(VEHICLE_ANNOUNCEMENT, entity.payload())

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._writers) >= self.max_sockets:
            writer.close()
            return
        self._writers.append(writer)
        self.connections += 1
        tester: Optional[int] = None

        async def send(payload_type: int, payload: bytes) -> None:
            writer.write(encode_message(payload_type, payload))
            await writer.drain()

        try:
            while True:
                try:
                    _, payload_type, payload = await read_message(reader)
                except DoipError:
                    await send(GENERIC_NACK, bytes([0x00]))
                    break
                if payload_type == ROUTING_ACTIVATION_REQUEST and len(payload) >= 7:
                    source = int.from_bytes(payload[0:2], 'big')
                    code = self._activation_code(source, payload[2], tester, writer)
                    if code == ROUTING_ACTIVATED:
                        tester = source
                        self._active_testers[source] = writer
                    await send(
                        ROUTING_ACTIVATION_RESPONSE,
                        _address_bytes(source, self.entity.logical_address) + bytes([code, 0, 0, 0, 0]),
                    )
                    if code != ROUTING_ACTIVATED:
                        break
                elif payload_type == ALIVE_CHECK_RESPONSE:
                    if len(payload) >= 2:
                        self._alive[int.from_bytes(payload[0:2], 'big')] = True
                elif payload_type == DIAGNOSTIC_MESSAGE and len(payload) > 4:
                    source, target = _addresses(payload)
                    if tester is None or source != tester:
                        await send(DIAGNOSTIC_NACK, _address_bytes(target, source) + bytes([0x02]))
                        break
                    server = self.targets.get(target)
                    if server is None:
                        await send(DIAGNOSTIC_NACK, _address_bytes(target, source) + bytes([0x03]))
                        continue
                    request = bytes(payload[4:])
                    self.diagnostic_messages.append((source, target, request))
                    await send(DIAGNOSTIC_ACK, _address_bytes(target, source) + bytes([0x00]))
                    for index, response in enumerate(server.handle(request)):
                        if index:
                            await asyncio.sleep(self.response_delay)
                        await send(DIAGNOSTIC_MESSAGE, _address_bytes(target, source) + response)
                else:
                    await send(GENERIC_NACK, bytes([0x01]))
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            if tester is not None and self._active_testers.get(tester) is writer:
                del self._active_testers[tester]
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    def _activation_code(self, source: int, activation_type: int, tester: Optional[int], writer) -> int:
        if source not in TESTER_ADDRESSES:
            return 0x00
        if activation_type not in self.activation_types:
            return 0x06
        if tester is not None and tester != source:
            return 0x02
        active = self._active_testers.get(source)
        if active is not None and active is not writer:
            return 0x03
        return ROUTING_ACTIVATED
