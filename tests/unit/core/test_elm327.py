import socket
import threading
import time

import can
import pytest

from can_explorer.core.elm327 import (
    ElmFrame,
    Elm327Bus,
    LineReader,
    bitrate_divisor,
    header_commands,
    parse_frame_line,
    protocol_b_parameters,
)


class ElmAdapter:
    """An ELM327 adapter behind a TCP socket (like a WiFi dongle): answers AT commands, monitors the bus with
    AT MA until interrupted, records commands and sent frames and answers requests with configured responses."""

    def __init__(self, protocol_b=True, device=None, answer=True):
        self.protocol_b, self.device, self.answer = protocol_b, device, answer
        self.commands = []
        self.sent = []  # (id, extended, data) of frames the host sent
        self.responses = {}  # Data sent → lines the adapter prints
        self.monitoring = False
        self.echo = True
        self.header, self.priority, self.extended = 0x7DF, 0x18, False
        self.connection = None
        self._lock = threading.Lock()
        self._server = socket.create_server(('127.0.0.1', 0))
        self.url = f'socket://127.0.0.1:{self._server.getsockname()[1]}'
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _send(self, text):
        with self._lock:
            self.connection.sendall(text.encode('ascii'))

    def _serve(self):
        try:
            self.connection, _ = self._server.accept()
        except OSError:
            return
        line = ''
        while True:
            try:
                data = self.connection.recv(4096)
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
        if not self.answer:
            return
        self.commands.append(command)
        reply = 'OK'
        if self.echo:
            self._send(command + '\r')
        if command == '':
            reply = 'REPEATED'  # A lone CR repeats the last command: the bus must never send one
        elif command == 'ATZ':
            self.echo = True
            reply = '\rELM327 v1.5'
        elif command == 'ATE0':
            self.echo = False
        elif command == 'ATI':
            reply = 'ELM327 v1.5'
        elif command == 'STI':
            reply = self.device or '?'
        elif command.startswith('ATPB'):
            reply = 'OK' if self.protocol_b else '?'
            self.extended = not int(command[4:6], 16) & 0x80
        elif command.startswith('ATSP'):
            if command[4:] in ('7', '9'):
                self.extended = True
            elif command[4:] in ('6', '8'):
                self.extended = False
        elif command.startswith('ATSH'):
            self.header = int(command[4:], 16)
        elif command.startswith('ATCP'):
            self.priority = int(command[4:], 16)
        elif command == 'ATMA':
            self.monitoring = True
            return
        elif not command.startswith(('AT', 'ST')):
            identifier = (self.priority << 24 | self.header) if self.extended else self.header
            self.sent.append((identifier, self.extended, bytes.fromhex(command)))
            lines = self.responses.get(command, ['NO DATA'])
            self._send(''.join(line + '\r' for line in lines) + '\r>')
            return
        elif command.startswith('ST'):
            reply = '?'
        self._send(reply + '\r\r>')

    def receive(self, line):
        """A frame on the bus, printed while monitoring."""
        self._send(line + '\r')

    def close(self):
        if self.connection is not None:
            self.connection.close()
        self._server.close()


@pytest.fixture
def adapter():
    adapter = ElmAdapter()
    yield adapter
    adapter.close()


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Timed out'
        time.sleep(0.01)


def test_frame_lines():
    assert parse_frame_line('7E8 8 06 41 00 BE 3F A8 13 00') == ElmFrame(
        0x7E8, False, bytes.fromhex('064100be3fa81300')
    )
    assert parse_frame_line('18 DA F1 10 3 02 41 0D') == ElmFrame(0x18DAF110, True, bytes.fromhex('02410d'))
    assert parse_frame_line('18DAF110 2 01 02') == ElmFrame(0x18DAF110, True, b'\x01\x02')
    assert parse_frame_line('7E8 03 41 0D') == ElmFrame(0x7E8, False, bytes.fromhex('03410d'))  # Without DLC
    assert parse_frame_line('123 0 RTR') == ElmFrame(0x123, False, b'', True)
    assert parse_frame_line('7E8 0') == ElmFrame(0x7E8, False, b'')
    for line in (
        'NO DATA',
        'BUFFER FULL',
        'STOPPED',
        'OK',
        '?',
        '7E8 8 01 02 <RX ERROR',
        'SEARCHING...',
        'ELM327 v1.5',
    ):
        assert parse_frame_line(line) is None, line
    reader = LineReader()
    assert reader.feed(b'ATI\r\nELM327') == ['ATI']
    assert reader.feed(b' v1.5\r\r>') == ['ELM327 v1.5', None]


def test_bitrates_and_headers():
    assert [bitrate_divisor(rate) for rate in (500_000, 250_000, 125_000, 50_000, 10_000)] == [1, 2, 4, 10, 50]
    for rate in (1_000_000, 800_000, 300_000, 0):
        with pytest.raises(can.CanInitializationError, match='500 kbit/s divided'):
            bitrate_divisor(rate)
    assert protocol_b_parameters(500_000, False) == 'E0 01' and protocol_b_parameters(125_000, True) == '60 04'
    assert header_commands(0x7DF, False) == ['ATSH 7DF']
    assert header_commands(0x18DB33F1, True) == ['ATCP 18', 'ATSH DB33F1']


