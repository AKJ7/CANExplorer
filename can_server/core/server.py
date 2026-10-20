"""CAN server: shares CAN interfaces of this machine with CANExplorer instances on the network.

Each configured bus has a name. Clients list the buses with `GET /buses` and connect to `ws://HOST:PORT/bus/NAME`;
the server then streams the bus's frames to them and sends the frames they transmit. With a token configured,
clients must present it (query parameter `token` or the header `Authorization: Bearer TOKEN`).

Messages are JSON text frames:

- server → client: `{"type": "info", "bus": ..., "description": ..., "bitrate": ..., "fd": ...}` once,
  `{"type": "frames", "frames": [FRAME, ...]}`, `{"type": "state", "state": "active"}`, `{"type": "error", ...}`
- client → server: `{"type": "send", "frame": FRAME}`

FRAME is `[timestamp, arbitration_id, flags, data_hex, dlc]` (see `encode_frame`).

With MQTT configured, every bus is opened when the server starts and kept open, and an MQTT bridge publishes its
signals (decoded with the bus's database) and optionally its frames, and sends the frames requested over MQTT
(see can_explorer.core.mqtt).
"""

import asyncio
import functools
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import can
from aiohttp import WSMsgType, web

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.mqtt import MqttBridge, MqttSettings
from can_explorer.core.remote import PROTOCOL_VERSION, RX, decode_frame, encode_frame

logger = logging.getLogger(__name__)

MAX_BATCH = 500
CLIENT_QUEUE = 2000  # Batches buffered per client before frames are dropped (slow network)
STATE_INTERVAL = 1.0


@dataclass
class BusSpec:
    """A bus the server offers: its name and the options of `can.Bus`."""

    name: str
    interface: str
    channel: str
    bitrate: int = 500_000
    fd: bool = False
    data_bitrate: Optional[int] = None

    @classmethod
    def parse(cls, text: str) -> 'BusSpec':
        """'NAME=INTERFACE:CHANNEL[:BITRATE[:DATA_BITRATE]]', e.g. 'vehicle=socketcan:can0:500000'."""
        name, separator, rest = text.partition('=')
        parts = rest.split(':')
        if not separator or not name.strip() or len(parts) < 2 or not all(parts[:2]):
            raise ValueError(f'Invalid bus {text!r}; expected NAME=INTERFACE:CHANNEL[:BITRATE[:DATA_BITRATE]]')
        try:
            bitrate = int(parts[2]) if len(parts) > 2 and parts[2] else 500_000
            data_bitrate = int(parts[3]) if len(parts) > 3 and parts[3] else None
        except ValueError:
            raise ValueError(f'Invalid bitrate in {text!r}') from None
        return cls(name.strip(), parts[0], parts[1], bitrate, data_bitrate is not None, data_bitrate)

    @property
    def description(self) -> str:
        rate = f'{self.bitrate} bit/s' + (f', data {self.data_bitrate} bit/s' if self.fd else '')
        return f'{self.interface}:{self.channel} @ {rate}'

    def info(self) -> Dict[str, Any]:
        return {
            'bus': self.name,
            'description': self.description,
            'bitrate': self.bitrate,
            'fd': self.fd,
            'data_bitrate': self.data_bitrate,
        }


@dataclass(eq=False)
class _Client:
    queue: asyncio.Queue
    receive_own: bool
    ws: Optional[web.WebSocketResponse] = None
    dropped: int = 0


