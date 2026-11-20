"""comma.ai panda over WiFi UDP: a receive-only python-can bus (interface "panda_udp", class PandaUdpBus) for the
CAN stream that the ESP8266 of the original (white) panda sends to UDP port 1338.

The channel is the panda's address, optionally followed by the port and a bus number: `192.168.0.10` (all buses,
the default address of the panda's access point), `192.168.0.10:1338`, `192.168.0.10@1` (bus 1 only). Without a
bus number the frames of all buses are received, each with its bus number as the message's channel.

Protocol (panda's open source `boardesp/proxy.c` and `python/__init__.py` `PandaWifiStreaming`, removed from the
panda repository in August 2020): any datagram sent to port 1338 subscribes its sender to the stream for about 5
seconds; the original client sends `hello`. The panda answers with datagrams of 16-byte records, little endian,
in the layout of the STM32's CAN mailbox:

- bytes 0-3, `f1`: bit 2 extended; the identifier is `f1 >> 3` (29 bit) or `f1 >> 21` (11 bit)
- bytes 4-7, `f2`: bits 0-3 data length, bits 4-11 bus number, bits 16-31 a 16-bit time value
- bytes 8-15: the data, padded

What it does not do: the UDP stream is one way (the panda ignores the content of what it receives), so this bus
cannot send frames, and the bitrate and silent mode are not configured through it. Classic CAN only. Timestamps are
the host's time of arrival: the unit of the panda's 16-bit time value is not documented in those sources. A bus
number with bit 7 set (128 + bus) marks a frame the panda itself transmitted, following panda's convention for
sent-frame echoes; such frames get `is_rx=False`.
"""

import select
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

import can

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

DEFAULT_HOST = '192.168.0.10'  # The panda's address in its own WiFi access point
DEFAULT_PORT = 1338
SUBSCRIBE = b'hello'
# The panda stops streaming 5 s after the last datagram from the host; resubscribe well before that
KEEPALIVE_INTERVAL = 1.0
RECORD = struct.Struct('<II')
RECORD_SIZE = 16
EXTENDED_FLAG = 0x4
SENT_FLAG = 0x80  # Bus number of a frame the panda transmitted: 128 + bus
MAX_DATAGRAM = 0x200 * RECORD_SIZE


@dataclass
class PandaFrame:
    arbitration_id: int
    is_extended_id: bool
    bus: int
    sent: bool
    bus_time: int
    data: bytes


def parse_channel(channel: Optional[str]) -> Tuple[str, int, Optional[int]]:
    """'192.168.0.10:1338@1' → ('192.168.0.10', 1338, 1); port and bus are optional (default port, all buses)."""
    text = str(channel or '').strip()
    bus: Optional[int] = None
    address, separator, suffix = text.rpartition('@')
    if separator:
        if not suffix.isdigit():
            raise ValueError(f'Bus number expected after "@": {text!r}')
        bus, text = int(suffix), address
    host, separator, port_text = text.rpartition(':')
    if separator:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ValueError(f'Port expected after ":": {text!r}')
        port = int(port_text)
    else:
        host, port = text, DEFAULT_PORT
    return host or DEFAULT_HOST, port, bus


def parse_records(datagram: bytes) -> Iterator[PandaFrame]:
    """The CAN records of one datagram; a trailing partial record and records with a length over 8 are skipped."""
    for offset in range(0, len(datagram) - RECORD_SIZE + 1, RECORD_SIZE):
        f1, f2 = RECORD.unpack_from(datagram, offset)
        length = f2 & 0xF
        if length > 8:
            continue
        extended = bool(f1 & EXTENDED_FLAG)
        source = (f2 >> 4) & 0xFF
        yield PandaFrame(
            arbitration_id=(f1 >> 3) & 0x1FFFFFFF if extended else (f1 >> 21) & 0x7FF,
            is_extended_id=extended,
            bus=source & ~SENT_FLAG,
            sent=bool(source & SENT_FLAG),
            bus_time=f2 >> 16,
            data=datagram[offset + 8 : offset + 8 + length],
        )


def encode_record(frame: PandaFrame) -> bytes:
    """A record as the panda sends it (the inverse of parse_records; used to simulate a panda)."""
    if frame.is_extended_id:
        f1 = (frame.arbitration_id << 3) | EXTENDED_FLAG
    else:
        f1 = frame.arbitration_id << 21
    source = frame.bus | (SENT_FLAG if frame.sent else 0)
    f2 = len(frame.data) | (source << 4) | ((frame.bus_time & 0xFFFF) << 16)
    return (RECORD.pack(f1, f2) + frame.data).ljust(RECORD_SIZE, b'\x00')


