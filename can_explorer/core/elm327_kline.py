"""K-Line diagnostics over an ELM327/OBDLink adapter: a python-can bus (interface "elm327_kline", class
Elm327KLineBus) for the ISO 9141-2 and ISO 14230 (Keyword Protocol 2000) K-Line buses of OBD-II vehicles from
roughly 1996 to 2008 that do not use CAN — many European and Asian cars, and US cars before CAN was mandated.

K-Line is a single-wire 10.4 kbit/s serial bus, not CAN. An ELM327-compatible adapter speaks it when set to one of
its K-Line protocols:

- `3` — ISO 9141-2 (5-baud slow initialisation)
- `4` — ISO 14230-4 KWP (5-baud slow initialisation)
- `5` — ISO 14230-4 KWP (fast initialisation)

A K-Line message has a three-byte header (a format byte, a target address and a source address), a payload and a
trailing checksum — the same shape the adapter prints for J1850, so this backend reuses the serial transport of
`elm327_vpw.py` (`Elm327VpwTransport`) and represents each frame as a python-can frame exactly as the VPW backend
does: the three header bytes packed into the extended arbitration id, the payload as the data. The adapter checks
and removes the checksum on receive and adds it on send. The whole application — the trace, dashboard, reverse
engineering and recording — then works on a K-Line bus like on any interface, and the **KWP2000** and **UDS**
trace decoders describe the service payloads.

The channel is the serial port, as for the `elm327` backend (`COM4`, `/dev/ttyUSB0`, a Bluetooth serial port, or
`socket://192.168.0.10:35000` for a WiFi adapter), with an optional `@<protocol>` suffix to choose the K-Line
protocol — `/dev/ttyUSB0@3` for ISO 9141-2, `@4` or `@5` for ISO 14230. Without a suffix the default is ISO
14230-4 fast init (`5`), the most common on later K-Line vehicles.

Limitations. This is not CAN: it is fixed at 10.4 kbit/s (the bitrate is ignored) and is not CAN FD. K-Line is a
request/response bus with no free-running traffic, so monitoring (`AT MA`) usually shows frames only once a tester
(the OBD-II page, a diagnostic client, or a sent request) has initialised a session; the ELM327 performs the slow
or fast initialisation itself on the first request for the selected protocol. The adapter is half-duplex, so frames
on the bus during a request can be missed, as the CAN and VPW backends document.
"""

import queue
import time
from typing import Any, Optional, Tuple

import can

from can_explorer.core.elm327_vpw import (
    Elm327VpwTransport,
    message_to_vpw,
    vpw_to_message,
)
from can_explorer.core.j1850 import J1850Error, J1850Message

SEND_TIMEOUT = 5.0

# ELM327 K-Line protocols (AT SP N) → the standard they implement
KLINE_PROTOCOLS = {
    3: 'ISO 9141-2',
    4: 'ISO 14230-4 KWP (slow init)',
    5: 'ISO 14230-4 KWP (fast init)',
}
DEFAULT_PROTOCOL = 5  # ISO 14230-4 fast init, the most common on later K-Line vehicles


def parse_channel(channel: str) -> Tuple[str, int]:
    """Split a channel into the serial port and the K-Line protocol number, from an optional `@N` suffix
    (`/dev/ttyUSB0@3`). Defaults to ISO 14230-4 fast init. Raises CanInitializationError for an unknown protocol."""
    port = str(channel)
    protocol = DEFAULT_PROTOCOL
    if '@' in port:
        port, _, suffix = port.rpartition('@')
        try:
            protocol = int(suffix)
        except ValueError:
            raise can.CanInitializationError(
                f'The K-Line protocol after @ must be a number ({_protocol_choices()}), not {suffix!r}'
            ) from None
        if protocol not in KLINE_PROTOCOLS:
            raise can.CanInitializationError(f'Unknown K-Line protocol {protocol}; use one of {_protocol_choices()}')
    return port, protocol


def _protocol_choices() -> str:
    return ', '.join(f'{number} ({name})' for number, name in KLINE_PROTOCOLS.items())


def init_commands(protocol: int) -> Tuple[str, ...]:
    """The ELM327 set-up for K-Line: echo and line feeds off, spaces and headers on, allow long messages, and the
    chosen K-Line protocol. Frames then print as `48 6B 10 41 00 BE 3F A8 13` (three header bytes and the data);
    the adapter checks and removes the K-Line checksum."""
    return ('ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATAL', f'ATSP{protocol}')


class Elm327KLineBus(can.BusABC):
    """A K-Line bus (ISO 9141-2 / ISO 14230 KWP) at the OBD port, through an ELM327 compatible adapter, as a
    python-can interface ("elm327_kline").

    Each K-Line frame is a python-can frame with the three header bytes (format, target, source) in the extended
    arbitration id and the payload as the data, so the whole application works on it like on any CAN interface and
    the KWP2000/UDS trace decoders describe the service payloads. This is not real CAN: it is fixed at 10.4 kbit/s
    (the bitrate is ignored) and is not CAN FD."""

    def __init__(
        self,
        channel: str,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        fd: bool = False,
        **kwargs: Any,
    ):
        if fd:
            raise can.CanInterfaceNotImplementedError('K-Line is a 10.4 kbit/s serial bus, not CAN FD; turn off CAN FD')
        self.port, self.kline_protocol = parse_channel(channel)
        self.receive_own_messages = receive_own_messages
        self._queue: queue.Queue = queue.Queue()
        protocol_name = KLINE_PROTOCOLS[self.kline_protocol]
        try:
            self._transport = Elm327VpwTransport(
                self.port,
                on_frame=self._on_frame,
                init_commands=init_commands(self.kline_protocol),
                label=protocol_name,
            )
        except J1850Error as e:
            raise can.CanInitializationError(str(e)) from e
        name = f'ELM327 K-Line ({protocol_name}) on {self.port}'
        self.channel_info = f'{self._transport.version} ({name})' if self._transport.version else name
        super().__init__(channel=channel, **kwargs)

    def _on_frame(self, frame: J1850Message) -> None:  # Called from the transport's worker thread
        self._queue.put(vpw_to_message(frame, self.port))

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        try:
            item = self._queue.get(timeout=timeout) if timeout is None or timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None, False
        if isinstance(item, Exception):
            raise item
        return item, False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        try:
            self._transport.submit(message_to_vpw(msg), timeout or SEND_TIMEOUT)
        except J1850Error as e:
            raise can.CanOperationError(str(e)) from e
        if self.receive_own_messages:
            echo = msg.__copy__()
            echo.timestamp, echo.is_rx, echo.channel = time.time(), False, self.port
            self._queue.put(echo)

    def shutdown(self) -> None:
        self._transport.close()
        super().shutdown()