def test_monitoring_sending_and_responses(adapter):
    adapter.responses['02010D'] = ['7E8 8 03 41 0D 2A 00 00 00 00']
    bus = can.Bus(interface='elm327', channel=adapter.url, receive_own_messages=True)
    try:
        assert isinstance(bus, Elm327Bus) and bus.info.version == 'ELM327 v1.5' and bus.info.protocol_b
        assert bus.channel_info == f'ELM327 v1.5 on {adapter.url}'
        for command in ('ATE0', 'ATH1', 'ATD1', 'ATCAF0', 'ATCSM0', 'ATPBE001', 'ATSPB'):
            assert command in adapter.commands
        wait_until(lambda: adapter.monitoring)
        adapter.receive('123 2 AA BB')
        adapter.receive('BUFFER FULL')
        received = bus.recv(2)
        assert received.arbitration_id == 0x123 and not received.is_extended_id and received.data == b'\xaa\xbb'
        assert received.is_rx

        bus.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=[2, 1, 0x0D]))
        echo = bus.recv(2)
        assert not echo.is_rx and echo.arbitration_id == 0x7DF
        response = bus.recv(2)
        assert response.arbitration_id == 0x7E8 and response.data == bytes.fromhex('03410d2a00000000')
        assert adapter.sent == [(0x7DF, False, b'\x02\x01\x0d')] and bus.overflows == 1

        bus.send(can.Message(arbitration_id=0x18DB33F1, is_extended_id=True, data=[2, 1, 0]))
        bus.send(can.Message(arbitration_id=0x18DB33F1, is_extended_id=True, data=[2, 1, 0x20]))
        wait_until(lambda: len(adapter.sent) == 3)
        assert adapter.sent[1:] == [(0x18DB33F1, True, b'\x02\x01\x00'), (0x18DB33F1, True, b'\x02\x01\x20')]
        assert adapter.commands.count('ATSHDB33F1') == 1 and 'ATPB6001' in adapter.commands
        wait_until(lambda: adapter.monitoring)  # Back to monitoring after the responses
        assert '' not in adapter.commands
        with pytest.raises(can.CanOperationError, match='1 to 8'):
            bus.send(can.Message(arbitration_id=0x100, data=[]))
    finally:
        bus.shutdown()


def test_listen_only_stn_adapter_and_fallback_protocol():
    adapter = ElmAdapter(protocol_b=False, device='STN2120 v5.6.19')
    try:
        bus = can.Bus(interface='elm327', channel=adapter.url, bitrate=250_000, listen_only=True)
        try:
            assert bus.info.device == 'STN2120 v5.6.19' and not bus.info.protocol_b
            assert bus.channel_info.startswith('STN2120') and 'ATSP8' in adapter.commands
            assert 'ATCSM1' in adapter.commands
            with pytest.raises(can.CanOperationError, match='listen only'):
                bus.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=[1]))
        finally:
            bus.shutdown()
    finally:
        adapter.close()
    adapter = ElmAdapter(protocol_b=False)
    try:
        with pytest.raises(can.CanInitializationError, match='only supports 500 and 250'):
            can.Bus(interface='elm327', channel=adapter.url, bitrate=125_000)
    finally:
        adapter.close()


def test_errors():
    silent = ElmAdapter(answer=False)
    try:
        with pytest.raises(can.CanInitializationError, match='No ELM327 or OBDLink adapter answered'):
            can.Bus(interface='elm327', channel=silent.url, timeout=0.3)
    finally:
        silent.close()
    with pytest.raises(can.CanInitializationError, match='Could not open'):
        can.Bus(interface='elm327', channel='/dev/does-not-exist')
    with pytest.raises(can.CanInterfaceNotImplementedError, match='classic CAN'):
        can.Bus(interface='elm327', channel='COM1', fd=True)
    with pytest.raises(can.CanInitializationError, match='500 kbit/s divided'):
        can.Bus(interface='elm327', channel='COM1', bitrate=1_000_000)


async def test_connection_through_a_session(adapter):
    import asyncio

    from can_explorer.core.session import BusSession
    from can_explorer.core.sources import BusConfig, LiveSource

    session = BusSession(LiveSource(BusConfig('elm327', adapter.url, bitrate=500_000)))
    session.start()
    try:
        deadline = time.monotonic() + 3
        while not adapter.monitoring:
            assert time.monotonic() < deadline, 'Timed out'
            await asyncio.sleep(0.01)
        adapter.receive('7E8 2 01 02')
        while session.trace.total < 1:
            assert time.monotonic() < deadline, 'Timed out'
            await asyncio.sleep(0.01)
        session.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, data=[2, 0x10, 3]))
        while not adapter.sent:
            assert time.monotonic() < deadline, 'Timed out'
            await asyncio.sleep(0.01)
        assert adapter.sent == [(0x7E0, False, b'\x02\x10\x03')]
    finally:
        session.stop()
