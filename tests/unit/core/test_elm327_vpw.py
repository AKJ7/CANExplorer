import asyncio
import socket
import threading
import time

import can
import pytest

from can_explorer.core.elm327_vpw import (
    INIT_COMMANDS,
    Elm327VpwBus,
    Elm327VpwTransport,
    SessionJ1850Transport,
    parse_vpw_line,
    vpw_to_message,
)
from can_explorer.core.j1850 import J1850Client, J1850Message


class VpwAdapter:
    """A minimal ELM327 adapter in J1850 VPW mode behind a TCP socket (like a WiFi dongle): answers AT commands,
    monitors the bus with `AT MA` until interrupted, records commands and sent payloads, and prints configured
    response lines for a data request."""

    def __init__(self):
        self.commands = []
        self.sent = []  # Hex payloads the host transmitted
        self.responses = {}  # Payload hex -> response lines the adapter prints
        self.echo = True
        self.monitoring = False
        self._lock = threading.Lock()
        self._server = socket.create_server(('127.0.0.1', 0))
        self.url = f'socket://127.0.0.1:{self._server.getsockname()[1]}'
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _send(self, text):
        with self._lock:
            self._connection.sendall(text.encode('ascii'))

    def _serve(self):
        try:
            self._connection, _ = self._server.accept()
        except OSError:
            return
        line = ''
        while True:
            try:
                data = self._connection.recv(4096)
            except OSError:
                return
            if not data:
                return
            for char in data.decode('ascii'):
                if self.monitoring:  # Any character stops monitoring and is dropped
                    self.monitoring = False
                    self._send('\r>')
                elif char == '\r':
                    self._handle(line.replace(' ', '').upper())
                    line = ''
                else:
                    line += char

    def _handle(self, command):
        self.commands.append(command)
        if self.echo:
            self._send(command + '\r')
        if command == 'ATZ':
            self.echo = True
            self._send('\rELM327 v1.5\r\r>')
        elif command == 'ATE0':
            self.echo = False
            self._send('OK\r\r>')
        elif command == 'ATMA':
            self.monitoring = True  # Monitor until interrupted: no prompt
        elif command.startswith(('AT', 'ST')):
            self._send('OK\r\r>')
        else:  # A data payload: transmit it and print the configured response
            self.sent.append(command)
            lines = self.responses.get(command, ['NO DATA'])
            self._send(''.join(line + '\r' for line in lines) + '\r>')

    def broadcast(self, line):
        """A frame on the bus, printed while monitoring."""
        self._send(line + '\r')

    def close(self):
        if hasattr(self, '_connection'):
            self._connection.close()
        self._server.close()


@pytest.fixture
def adapter():
    adapter = VpwAdapter()
    yield adapter
    adapter.close()


async def wait_until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out'
        await asyncio.sleep(0.01)


def test_parse_vpw_line():
    assert parse_vpw_line('48 6B 10 41 0C 0C 6B') == J1850Message(0x48, 0x6B, 0x10, bytes.fromhex('410c0c6b'))
    assert parse_vpw_line('6C 10 F1 43 01 43 01 71') == J1850Message(0x6C, 0x10, 0xF1, bytes.fromhex('4301430171'))
    for line in ('NO DATA', 'OK', '?', 'STOPPED', 'BUS INIT', 'UNABLE TO CONNECT', '48 6B 10 <RX ERROR', '41 0C'):
        assert parse_vpw_line(line) is None, line


async def test_diagnostics_round_trip(adapter):
    adapter.responses['010C'] = ['48 6B 10 41 0C 0C 6B']  # Engine speed
    adapter.responses['03'] = ['48 6B 10 43 01 43 01 71']  # Two stored DTCs: P0143, P0171
    loop = asyncio.get_running_loop()
    transport = await loop.run_in_executor(None, Elm327VpwTransport, adapter.url)
    try:
        client = J1850Client(transport)
        assert transport.version == 'ELM327 v1.5'
        for command in ('ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP2', 'ATAL'):
            assert command in adapter.commands
        # With no frame consumer attached, the adapter is never put into monitoring.
        assert 'ATMA' not in adapter.commands

        assert await client.current_data(0x0C) == (256 * 0x0C + 0x6B) / 4
        assert adapter.sent == ['010C'] and adapter.commands.count('ATSH686AF1') == 1
        assert await client.read_dtcs() == ['P0143', 'P0171']
        assert adapter.commands.count('ATSH686AF1') == 1  # Functional header set once
    finally:
        transport.close()


