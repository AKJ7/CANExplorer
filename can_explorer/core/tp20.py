"""VW Transport Protocol 2.0 (TP 2.0): the CAN transport of Volkswagen-group (VW, Audi, Seat, Škoda) diagnostics in
vehicles from roughly 2000 to 2011, before they moved to UDS on ISO-TP. Its payload is normally KWP2000 (ISO 14230-3,
see `core/kwp2000_client.py`). A module is not reachable with ISO-TP at all: the tester first opens a *channel* to
the module's logical address (e.g. 0x01, the engine), negotiates its timing, and then exchanges
acknowledged data packets on the two CAN identifiers the module assigned.

`Tp20Client` is the tester end — channel setup, parameters, keep-alive, disconnect — and a `UdsTransport`
(`send(payload)` / `receive(timeout)`), so `KwpClient` runs over it as over ISO-TP. `Tp20Channel` is the data layer
of either end (the client uses it after the setup; a test or a simulated module uses it on the module side).
`Tp20Reassembler` and the frame helpers serve the passive **VW TP 2.0** trace decoder (`core/protocols/tp20.py`).

Source. TP 2.0 is a VW in-house protocol with no public specification. This module follows the community
description at https://jazdw.net/tp20, worked out by its author from logged traffic, and its example exchange (which
the unit tests replay). What that page states only through its example frames is inferred from them:

- a channel identifier is two bytes: the low byte, then a byte with the validity flag in its high nibble (0 valid,
  1 invalid) and bits 8–10 of the identifier in its low nibble (`00 03` = 0x300, `40 07` = 0x740, `00 10` = none).
  In a setup request the fields tell the module which identifier to listen on and which to transmit on; in the
  response they tell the tester the same (the tester then transmits on the response's TX ID, e.g. 0x740, and
  receives on its RX ID, e.g. 0x300);
- a timing byte is a unit in its top two bits (0.1 ms, 1 ms, 10 ms, 100 ms) times a scale in its low six bits
  (`8A` = 10 × 10 ms = 100 ms; `32` = 50 × 0.1 ms = 5 ms);
- a data packet's first byte carries the opcode in its high nibble and the sequence number (0–15, wrapping) in its
  low nibble; each direction counts its own sequence; an ACK carries the *next* expected sequence number;
- the block size counts the packets sent before an ACK is required. The source's example sends 0x0F and calls it
  16 packets; this client keeps to the smaller reading, which is safe under both.

Not covered: the negative setup responses (0xD6–0xD8) are reported without a reason, as the source gives none;
broadcast frames (0x23/0x24 on 0x200) are only labelled by the trace decoder; the source gives no keep-alive
interval (the channel test is sent after `KEEP_ALIVE_INTERVAL` of silence, configurable) and no retransmission
rule (a lost ACK fails the send; a sequence gap drops the message being received).
"""

import asyncio
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

import can

from can_explorer.core.session import BusSession, SessionError

SETUP_ID = 0x200  # The tester sends channel setups here; module N answers on SETUP_ID + N
APPLICATION_KWP = 0x01  # Application type of a KWP2000 channel ("seems to always be 0x01")
DEFAULT_RX_ID = 0x300  # The identifier the tester asks the module to transmit on (the source suggests 0x300-0x310)

# Channel setup opcodes
OP_SETUP_REQUEST = 0xC0
OP_SETUP_POSITIVE = 0xD0
SETUP_NEGATIVE = (0xD6, 0xD7, 0xD8)
OP_BROADCAST_REQUEST = 0x23
OP_BROADCAST_RESPONSE = 0x24

# Channel control opcodes (on the channel's identifiers)
OP_PARAMETERS_REQUEST = 0xA0
OP_PARAMETERS_RESPONSE = 0xA1
OP_CHANNEL_TEST = 0xA3
OP_BREAK = 0xA4
OP_DISCONNECT = 0xA8
CONTROL_OPCODES = {
    OP_PARAMETERS_REQUEST: 'parameters request',
    OP_PARAMETERS_RESPONSE: 'parameters response',
    OP_CHANNEL_TEST: 'channel test',
    OP_BREAK: 'break',
    OP_DISCONNECT: 'disconnect',
}

