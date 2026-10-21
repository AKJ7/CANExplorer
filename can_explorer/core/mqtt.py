"""MQTT bridge: publishes the decoded signals (and optionally the frames) of a bus to an MQTT broker, and sends
frames that other programs publish, for dashboards and home automation (Home Assistant, Node-RED, Grafana …).

Topics, below `PREFIX/BUS` (e.g. `canexplorer/vehicle`; '/', '+' and '#' in names become '_'):

- `signals/MESSAGE/SIGNAL`: the signal's value as JSON (a number, or the name of a value with a choice), published
  when it changes, at most once per interval
- `frames`: optional, the frames of each interval as a JSON list of [timestamp, id, flags, data hex, dlc]
  (the frame format of can-server, see can_explorer.core.remote)
- `status`: `online` or `offline` (retained; `offline` is the last will when the connection drops)
- `send`: optional and off by default, frames to send: {"message": "EngineControl", "signals": {"TargetSpeed": 2000}}
  or {"id": "7DF", "data": "02 01 0D", "extended": false}. Only messages and identifiers of the allow list are
  sent; errors are published to `errors`.

The MQTT client runs on a thread of its own and reconnects when the broker is not reachable. It needs the
optional package aiomqtt (`uv sync --extra mqtt`).
"""

import asyncio
import contextlib
import json
import logging
import ssl
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import can

from can_explorer.core.frames import parse_can_id, parse_hex_bytes
from can_explorer.core.remote import encode_frame

logger = logging.getLogger(__name__)

RECONNECT_DELAYS = (1, 2, 5, 10, 30)
MAX_FRAMES = 20_000  # Frames buffered between two batches; more are dropped
STATUS_ONLINE, STATUS_OFFLINE = 'online', 'offline'
STOP_TIMEOUT = 3.0  # Seconds to wait for the 'offline' status when stopping


@dataclass
class MqttSettings:
    host: str = 'localhost'
    port: int = 1883
    tls: bool = False
    username: str = ''
    password: str = ''
    prefix: str = 'canexplorer'
    signals: bool = True  # Publish decoded signals (needs a signal database)
    frames: bool = False  # Publish the frames
    interval: float = 0.1  # Seconds: at most one value per signal, and one frame batch, per interval
    retain: bool = False  # Retain signal values, so that new subscribers get the last one
    allow_send: bool = False  # Send frames published to the send topic
    send_allowlist: List[str] = field(default_factory=list)  # Message names or hex identifiers; '*' allows all

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> 'MqttSettings':
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in (values or {}).items() if key in known})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_url(cls, url: str, **options: Any) -> 'MqttSettings':
        """Settings from a broker URL: mqtt://[USER[:PASSWORD]@]HOST[:PORT][/PREFIX], mqtts:// for TLS."""
        parts = urlsplit(url if '://' in url else f'mqtt://{url}')
        if parts.scheme not in ('mqtt', 'mqtts') or not parts.hostname:
            raise ValueError(f'Invalid MQTT broker {url!r}; expected mqtt://[USER[:PASSWORD]@]HOST[:PORT][/PREFIX]')
        tls = parts.scheme == 'mqtts'
        settings = cls(
            host=parts.hostname,
            port=parts.port or (8883 if tls else 1883),
            tls=tls,
            username=unquote(parts.username or ''),
            password=unquote(parts.password or ''),
            **options,
        )
        if parts.path.strip('/'):
            settings.prefix = parts.path.strip('/')
        return settings


def topic_part(name: str) -> str:
    """A name as one topic level."""
    return ''.join('_' if char in '/+#' else char for char in str(name)) or '_'


