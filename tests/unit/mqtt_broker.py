"""A small MQTT 3.1.1 broker for tests: QoS 0 and 1 publishing, subscriptions with wildcards, retained messages,
last wills and an optional user list. Runs on its own thread, records everything published to it."""

import asyncio
import threading
import time
from typing import Dict, List, Optional, Tuple

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK = 1, 2, 3, 4, 8, 9
UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 10, 11, 12, 13, 14


def matches(pattern: str, topic: str) -> bool:
    pattern_levels, topic_levels = pattern.split('/'), topic.split('/')
    for index, level in enumerate(pattern_levels):
        if level == '#':
            return True
        if index >= len(topic_levels) or (level != '+' and level != topic_levels[index]):
            return False
    return len(pattern_levels) == len(topic_levels)


def _length(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte, value = value % 128, value // 128
        encoded.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(encoded)


def _string(text: bytes) -> bytes:
    return len(text).to_bytes(2, 'big') + text


def _packet(kind: int, flags: int, body: bytes) -> bytes:
    return bytes([kind << 4 | flags]) + _length(len(body)) + body


class _Session:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.subscriptions: List[str] = []
        self.will: Optional[Tuple[str, bytes, bool]] = None
        self.client_id = ''


class MqttBroker:
    def __init__(self, users: Optional[Dict[str, str]] = None, port: int = 0):
        self.users = users
        self.published: List[Tuple[str, bytes, bool]] = []  # (topic, payload, retain) of every PUBLISH received
        self.retained: Dict[str, bytes] = {}
        self.connections = 0
        self.refused = 0
        self._sessions: List[_Session] = []
        self._loop = asyncio.new_event_loop()
        self._server: Optional[asyncio.base_events.Server] = None
        self._thread = threading.Thread(target=self._loop.run_forever, name='MQTT broker', daemon=True)
        self.port = port  # 0: a free one

    def start(self) -> int:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(5)
        return self.port

    async def _start(self) -> None:
        self._server = await asyncio.start_server(self._serve, '127.0.0.1', self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        async def close():
            for session in list(self._sessions):
                session.writer.close()
            self._server.close()

        asyncio.run_coroutine_threadsafe(close(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def drop_clients(self) -> None:
        """Close all client connections without a DISCONNECT (their wills are published)."""
        for session in list(self._sessions):
            self._loop.call_soon_threadsafe(session.writer.transport.abort)

    def publish(self, topic: str, payload: bytes | str, retain: bool = False) -> None:
        """Publish to the subscribers, as another client would."""
        data = payload.encode() if isinstance(payload, str) else payload
        self._loop.call_soon_threadsafe(self._route, topic, data, retain)

    def messages(self, pattern: str = '#') -> List[Tuple[str, bytes]]:
        return [(topic, payload) for topic, payload, _ in list(self.published) if matches(pattern, topic)]

    def wait_for(self, pattern: str, timeout: float = 5.0, count: int = 1) -> List[Tuple[str, bytes]]:
        deadline = time.monotonic() + timeout
        while len(self.messages(pattern)) < count:
            assert time.monotonic() < deadline, f'No message on {pattern}; got {[t for t, _ in self.messages()]}'
            time.sleep(0.01)
        return self.messages(pattern)

    def _route(self, topic: str, payload: bytes, retain: bool) -> None:
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        packet = _packet(PUBLISH, 0, _string(topic.encode()) + payload)
        for session in self._sessions:
            if any(matches(pattern, topic) for pattern in session.subscriptions):
                session.writer.write(packet)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = _Session(writer)
        clean = False
        try:
            while True:
                header = await reader.readexactly(1)
                length, shift = 0, 0
                while True:
                    byte = (await reader.readexactly(1))[0]
                    length |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                body = await reader.readexactly(length)
                kind, flags = header[0] >> 4, header[0] & 0x0F
                if kind == CONNECT:
                    if not self._connect(session, body):
                        writer.write(_packet(CONNACK, 0, b'\x00\x05'))  # Not authorised
                        self.refused += 1
                        return
                    self._sessions.append(session)
                    self.connections += 1
                    writer.write(_packet(CONNACK, 0, b'\x00\x00'))
                elif kind == PUBLISH:
                    qos, retain = (flags >> 1) & 0x03, bool(flags & 0x01)
                    topic_length = int.from_bytes(body[:2], 'big')
                    topic = body[2 : 2 + topic_length].decode()
                    offset = 2 + topic_length
                    if qos:
                        writer.write(_packet(PUBACK, 0, body[offset : offset + 2]))
                        offset += 2
                    payload = body[offset:]
                    self.published.append((topic, payload, retain))
                    self._route(topic, payload, retain)
                elif kind == SUBSCRIBE:
                    packet_id, offset, granted, patterns = body[:2], 2, bytearray(), []
                    while offset < len(body):
                        topic_length = int.from_bytes(body[offset : offset + 2], 'big')
                        patterns.append(body[offset + 2 : offset + 2 + topic_length].decode())
                        offset += 2 + topic_length + 1
                        granted.append(0)
                    session.subscriptions += patterns
                    writer.write(_packet(SUBACK, 0, packet_id + bytes(granted)))
                    for topic, payload in self.retained.items():
                        if any(matches(pattern, topic) for pattern in patterns):
                            writer.write(_packet(PUBLISH, 1, _string(topic.encode()) + payload))
                elif kind == UNSUBSCRIBE:
                    writer.write(_packet(UNSUBACK, 0, body[:2]))
                elif kind == PINGREQ:
                    writer.write(_packet(PINGRESP, 0, b''))
                elif kind == DISCONNECT:
                    clean = True
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if session in self._sessions:
                self._sessions.remove(session)
            if not clean and session.will is not None:
                topic, payload, retain = session.will
                self.published.append((topic, payload, retain))
                self._route(topic, payload, retain)
            writer.close()

    def _connect(self, session: _Session, body: bytes) -> bool:
        name_length = int.from_bytes(body[:2], 'big')
        offset = 2 + name_length + 1  # Protocol name and level
        flags = body[offset]
        offset += 3  # Flags and keep alive

        def field() -> bytes:
            nonlocal offset
            length = int.from_bytes(body[offset : offset + 2], 'big')
            value = body[offset + 2 : offset + 2 + length]
            offset += 2 + length
            return value

        session.client_id = field().decode()
        if flags & 0x04:  # Will
            will_topic = field().decode()
            session.will = (will_topic, field(), bool(flags & 0x20))
        username = field().decode() if flags & 0x80 else None
        password = field().decode() if flags & 0x40 else None
        if self.users is not None:
            return username is not None and self.users.get(username) == password
        return True