class PandaUdpBus(can.BusABC):
    """The CAN stream of a WiFi panda, receive only."""

    def __init__(
        self,
        channel: Optional[str] = None,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        fd: bool = False,
        keepalive_interval: float = KEEPALIVE_INTERVAL,
        **kwargs: Any,
    ):
        if fd:
            raise can.CanInterfaceNotImplementedError('The panda WiFi stream carries classic CAN only; turn off CAN FD')
        try:
            self.host, self.port, self.bus = parse_channel(channel)
        except ValueError as e:
            raise can.CanInitializationError(str(e)) from e
        if bitrate is not None:
            logger.info('panda %s: the bitrate is not configured over WiFi UDP; the panda keeps its own', self.host)
        bus_text = 'all buses' if self.bus is None else f'bus {self.bus}'
        self.channel_info = f'panda {self.host}:{self.port} {bus_text} (WiFi UDP, receive only)'
        self.receive_own_messages = receive_own_messages
        self._pending: Deque[can.Message] = deque()
        self._closed = False
        self._refused = False
        self._wake = threading.Event()
        self._keepalive_interval = keepalive_interval
        try:
            address = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            raise can.CanInitializationError(f'Could not resolve {self.host}: {e}') from e
        try:
            self._socket.setblocking(False)
            self._socket.connect(address)  # Only the panda's datagrams are received
            self._subscribe()
        except OSError as e:
            self._socket.close()
            raise can.CanInitializationError(f'Could not reach the panda at {self.host}:{self.port}: {e}') from e
        self._keeper: Optional[threading.Thread] = None
        if keepalive_interval > 0:
            self._keeper = threading.Thread(target=self._keepalive, name=f'PandaUdpBus {self.host}', daemon=True)
            self._keeper.start()
        super().__init__(channel=channel, **kwargs)

    def _subscribe(self) -> None:
        try:
            self._socket.send(SUBSCRIBE)
        except ConnectionRefusedError:  # An earlier datagram was refused (ICMP); the panda may still be booting
            self._note_refused()

    def _note_refused(self) -> None:
        if not self._refused:
            self._refused = True
            logger.warning('panda %s:%d refused the UDP stream; is it a WiFi panda?', self.host, self.port)

    def _keepalive(self) -> None:
        """Resubscribe every interval: the panda stops streaming 5 s after the host's last datagram."""
        while not self._wake.wait(self._keepalive_interval):
            try:
                self._subscribe()
            except OSError:
                if self._closed:
                    return

    def _convert(self, datagram: bytes) -> None:
        now = time.time()
        for frame in parse_records(datagram):
            if self.bus is not None and frame.bus != self.bus:
                continue
            if frame.sent and not self.receive_own_messages:
                continue
            self._pending.append(
                can.Message(
                    timestamp=now,
                    arbitration_id=frame.arbitration_id,
                    is_extended_id=frame.is_extended_id,
                    data=frame.data,
                    channel=frame.bus,
                    is_rx=not frame.sent,
                )
            )

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._pending:
            if self._closed:
                return None, False
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            # Wake up now and then so a shutdown from another thread ends a wait without a timeout
            wait = 0.5 if remaining is None else min(remaining, 0.5)
            try:
                readable, _, _ = select.select([self._socket], [], [], wait)
            except (OSError, ValueError):  # The socket was closed by shutdown()
                return None, False
            if readable:
                try:
                    self._convert(self._socket.recv(MAX_DATAGRAM))
                except ConnectionRefusedError:
                    self._note_refused()
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError as e:
                    if self._closed:
                        return None, False
                    raise can.CanOperationError(f'Receiving from the panda at {self.host} failed: {e}') from e
            elif remaining is not None and remaining <= wait:
                return None, False
        return self._pending.popleft(), False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        raise can.CanOperationError(
            'The panda WiFi UDP stream is receive only; connect the panda over USB to send frames'
        )

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        if self._keeper is not None:
            self._keeper.join(1)
        self._socket.close()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        """The panda's default address, when this computer is in the panda's network (192.168.0.x) — found by a route
        lookup only, nothing is sent."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((DEFAULT_HOST, DEFAULT_PORT))
            local = probe.getsockname()[0]
        except OSError:
            return []
        finally:
            probe.close()
        if local.startswith('192.168.0.') and local != DEFAULT_HOST:
            return [{'interface': 'panda_udp', 'channel': DEFAULT_HOST}]
        return []