# Data packet opcodes (high nibble of the first byte)
DATA_WAIT_MORE = 0x0  # Waiting for ACK, more packets follow (block size reached)
DATA_WAIT_LAST = 0x1  # Waiting for ACK, last packet
DATA_MORE = 0x2  # Not waiting for ACK, more packets follow
DATA_LAST = 0x3  # Not waiting for ACK, last packet
ACK_READY = 0xB
ACK_NOT_READY = 0x9
DATA_OPCODES = {
    DATA_WAIT_MORE: 'ACK requested, more follow',
    DATA_WAIT_LAST: 'last, ACK requested',
    DATA_MORE: 'more follow',
    DATA_LAST: 'last',
}

TIMING_UNITS = (0.0001, 0.001, 0.01, 0.1)  # Seconds per step, by the top two bits of a timing byte
TIMING_UNUSED = 0xFF  # T2 and T4 are always 0xFF
MAX_PAYLOAD = 0xFFFF  # The two-byte length of the first packet
FIRST_DATA = 5  # Payload bytes in the first packet, after the two length bytes
NEXT_DATA = 7  # Payload bytes in the following packets
DEFAULT_BLOCK_SIZE = 0x0F
DEFAULT_T1 = 0.1  # Wait for an ACK (the source's example: 0x8A)
DEFAULT_T3 = 0.005  # Interval between two packets (the source's example: 0x32)
KEEP_ALIVE_INTERVAL = 1.0  # Silence after which the client sends a channel test; not given by the source
DISCONNECT_WAIT = 0.5  # How long disconnect() waits for the module's disconnect reply


class Tp20Error(Exception):
    pass


class Tp20NoAnswer(Tp20Error):
    """No module answered the channel setup (nothing at that logical address)."""


class Tp20Refused(Tp20Error):
    """A module answered the channel setup with a negative response."""

    def __init__(self, module: int, opcode: int):
        self.module, self.opcode = module, opcode
        super().__init__(f'Module 0x{module:02X} refused the TP 2.0 channel (0x{opcode:02X})')


# Frame helpers


def encode_channel_id(can_id: Optional[int]) -> bytes:
    """A channel identifier as its two bytes; None is the invalid (unspecified) identifier `00 10`."""
    if can_id is None:
        return b'\x00\x10'
    if not 0 <= can_id <= 0x7FF:
        raise ValueError(f'A TP 2.0 channel identifier is an 11-bit CAN id, got 0x{can_id:X}')
    return bytes([can_id & 0xFF, can_id >> 8])


def decode_channel_id(data: bytes) -> Optional[int]:
    """The identifier of two channel-id bytes, or None when they are marked invalid."""
    if data[1] >> 4:
        return None
    return ((data[1] & 0x0F) << 8) | data[0]


def encode_timing(seconds: float) -> int:
    """A timing byte for a duration: the finest unit whose six-bit scale reaches it (at most 63 × 100 ms)."""
    for index, unit in enumerate(TIMING_UNITS):
        scale = round(seconds / unit)
        if scale <= 0x3F:
            return (index << 6) | scale
    return 0xFF  # 63 × 100 ms, the longest


def decode_timing(value: int) -> float:
    return TIMING_UNITS[value >> 6] * (value & 0x3F)


@dataclass(frozen=True, slots=True)
class ChannelSetup:
    """A channel setup request (0xC0) or response (0xD0, or a negative 0xD6-0xD8) on 0x200 / 0x200 + module.
    `rx_id`/`tx_id` tell the receiver of the frame which identifier to listen on and to transmit on."""

    destination: int
    opcode: int
    rx_id: Optional[int]
    tx_id: Optional[int]
    application: int = APPLICATION_KWP

    def encode(self) -> bytes:
        return (
            bytes([self.destination, self.opcode])
            + encode_channel_id(self.rx_id)
            + encode_channel_id(self.tx_id)
            + bytes([self.application])
        )

    @classmethod
    def parse(cls, data: bytes) -> Optional['ChannelSetup']:
        if len(data) < 7 or data[1] not in (OP_SETUP_REQUEST, OP_SETUP_POSITIVE, *SETUP_NEGATIVE):
            return None
        return cls(data[0], data[1], decode_channel_id(data[2:4]), decode_channel_id(data[4:6]), data[6])

    @property
    def negative(self) -> bool:
        return self.opcode in SETUP_NEGATIVE

    def describe(self) -> str:
        def name(can_id: Optional[int]) -> str:
            return 'any' if can_id is None else f'0x{can_id:03X}'

        ids = f'listen on {name(self.rx_id)}, transmit on {name(self.tx_id)}, application 0x{self.application:02X}'
        if self.opcode == OP_SETUP_REQUEST:
            return f'channel setup request to module 0x{self.destination:02X}: {ids}'
        if self.opcode == OP_SETUP_POSITIVE:
            return f'channel setup accepted (to 0x{self.destination:02X}): {ids}'
        return f'channel setup refused (0x{self.opcode:02X}; the reason is not documented)'


