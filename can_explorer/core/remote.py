"""Client of `can-server`: a python-can bus (interface "canexplorer_remote", class RemoteBus) for a bus shared by a
CAN server on another machine, e.g. a Raspberry Pi in a vehicle.

The channel is `HOST:PORT/BUS`, optionally with `?token=TOKEN`, e.g. `192.168.1.20:8765/vehicle?token=secret`
(or a full `ws://` / `wss://` URL). The connection runs on a thread with its own event loop, so the bus works like
any other python-can bus.
"""

import asyncio
import json
import queue
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import can
from can.bus import BusState

DEFAULT_PORT = 8765
# Version of the can-server protocol; clients and servers must speak the same one
PROTOCOL_VERSION = 1
CONNECT_TIMEOUT = 5.0
_CLOSED = object()

EXTENDED, REMOTE, FD, BRS, ESI, ERROR, RX = (1 << bit for bit in range(7))


def encode_frame(msg: can.Message) -> list:
    flags = (
        (EXTENDED if msg.is_extended_id else 0)
        | (REMOTE if msg.is_remote_frame else 0)
        | (FD if msg.is_fd else 0)
        | (BRS if msg.bitrate_switch else 0)
        | (ESI if msg.error_state_indicator else 0)
        | (ERROR if msg.is_error_frame else 0)
        | (RX if msg.is_rx else 0)
    )
    return [msg.timestamp, msg.arbitration_id, flags, bytes(msg.data).hex(), msg.dlc]


def decode_frame(frame: list, channel: Any = None) -> can.Message:
    timestamp, arbitration_id, flags, data, dlc = frame
    return can.Message(
        timestamp=float(timestamp),
        arbitration_id=int(arbitration_id),
        is_extended_id=bool(flags & EXTENDED),
        is_remote_frame=bool(flags & REMOTE),
        is_fd=bool(flags & FD),
        bitrate_switch=bool(flags & BRS),
        error_state_indicator=bool(flags & ESI),
        is_error_frame=bool(flags & ERROR),
        is_rx=bool(flags & RX),
        data=bytes.fromhex(data),
        dlc=int(dlc),
        channel=channel,
        check=False,
    )


def parse_channel(channel: str, receive_own_messages: bool = False) -> Tuple[str, str]:
    """The WebSocket URL of a channel 'HOST[:PORT]/BUS[?token=…]' or 'ws://…', and the bus name."""
    text = channel.strip()
    if '://' not in text:
        text = 'ws://' + text
    parts = urlsplit(text)
    if parts.scheme not in ('ws', 'wss', 'http', 'https'):
        raise ValueError(f'Unsupported scheme {parts.scheme!r} in {channel!r}')
    name = parts.path.strip('/').removeprefix('bus/')
    if not parts.hostname or not name or '/' in name:
        raise ValueError(f'Invalid remote channel {channel!r}; expected HOST:PORT/BUS, e.g. raspberrypi:8765/vehicle')
    scheme = {'http': 'ws', 'https': 'wss'}.get(parts.scheme, parts.scheme)
    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    if receive_own_messages:
        query['own'] = '1'
    netloc = parts.netloc if parts.port else f'{parts.netloc}:{DEFAULT_PORT}'
    return urlunsplit((scheme, netloc, f'/bus/{name}', urlencode(query), '')), name


class RemoteBus(can.BusABC):
    """A bus shared by `can-server` on another machine."""

    def __init__(
        self,
        channel: str,
        receive_own_messages: bool = False,
        timeout: float = CONNECT_TIMEOUT,
        **kwargs: Any,  # bitrate, fd, ...: set on the server
    ):
        self.url, self.name = parse_channel(str(channel), receive_own_messages)
        self.channel_info = f'remote {self.name} at {urlsplit(self.url).netloc}'
        self.info: Dict[str, Any] = {}
        self._remote_state = ''
        self._queue: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._closed = False
        self._ws = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name=f'RemoteBus {self.name}', daemon=True)
        self._thread.start()
        self._task = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        if not self._ready.wait(timeout):
            self._stop_loop()
            raise can.CanInitializationError(f'Could not connect to {self.url}: no answer within {timeout:.0f} s')
        if self._error is not None:
            self._stop_loop()
            raise can.CanInitializationError(self._error)
        super().__init__(channel=channel, **kwargs)

    async def _run(self) -> None:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as http:
                separator = '&' if '?' in self.url else '?'
                url = f'{self.url}{separator}protocol={PROTOCOL_VERSION}'
                async with http.ws_connect(url, heartbeat=20) as ws:
                    self._ws = ws
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        self._handle(json.loads(message.data))
                    if not self._ready.is_set():
                        self._error = self._error or f'{self.url} closed the connection'
        except aiohttp.WSServerHandshakeError as e:
            self._error = {
                401: 'The server needs a token: add ?token=… to the channel',
                404: f'The server has no bus {self.name!r}',
                426: f'The server speaks another protocol version than this CANExplorer ({PROTOCOL_VERSION}); '
                'update the older of the two',
            }.get(e.status, f'Could not connect to {self.url}: {e.status} {e.message}')
        except Exception as e:
            self._error = self._error or f'Could not connect to {self.url}: {e}'
        finally:
            self._ws = None
            self._ready.set()
            self._queue.put(_CLOSED)

    def _handle(self, message: Dict[str, Any]) -> None:
        kind = message.get('type')
        if kind == 'frames':
            for frame in message['frames']:
                self._queue.put(decode_frame(frame, self.name))
        elif kind == 'info':
            self.info = message
            self._remote_state = message.get('state', '')
            protocol = message.get('protocol')
            if protocol != PROTOCOL_VERSION:  # Servers that do not check the version of the client themselves
                self._error = (
                    f'The server speaks protocol version {protocol}, this CANExplorer version {PROTOCOL_VERSION}; '
                    'update the older of the two'
                )
            self._ready.set()
        elif kind == 'state':
            self._remote_state = message.get('state', '')
        elif kind == 'error':
            self._error = message.get('message', 'Server error')
            if self._ready.is_set():
                self._queue.put(can.CanOperationError(self._error))

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        try:
            item = self._queue.get(timeout=timeout) if timeout is None or timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None, False
        if item is _CLOSED:
            self._queue.put(_CLOSED)  # Keep reporting it
            if self._closed:
                return None, False
            raise can.CanOperationError(f'Connection to {self.url} lost' + (f': {self._error}' if self._error else ''))
        if isinstance(item, Exception):
            raise item
        return item, False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise can.CanOperationError(f'Not connected to {self.url}')
        payload = json.dumps({'type': 'send', 'frame': encode_frame(msg)})
        future = asyncio.run_coroutine_threadsafe(ws.send_str(payload), self._loop)
        try:
            future.result(timeout if timeout else 2.0)
        except Exception as e:
            raise can.CanOperationError(f'Could not send to {self.url}: {e}') from e

    @property
    def state(self) -> BusState:
        return {'passive': BusState.PASSIVE, 'error': BusState.ERROR}.get(self._remote_state, BusState.ACTIVE)

    def _stop_loop(self) -> None:
        """Close the connection, let the connection task finish, then stop the loop and its thread."""

        async def close():
            if self._ws is not None:
                await self._ws.close()

        if self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(close(), self._loop).result(2)
                self._task.result(3)
            except Exception:
                self._task.cancel()
                try:
                    self._task.result(1)
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(2)
        if not self._loop.is_running() and not self._loop.is_closed():
            self._loop.close()

    def shutdown(self) -> None:
        self._closed = True
        self._stop_loop()
        super().shutdown()

    @staticmethod
    def _detect_available_configs():
        return []