@dataclass(eq=False)
class BusHub:
    """One bus shared by all clients connected to it. Opened with the first client, closed after the last."""

    spec: BusSpec
    clients: Set[_Client] = field(default_factory=set)
    bus: Optional[can.BusABC] = None
    _notifier: Optional[can.Notifier] = None
    _reader: Optional[can.AsyncBufferedReader] = None
    _tasks: List[asyncio.Task] = field(default_factory=list)
    state: str = ''
    listeners: List[Callable[[can.Message], None]] = field(default_factory=list)  # Called with every frame
    keep_open: bool = False  # Open without clients (for MQTT)
    database: Any = None  # Signal database for MQTT

    def open(self) -> None:
        spec = self.spec
        options: Dict[str, Any] = dict(interface=spec.interface, channel=spec.channel, bitrate=spec.bitrate)
        if spec.fd:
            options.update(fd=True, data_bitrate=spec.data_bitrate)
        self.bus = can.Bus(**options)
        loop = asyncio.get_running_loop()
        self._reader = can.AsyncBufferedReader()
        self._notifier = can.Notifier(self.bus, [self._reader], timeout=0.1, loop=loop)
        self._tasks = [asyncio.ensure_future(self._pump()), asyncio.ensure_future(self._watch_state())]
        logger.info(f'Opened bus {spec.name}: {spec.description}')

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None
        if self.bus is not None:
            self.bus.shutdown()
            self.bus = None
            logger.info(f'Closed bus {self.spec.name}')

    def _broadcast(self, frames: List[list], exclude: Optional[_Client] = None) -> None:
        payload = json.dumps({'type': 'frames', 'frames': frames})
        for client in self.clients:
            if client is exclude:
                continue
            try:
                client.queue.put_nowait(payload)
            except asyncio.QueueFull:
                client.dropped += len(frames)

    async def _pump(self) -> None:
        reader = self._reader
        while True:
            batch = [await reader.get_message()]
            while len(batch) < MAX_BATCH and not reader.buffer.empty():
                batch.append(reader.buffer.get_nowait())
            self._broadcast([encode_frame(msg) for msg in batch])
            for listener in self.listeners:
                for msg in batch:
                    listener(msg)

    async def _watch_state(self) -> None:
        while True:
            try:
                state = self.bus.state.name.lower()
            except (NotImplementedError, AttributeError, can.CanError):
                state = ''
            if state != self.state:
                self.state = state
                payload = json.dumps({'type': 'state', 'state': state})
                for client in self.clients:
                    if not client.queue.full():
                        client.queue.put_nowait(payload)
            await asyncio.sleep(STATE_INTERVAL)

    def send(self, msg: can.Message, origin: Optional[_Client] = None) -> None:
        """Send a frame of a client (or of MQTT, without origin); the other clients receive it."""
        if self.bus is None:
            raise can.CanOperationError(f'Bus {self.spec.name} is not open')
        self.bus.send(msg, timeout=0.1)
        msg.timestamp = msg.timestamp or time.time()
        frame = encode_frame(msg)
        self._broadcast([frame[:2] + [frame[2] | RX] + frame[3:]], exclude=origin)  # Other clients receive it
        if origin is not None and origin.receive_own:  # Echoed to the sender as transmitted
            origin.queue.put_nowait(
                json.dumps({'type': 'frames', 'frames': [frame[:2] + [frame[2] & ~RX] + frame[3:]]})
            )