@dataclass(frozen=True, slots=True)
class ChannelParameters:
    """A parameters request (0xA0) or response (0xA1): block size, T1 (wait for an ACK) and T3 (interval between
    packets), in seconds. T2 and T4 are always 0xFF."""

    block_size: int = DEFAULT_BLOCK_SIZE
    t1: float = DEFAULT_T1
    t3: float = DEFAULT_T3
    opcode: int = OP_PARAMETERS_REQUEST

    def encode(self) -> bytes:
        return bytes(
            [self.opcode, self.block_size, encode_timing(self.t1), TIMING_UNUSED, encode_timing(self.t3), TIMING_UNUSED]
        )

    @classmethod
    def parse(cls, data: bytes) -> Optional['ChannelParameters']:
        if len(data) < 6 or data[0] not in (OP_PARAMETERS_REQUEST, OP_PARAMETERS_RESPONSE):
            return None
        return cls(data[1], decode_timing(data[2]), decode_timing(data[4]), data[0])

    def describe(self) -> str:
        return (
            f'{CONTROL_OPCODES[self.opcode]}: block size {self.block_size}, T1 {self.t1 * 1000:g} ms, '
            f'T3 {self.t3 * 1000:g} ms'
        )


def split_payload(payload: bytes) -> List[bytes]:
    """The packet data of a message: the two-byte length and the first five bytes, then seven bytes a packet."""
    length = len(payload)
    if not 0 < length <= MAX_PAYLOAD:
        raise Tp20Error(f'A TP 2.0 message is 1-{MAX_PAYLOAD} bytes, got {length}')
    chunks = [length.to_bytes(2, 'big') + payload[:FIRST_DATA]]
    chunks.extend(payload[offset : offset + NEXT_DATA] for offset in range(FIRST_DATA, length, NEXT_DATA))
    return chunks


def describe_channel_frame(data: bytes) -> Optional[str]:
    """A channel control frame (parameters, channel test, break, disconnect) or an ACK; None for a data packet."""
    if not data:
        return None
    if data[0] in CONTROL_OPCODES:
        parameters = ChannelParameters.parse(data)
        return parameters.describe() if parameters else CONTROL_OPCODES[data[0]]
    if data[0] >> 4 in (ACK_READY, ACK_NOT_READY):
        ready = 'ready' if data[0] >> 4 == ACK_READY else 'not ready'
        return f'ACK, {ready}, next sequence {data[0] & 0x0F}'
    return None


class Tp20Reassembler:
    """Reassembles the data packets of one direction of a channel (passive decoding)."""

    def __init__(self):
        self._data: Optional[bytearray] = None
        self._length = 0
        self._next: Optional[int] = None

    def reset(self) -> None:
        self._data, self._length, self._next = None, 0, None

    def feed(self, data: bytes) -> Tuple[str, Optional[bytes]]:
        """Describe a data packet; returns the payload once its last packet arrives."""
        opcode, sequence = data[0] >> 4, data[0] & 0x0F
        description = f'data #{sequence} ({DATA_OPCODES[opcode]})'
        if self._data is not None and self._next is not None and sequence != self._next:
            self._data = None  # A gap: the message cannot be completed
            description += ', sequence gap'
        self._next = (sequence + 1) & 0x0F
        if self._data is None:
            if len(data) < 3:
                return description, None
            self._length = int.from_bytes(data[1:3], 'big')
            self._data = bytearray(data[3:])
            description += f', {self._length} B message'
        else:
            self._data.extend(data[1:])
        if opcode in (DATA_WAIT_LAST, DATA_LAST):
            received, self._data = self._data, None
            if len(received) >= self._length:
                return description, bytes(received[: self._length])
            return f'{description}, incomplete ({len(received)} of {self._length} B)', None
        return description, None


# The data layer