async def test_monitoring_and_diagnostics_together(adapter):
    adapter.responses['010C'] = ['48 6B 10 41 0C 0C 6B']
    loop = asyncio.get_running_loop()
    frames = []
    on_frame = lambda frame: loop.call_soon_threadsafe(frames.append, frame)  # noqa: E731
    transport = await loop.run_in_executor(None, lambda: Elm327VpwTransport(adapter.url, on_frame=on_frame))
    try:
        client = J1850Client(transport)
        # A frame consumer is attached, so the adapter monitors the bus; a broadcast frame reaches the consumer.
        await wait_until(lambda: adapter.monitoring)
        adapter.broadcast('6C 10 F1 41 0D 37')  # A PCM broadcast on the bus
        await wait_until(lambda: any(f.data[:1] == b'\x41' and f.source == 0xF1 for f in frames))

        # Diagnostics still work: monitoring is interrupted for the request, then resumes.
        assert await client.current_data(0x0C) == (256 * 0x0C + 0x6B) / 4
        assert adapter.sent == ['010C']
        await wait_until(lambda: adapter.monitoring)  # Back to monitoring after the request
        # The diagnostic response was also delivered to the frame consumer (it is a bus frame too).
        assert any(f.data[:2] == b'\x41\x0c' for f in frames)
    finally:
        transport.close()


class FakeSession:
    """A stand-in BusSession: records sent frames and delivers frames to the registered listeners."""

    def __init__(self):
        self._listeners = []
        self.sent = []

    def add_message_listener(self, callback):
        self._listeners.append(callback)

    def remove_message_listener(self, callback):
        self._listeners.remove(callback)

    def send(self, msg):
        self.sent.append(msg)

    def deliver(self, frame):
        msg = vpw_to_message(frame)
        for callback in list(self._listeners):
            callback(msg)


async def test_session_transport_matches_response_amid_traffic():
    """Diagnostics over a session that carries the whole bus: the client ignores unrelated frames and our own
    transmission, and matches the response to its request by content."""
    session = FakeSession()
    transport = SessionJ1850Transport(session)
    client = J1850Client(transport)

    async def respond():
        await wait_until(lambda: session.sent)
        sent = session.sent[-1]
        assert sent.arbitration_id == 0x686AF1 and bytes(sent.data) == bytes([0x01, 0x0C])
        session.deliver(J1850Message(0x6C, 0x10, 0x40, bytes([0x55, 0x00])))  # Unrelated broadcast, must be ignored
        session.deliver(J1850Message(0x48, 0x6B, 0x10, bytes([0x41, 0x0C, 0x0C, 0x6B])))  # The response

    task = asyncio.ensure_future(respond())
    assert await client.current_data(0x0C) == (256 * 0x0C + 0x6B) / 4
    await task
    transport.close()
    assert session._listeners == []


def test_elm327_vpw_bus(adapter):
    """The python-can interface: monitored and sent frames as can.Messages with the header in the extended id."""
    adapter.responses['010C'] = ['48 6B 10 41 0C 0C 6B']
    bus = Elm327VpwBus(adapter.url)
    try:
        for command in INIT_COMMANDS:
            assert command in adapter.commands
        deadline = time.monotonic() + 2  # The bus attaches a frame consumer, so the adapter monitors
        while not adapter.monitoring:
            assert time.monotonic() < deadline, 'Did not start monitoring'
            time.sleep(0.01)

        adapter.broadcast('6C 10 F1 41 0D 37')  # A PCM broadcast
        msg = bus.recv(2)
        assert msg.is_extended_id and msg.arbitration_id == 0x6C10F1 and msg.data == bytes.fromhex('410d37')

        bus.send(can.Message(arbitration_id=0x686AF1, is_extended_id=True, data=bytes.fromhex('010C')))
        assert '010C' in adapter.sent
        response = bus.recv(2)
        assert response.arbitration_id == 0x486B10 and response.data == bytes.fromhex('410c0c6b')
    finally:
        bus.shutdown()