def json_value(value: Any) -> Any:
    """A signal value as JSON: numbers as they are, values with a choice by name."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


def decoded_signals(database, msg: can.Message) -> List[Tuple[str, str, Any]]:
    """(message, signal, value) of a frame, for all messages of a container frame."""
    definition = database.message_definition(msg)
    if definition is None:
        return []
    try:
        if hasattr(database, 'decode_message'):  # cantools based databases
            parts = database.decode_message(definition, msg.data, decode_choices=True)
        else:  # LDF
            parts = [(definition.name, database.decode(msg) or {})]
    except Exception:
        return []
    return [(message, signal, value) for message, values in parts for signal, value in values.items()]


class SendRequestError(ValueError):
    pass


def parse_send_request(payload: bytes, database, allowlist: List[str]) -> can.Message:
    """The frame of a send request, if the allow list permits it."""
    from can_explorer.scripting.api import ScriptError
    from can_explorer.scripting.runtime import message_buffer

    try:
        request = json.loads(payload)
    except ValueError as e:
        raise SendRequestError(f'Invalid JSON: {e}') from None
    if not isinstance(request, dict):
        raise SendRequestError('Expected a JSON object')
    allowed = {str(item).strip().upper() for item in allowlist}
    if 'message' in request:
        name = str(request['message'])
        try:
            buffer = message_buffer(database, name).update(request.get('signals') or {})
            data = buffer.encode()
        except ScriptError as e:
            raise SendRequestError(str(e)) from None
        if not allowed & {'*', name.upper(), f'{buffer.id:X}'}:
            raise SendRequestError(f'Message {name} is not in the allow list')
        return can.Message(
            arbitration_id=buffer.id,
            is_extended_id=buffer.is_extended,
            is_fd=buffer.is_fd,
            bitrate_switch=buffer.is_fd,
            data=data,
        )
    if 'id' in request:
        raw_id = request['id']
        try:
            extended = bool(request.get('extended', False))
            identifier = int(raw_id) if isinstance(raw_id, int) else parse_can_id(str(raw_id), True)
            if not extended and identifier > 0x7FF:
                extended = True
            data = request.get('data', '')
            data = bytes(data) if isinstance(data, list) else parse_hex_bytes(str(data))
        except (ValueError, TypeError) as e:
            raise SendRequestError(str(e)) from None
        if not allowed & {'*', f'{identifier:X}'}:
            raise SendRequestError(f'Identifier {identifier:X} is not in the allow list')
        return can.Message(arbitration_id=identifier, is_extended_id=extended, is_fd=len(data) > 8, data=data)
    raise SendRequestError('Expected "message" or "id"')


class MqttBridge:
    """Publishes the frames of one bus to an MQTT broker and sends the frames requested there.

    The MQTT client runs on its own thread with its own event loop (aiomqtt needs a selector event loop, which the
    Qt event loop on Windows is not, and a slow broker must not hold up the application). Feed the bridge the
    bus's frames with on_message(); `send` and `on_change` are called on the event loop that started the bridge."""

    def __init__(
        self,
        settings: MqttSettings,
        bus_name: str,
        database: Callable[[], Any],
        send: Optional[Callable[[can.Message], None]] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.settings = settings
        self.base = f'{settings.prefix.strip("/")}/{topic_part(bus_name)}'.lstrip('/')
        self._database = database
        self._send = send
        self._on_change = on_change
        self.state = 'stopped'  # stopped, connecting, connected, or the last error
        self.published = 0
        self.sent = 0
        self.rejected = 0
        self.dropped = 0
        self._values: Dict[Tuple[str, str], Any] = {}  # Latest value of each signal
        self._published_values: Dict[Tuple[str, str], Any] = {}
        self._frames: Deque[list] = deque()
        self._caller: Optional[asyncio.AbstractEventLoop] = None  # Loop of send and on_change
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # Loop of the MQTT client
        self._task: Optional[asyncio.Future] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None

    # Input (on the caller's loop)

    def on_message(self, msg: can.Message) -> None:
        if self._thread is None or msg.is_error_frame:
            return
        if self.settings.frames:
            if len(self._frames) < MAX_FRAMES:
                self._frames.append(encode_frame(msg))
            else:
                self.dropped += 1
        database = self._database() if self.settings.signals else None
        if database is not None:
            for message, signal, value in decoded_signals(database, msg):
                self._values[(message, signal)] = json_value(value)

    # Running

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self._caller = asyncio.get_running_loop()
        except RuntimeError:
            self._caller = None
        # A selector loop on every platform (the default loop on Windows is a proactor loop)
        self._loop = asyncio.SelectorEventLoop()
        self._task = None
        self._thread = threading.Thread(target=self._thread_main, name=f'MQTT {self.base}', daemon=True)
        self._set_state('connecting')
        self._thread.start()

    def stop(self) -> None:
        thread, loop = self._thread, self._loop
        self._thread = None
        if thread is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel)
            except RuntimeError:  # The loop is closed: the thread has ended (e.g. without aiomqtt)
                pass
            thread.join(STOP_TIMEOUT)
        self._values.clear()
        self._published_values.clear()
        self._frames.clear()
        self._set_state('stopped')

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _thread_main(self) -> None:
        loop = self._loop
        assert loop is not None
        self._thread_id = threading.get_ident()
        asyncio.set_event_loop(loop)
        self._task = loop.create_task(self._run())
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    def _set_state(self, state: str) -> None:
        self.state = state
        self._notify()

    def _notify(self) -> None:
        if self._on_change is None:
            return
        caller = self._caller
        if caller is None or threading.get_ident() != self._thread_id:
            self._on_change()
        elif not caller.is_closed():
            caller.call_soon_threadsafe(self._on_change)

    async def _send_frame(self, frame: can.Message) -> None:
        """Send on the caller's loop, where the bus is used."""
        send_frame = self._send
        assert send_frame is not None  # Send requests are only received with a send function
        if self._caller is None:
            send_frame(frame)
            return

        async def send() -> None:
            send_frame(frame)

        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(send(), self._caller))

    # MQTT (on the bridge's thread)

    def _client(self):
        import aiomqtt

        settings = self.settings
        return aiomqtt.Client(
            settings.host,
            settings.port,
            username=settings.username or None,
            password=settings.password or None,
            identifier=f'canexplorer-{uuid.uuid4().hex[:12]}',
            will=aiomqtt.Will(f'{self.base}/status', STATUS_OFFLINE, qos=1, retain=True),
            tls_context=ssl.create_default_context() if settings.tls else None,
            timeout=10,
        )

    async def _run(self) -> None:
        try:
            import aiomqtt
        except ImportError:
            self._set_state('MQTT needs the aiomqtt package (uv sync --extra mqtt)')
            return
        attempt = 0
        while True:
            self._set_state('connecting')
            try:
                async with self._client() as client:
                    attempt = 0
                    await client.publish(f'{self.base}/status', STATUS_ONLINE, qos=1, retain=True)
                    self._set_state('connected')
                    logger.info(f'MQTT: publishing {self.base} to {self.settings.host}:{self.settings.port}')
                    tasks: list = []
                    try:
                        tasks.append(asyncio.ensure_future(self._publish(client)))
                        if self.settings.allow_send and self._send is not None:
                            await client.subscribe(f'{self.base}/send', qos=1)
                            tasks.append(asyncio.ensure_future(self._receive(client)))
                        await asyncio.gather(*tasks)
                    except asyncio.CancelledError:
                        # Stopping (the connection dropped or MQTT was switched off): announce offline. This covers
                        # a cancellation anywhere after connecting, e.g. while still subscribing (slow on Windows),
                        # and tolerates a slow or missing acknowledgement so the thread still ends promptly.
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                client.publish(f'{self.base}/status', STATUS_OFFLINE, qos=1, retain=True), 2
                            )
                        raise
                    finally:
                        for task in tasks:
                            task.cancel()
            except aiomqtt.MqttError as e:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                attempt += 1
                self._set_state(f'{e} (retrying in {delay} s)')
                logger.warning(f'MQTT {self.settings.host}:{self.settings.port}: {e}; retrying in {delay} s')
                await asyncio.sleep(delay)

    async def _publish(self, client) -> None:
        settings = self.settings
        while True:
            started = time.monotonic()
            for key, value in list(self._values.items()):
                if self._published_values.get(key, ...) != value:
                    self._published_values[key] = value
                    message, signal = key
                    topic = f'{self.base}/signals/{topic_part(message)}/{topic_part(signal)}'
                    await client.publish(topic, json.dumps(value), retain=settings.retain)
                    self.published += 1
            frames = [self._frames.popleft() for _ in range(len(self._frames))]
            if frames:
                await client.publish(f'{self.base}/frames', json.dumps(frames))
                self.published += 1
            await asyncio.sleep(max(0.0, settings.interval - (time.monotonic() - started)))

    async def _receive(self, client) -> None:
        async for message in client.messages:
            try:
                frame = parse_send_request(message.payload, self._database(), self.settings.send_allowlist)
                await self._send_frame(frame)
                self.sent += 1
            except Exception as e:  # Invalid or refused request, or the bus could not send
                self.rejected += 1
                logger.warning(f'MQTT send request refused: {e}')
                await client.publish(f'{self.base}/errors', json.dumps({'error': str(e)}))
            self._notify()