class Tp20Channel:
    """The data layer of an open TP 2.0 channel, for either end: transmits on `tx_id`, receives on `rx_id`.

    Sends messages in packets — at most `block_size` before waiting for an ACK, `t3` apart — and receives them,
    acknowledging the packets that ask for it. Answers a parameters request, a channel test and a disconnect from
    the other end. `parameters` are this end's (sent in a parameters request or response); `peer` is the other
    end's, once known, and its block size and T3 are honoured when sending (the smaller block size, the longer T3).
    """

    def __init__(
        self,
        session: BusSession,
        tx_id: int,
        rx_id: int,
        parameters: Optional[ChannelParameters] = None,
        timeout: float = 1.0,
    ):
        self.session = session
        self.tx_id, self.rx_id = tx_id, rx_id
        self.parameters = parameters or ChannelParameters()
        self.peer: Optional[ChannelParameters] = None
        self.timeout = timeout
        self.open = True
        self.last_activity = time.monotonic()
        self._tx_sequence = 0
        self._rx_sequence: Optional[int] = None
        self._reception: Optional[bytearray] = None
        self._length = 0
        self._received: asyncio.Queue[Union[bytes, Exception]] = asyncio.Queue()
        self._acks: asyncio.Queue[Union[Tuple[int, int], Exception]] = asyncio.Queue()
        self._parameters: asyncio.Queue[ChannelParameters] = asyncio.Queue()
        self._disconnected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._listening = True
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        """Stop listening (without sending anything)."""
        self.open = False
        if self._listening:
            self.session.remove_message_listener(self._on_message)
            self._listening = False

    @property
    def busy(self) -> bool:
        return self._send_lock.locked()

    # Transmission

    def send_frame(self, data: bytes) -> None:
        try:
            self.session.send(can.Message(arbitration_id=self.tx_id, is_extended_id=False, data=bytes(data)))
        except SessionError as e:
            raise Tp20Error(str(e)) from e
        self.last_activity = time.monotonic()

    def _block_size(self) -> int:
        sizes = [size for size in (self.parameters.block_size, self.peer.block_size if self.peer else 0) if size]
        return min(sizes) if sizes else DEFAULT_BLOCK_SIZE

    def _interval(self) -> float:
        return max(self.parameters.t3, self.peer.t3 if self.peer else 0.0)

    async def send(self, payload: bytes) -> None:
        if not self.open:
            raise Tp20Error('The TP 2.0 channel is closed')
        chunks = split_payload(bytes(payload))
        async with self._send_lock:
            self._drain(self._acks)
            block_size, interval = self._block_size(), self._interval()
            since_ack = 0
            for index, chunk in enumerate(chunks):
                if index:
                    await asyncio.sleep(interval)
                since_ack += 1
                if index == len(chunks) - 1:
                    opcode = DATA_WAIT_LAST
                elif since_ack >= block_size:
                    opcode = DATA_WAIT_MORE
                else:
                    opcode = DATA_MORE
                self.send_frame(bytes([(opcode << 4) | self._tx_sequence]) + chunk)
                self._tx_sequence = (self._tx_sequence + 1) & 0x0F
                if opcode in (DATA_WAIT_MORE, DATA_WAIT_LAST):
                    await self._wait_ack(self._tx_sequence)
                    since_ack = 0

    async def _wait_ack(self, expected: int) -> None:
        """Wait for a ready ACK of the next sequence number, `timeout` at most; a not-ready ACK of it restarts the
        wait. An ACK of another sequence number answers an earlier packet — typically of a send that was cancelled
        while it waited (a stopped live read), whose ACK arrives late — and is skipped, not taken for this one."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        stale: Optional[int] = None
        while True:
            try:
                ack = await asyncio.wait_for(self._acks.get(), max(deadline - loop.time(), 0))
            except asyncio.TimeoutError:
                detail = f' (an ACK for sequence {stale} came, expected {expected})' if stale is not None else ''
                raise Tp20Error(f'Timeout waiting for a TP 2.0 ACK{detail}') from None
            if isinstance(ack, Exception):
                raise ack
            opcode, sequence = ack
            if sequence != expected:
                stale = sequence
                continue
            if opcode == ACK_READY:
                return
            deadline = loop.time() + self.timeout  # Not ready: the module has the packet, wait again

    # Reception

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        try:
            item = await asyncio.wait_for(self._received.get(), self.timeout if timeout is None else timeout)
        except asyncio.TimeoutError:
            raise Tp20Error('Timeout waiting for a response') from None
        if isinstance(item, Exception):
            raise item
        return item

    async def request(self, payload: bytes, timeout: Optional[float] = None) -> bytes:
        self._drain(self._received)
        await self.send(payload)
        return await self.receive(timeout)

    async def wait_parameters(self, timeout: Optional[float] = None) -> ChannelParameters:
        try:
            return await asyncio.wait_for(self._parameters.get(), self.timeout if timeout is None else timeout)
        except asyncio.TimeoutError:
            raise Tp20Error('Timeout waiting for the TP 2.0 channel parameters') from None

    async def wait_disconnected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._disconnected.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    @staticmethod
    def _drain(queue: asyncio.Queue) -> None:
        while not queue.empty():
            queue.get_nowait()

    def _response_parameters(self) -> bytes:
        p = self.parameters
        return ChannelParameters(p.block_size, p.t1, p.t3, OP_PARAMETERS_RESPONSE).encode()

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or msg.arbitration_id != self.rx_id or msg.is_extended_id or not msg.data or not self.open:
            return
        data = bytes(msg.data)
        first = data[0]
        if first in (OP_PARAMETERS_REQUEST, OP_PARAMETERS_RESPONSE):
            parameters = ChannelParameters.parse(data)
            if parameters is None:
                return
            self.peer = parameters
            if first == OP_PARAMETERS_REQUEST:
                self.send_frame(self._response_parameters())
            else:  # Only the latest is kept: the answers to periodic channel tests would otherwise pile up
                self._drain(self._parameters)
                self._parameters.put_nowait(parameters)
        elif first == OP_CHANNEL_TEST:
            self.send_frame(self._response_parameters())
        elif first == OP_BREAK:
            self._reception = None  # Discard everything since the last ACK
        elif first == OP_DISCONNECT:
            self.send_frame(bytes([OP_DISCONNECT]))
            self._disconnected.set()
            error = Tp20Error('The module closed the TP 2.0 channel')
            self._received.put_nowait(error)
            self._acks.put_nowait(error)
            self.close()
        elif first >> 4 in (ACK_READY, ACK_NOT_READY):
            self._acks.put_nowait((first >> 4, first & 0x0F))
        elif first >> 4 in DATA_OPCODES:
            self._on_data(data)

    def _on_data(self, data: bytes) -> None:
        opcode, sequence = data[0] >> 4, data[0] & 0x0F
        if self._reception is not None and self._rx_sequence is not None and sequence != self._rx_sequence:
            self._reception = None  # A lost packet: this message cannot be completed (the requester times out)
        self._rx_sequence = (sequence + 1) & 0x0F
        if self._reception is None:
            if len(data) >= 3:
                self._length = int.from_bytes(data[1:3], 'big')
                self._reception = bytearray(data[3:])
        else:
            self._reception.extend(data[1:])
        if opcode in (DATA_WAIT_MORE, DATA_WAIT_LAST):
            self.send_frame(bytes([(ACK_READY << 4) | self._rx_sequence]))
        if opcode in (DATA_WAIT_LAST, DATA_LAST) and self._reception is not None:
            received, self._reception = self._reception, None
            if len(received) >= self._length:
                self._received.put_nowait(bytes(received[: self._length]))


# The tester end


class Tp20Client:
    """Tester end of a TP 2.0 channel to the module at logical address `module` (e.g. 0x01 engine), over a
    BusSession; a `UdsTransport`, so `KwpClient` (or `UdsClient`) runs over it.

    `open()` (or `async with`) sets the channel up on 0x200, asking the module to transmit on `rx_id`, then
    exchanges channel parameters; a channel test keeps the channel open after `keep_alive` seconds of silence (0
    disables it). `disconnect()` closes it; `close()` does so without waiting for the module's reply.
    """

    def __init__(
        self,
        session: BusSession,
        module: int,
        application: int = APPLICATION_KWP,
        rx_id: int = DEFAULT_RX_ID,
        parameters: Optional[ChannelParameters] = None,
        timeout: float = 1.0,
        keep_alive: float = KEEP_ALIVE_INTERVAL,
        setup_timeout: Optional[float] = None,
    ):
        """`setup_timeout` (default `timeout`) is the wait for the answer to the channel setup alone, e.g. short for a
        scan of addresses where most do not answer."""
        if not 0x00 <= module <= 0xFF:
            raise ValueError(f'A module logical address is 0x00-0xFF, got 0x{module:X}')
        self.session = session
        self.module, self.application, self.requested_rx_id = module, application, rx_id
        self.parameters = parameters or ChannelParameters()
        self.timeout = timeout
        self.setup_timeout = timeout if setup_timeout is None else setup_timeout
        self.keep_alive = keep_alive
        self.channel: Optional[Tp20Channel] = None
        self.setup: Optional[ChannelSetup] = None  # The module's setup response
        self._setups: asyncio.Queue[ChannelSetup] = asyncio.Queue()
        self._keep_alive_task: Optional[asyncio.Task] = None

    @property
    def is_open(self) -> bool:
        return self.channel is not None and self.channel.open

    @property
    def tx_id(self) -> Optional[int]:
        return self.channel.tx_id if self.channel else None

    @property
    def rx_id(self) -> Optional[int]:
        return self.channel.rx_id if self.channel else None

    async def __aenter__(self) -> 'Tp20Client':
        await self.open()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.disconnect()

    def _on_setup(self, msg: can.Message) -> None:
        if msg.is_rx and not msg.is_extended_id and msg.arbitration_id == SETUP_ID + self.module:
            setup = ChannelSetup.parse(bytes(msg.data))
            if setup is not None and setup.opcode != OP_SETUP_REQUEST:
                self._setups.put_nowait(setup)

    async def open(self) -> None:
        """Set the channel up and exchange parameters. Raises Tp20Error if the module does not answer or refuses."""
        if self.is_open:
            return
        request = ChannelSetup(self.module, OP_SETUP_REQUEST, None, self.requested_rx_id, self.application)
        self.session.add_message_listener(self._on_setup)
        try:
            try:
                self.session.send(can.Message(arbitration_id=SETUP_ID, is_extended_id=False, data=request.encode()))
            except SessionError as e:
                raise Tp20Error(str(e)) from e
            try:
                setup = await asyncio.wait_for(self._setups.get(), self.setup_timeout)
            except asyncio.TimeoutError:
                raise Tp20NoAnswer(f'Module 0x{self.module:02X} did not answer the TP 2.0 channel setup') from None
        finally:
            self.session.remove_message_listener(self._on_setup)
        if setup.negative:
            raise Tp20Refused(self.module, setup.opcode)
        if setup.tx_id is None or setup.rx_id is None:
            raise Tp20Error(f'Module 0x{self.module:02X} answered the channel setup without both identifiers')
        self.setup = setup
        channel = Tp20Channel(self.session, setup.tx_id, setup.rx_id, self.parameters, self.timeout)
        self.channel = channel
        try:
            channel.send_frame(self.parameters.encode())
            await channel.wait_parameters()
        except Tp20Error:
            channel.close()
            self.channel = None
            raise
        if self.keep_alive > 0:
            self._keep_alive_task = asyncio.get_running_loop().create_task(self._run_keep_alive(), name='TP 2.0')

    async def _run_keep_alive(self) -> None:
        while self.is_open:
            await asyncio.sleep(self.keep_alive / 4)
            channel = self.channel
            if channel is None or not channel.open:
                return
            if not channel.busy and time.monotonic() - channel.last_activity >= self.keep_alive:
                try:
                    channel.send_frame(bytes([OP_CHANNEL_TEST]))
                except Tp20Error:
                    pass  # Keep trying: the bus may come back

    def _require_channel(self) -> Tp20Channel:
        if self.channel is None or not self.channel.open:
            raise Tp20Error('The TP 2.0 channel is not open')
        return self.channel

    async def send(self, payload: bytes) -> None:
        await self._require_channel().send(payload)

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        return await self._require_channel().receive(timeout)

    async def request(self, payload: bytes, timeout: Optional[float] = None) -> bytes:
        return await self._require_channel().request(payload, timeout)

    def _stop_keep_alive(self) -> None:
        if self._keep_alive_task is not None:
            self._keep_alive_task.cancel()
            self._keep_alive_task = None

    async def disconnect(self) -> None:
        """Send a disconnect and wait briefly for the module's own."""
        self._stop_keep_alive()
        channel = self.channel
        if channel is None:
            return
        if channel.open:
            try:
                channel.send_frame(bytes([OP_DISCONNECT]))
                await channel.wait_disconnected(DISCONNECT_WAIT)
            except Tp20Error:
                pass
        channel.close()
        self.channel = None

    def close(self) -> None:
        """Send a disconnect without waiting for the reply, and stop listening."""
        self._stop_keep_alive()
        channel = self.channel
        if channel is None:
            return
        if channel.open:
            try:
                channel.send_frame(bytes([OP_DISCONNECT]))
            except Tp20Error:
                pass
        channel.close()
        self.channel = None