class CanServer:
    def __init__(
        self,
        buses: List[BusSpec],
        token: Optional[str] = None,
        mqtt: Optional[MqttSettings] = None,
        databases: Optional[Dict[str, Path]] = None,
    ):
        """`databases` maps bus names to signal databases, for publishing signals over MQTT."""
        names = [spec.name for spec in buses]
        if len(set(names)) != len(names):
            raise ValueError('Bus names must be unique')
        self.hubs: Dict[str, BusHub] = {spec.name: BusHub(spec) for spec in buses}
        self.token = token
        self.mqtt = mqtt
        self.bridges: Dict[str, MqttBridge] = {}
        for name, path in (databases or {}).items():
            if name not in self.hubs:
                raise ValueError(f'Database for unknown bus {name!r}; buses: {", ".join(self.hubs)}')
            self.hubs[name].database = open_signal_database(path)

    def _authorised(self, request: web.Request) -> bool:
        if not self.token:
            return True
        given = request.query.get('token') or request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        return hmac.compare_digest(given.encode(), self.token.encode())

    def application(self) -> web.Application:
        app = web.Application()
        app.add_routes([web.get('/', self.index), web.get('/buses', self.buses), web.get('/bus/{name}', self.connect)])
        app.on_startup.append(self._start_mqtt)
        app.on_shutdown.append(self._shutdown)
        return app

    async def _start_mqtt(self, app: web.Application) -> None:
        """Open every bus and publish it over MQTT."""
        if self.mqtt is None:
            return
        for name, hub in self.hubs.items():
            hub.keep_open = True
            try:
                hub.open()
            except Exception as e:
                logger.error(f'Could not open bus {name} for MQTT: {e}')
                continue
            if hub.database is None and self.mqtt.signals:
                logger.warning(f'No database for bus {name}: its signals are not published (--database {name}=FILE)')
            bridge = MqttBridge(self.mqtt, name, functools.partial(getattr, hub, 'database'), send=hub.send)
            hub.listeners.append(bridge.on_message)
            bridge.start()
            self.bridges[name] = bridge

    async def _shutdown(self, app: web.Application) -> None:
        for bridge in self.bridges.values():
            bridge.stop()
        for hub in self.hubs.values():
            for client in list(hub.clients):
                if client.ws is not None:
                    await client.ws.close(code=1001, message=b'Server shutting down')
            hub.close()

    def _check(self, request: web.Request) -> None:
        if not self._authorised(request):
            raise web.HTTPUnauthorized(text='Missing or wrong token')

    async def index(self, request: web.Request) -> web.Response:
        self._check(request)
        return web.json_response(
            {'server': 'CANExplorer CAN server', 'protocol': PROTOCOL_VERSION, 'buses': list(self.hubs)}
        )

    async def buses(self, request: web.Request) -> web.Response:
        self._check(request)
        return web.json_response(
            [dict(hub.spec.info(), clients=len(hub.clients), state=hub.state) for hub in self.hubs.values()]
        )

    async def connect(self, request: web.Request) -> web.WebSocketResponse:
        self._check(request)
        hub = self.hubs.get(request.match_info['name'])
        if hub is None:
            raise web.HTTPNotFound(text=f'Unknown bus {request.match_info["name"]!r}; buses: {", ".join(self.hubs)}')
        client_protocol = request.query.get('protocol', str(PROTOCOL_VERSION))
        if client_protocol != str(PROTOCOL_VERSION):
            raise web.HTTPUpgradeRequired(
                text=f'This server speaks protocol version {PROTOCOL_VERSION}, the client {client_protocol}'
            )
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        if hub.bus is None:
            try:
                hub.open()
            except Exception as e:
                logger.error(f'Could not open bus {hub.spec.name}: {e}')
                await ws.send_json({'type': 'error', 'message': f'Could not open {hub.spec.description}: {e}'})
                await ws.close()
                return ws
        client = _Client(asyncio.Queue(CLIENT_QUEUE), request.query.get('own', '0') in ('1', 'true'), ws)
        hub.clients.add(client)
        peer = request.remote
        logger.info(f'Client {peer} connected to {hub.spec.name} ({len(hub.clients)} connected)')
        await ws.send_json({'type': 'info', 'protocol': PROTOCOL_VERSION, 'state': hub.state, **hub.spec.info()})
        writer = asyncio.ensure_future(self._write(ws, client))
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    request_data = json.loads(message.data)
                    if request_data.get('type') == 'send':
                        hub.send(decode_frame(request_data['frame']), client)
                except (ValueError, KeyError, TypeError) as e:
                    await ws.send_json({'type': 'error', 'message': f'Invalid request: {e}'})
                except can.CanError as e:
                    await ws.send_json({'type': 'error', 'message': f'Could not send: {e}'})
        finally:
            writer.cancel()
            hub.clients.discard(client)
            if client.dropped:
                logger.warning(f'{client.dropped} frames to {peer} were dropped (slow connection)')
            logger.info(f'Client {peer} left {hub.spec.name} ({len(hub.clients)} connected)')
            if not hub.clients and not hub.keep_open:
                hub.close()
        return ws

    @staticmethod
    async def _write(ws: web.WebSocketResponse, client: _Client) -> None:
        while True:
            payload = await client.queue.get()
            await ws.send_str(payload)


async def create_server(buses: Optional[List[BusSpec]] = None, token: Optional[str] = None) -> web.Application:
    return CanServer(buses or [], token).application()


class ServerThread:
    """Runs a server on a thread with its own event loop (for tests and embedding)."""

    def __init__(
        self,
        buses: List[BusSpec],
        token: Optional[str] = None,
        host: str = '127.0.0.1',
        port: int = 0,
        mqtt: Optional[MqttSettings] = None,
        databases: Optional[Dict[str, Path]] = None,
    ):
        self.server = CanServer(buses, token, mqtt, databases)
        self.host, self.port = host, port
        self._loop = asyncio.new_event_loop()
        self._runner: Optional[web.AppRunner] = None
        self._thread = threading.Thread(target=self._loop.run_forever, name='CAN server', daemon=True)

    def start(self) -> int:
        """Start serving; returns the port (a free one when 0 was given)."""
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(10)
        return self.port

    async def _start(self) -> None:
        self._runner = web.AppRunner(self.server.application())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._runner is not None:
            asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
