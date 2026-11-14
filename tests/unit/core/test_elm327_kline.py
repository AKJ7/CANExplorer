import socket
import threading
import time

import can
import pytest

from can_explorer.core.elm327_kline import (
    DEFAULT_PROTOCOL,
    KLINE_PROTOCOLS,
    Elm327KLineBus,
    init_commands,
    parse_channel,
)


class KLineAdapter:
    """A minimal ELM327 adapter in a K-Line mode behind a TCP socket (like a WiFi dongle): answers AT commands,
    monitors with `AT MA` until interrupted, records commands and sent payloads, and prints response lines for a
    data request. A K-Line frame prints as three header bytes and the data (the adapter removes the checksum)."""

    def __init__(self):
        self.commands = []
        self.sent = []
        self.responses = {}
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
                if self.monitoring:
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
            self.monitoring = True
        elif command.startswith(('AT', 'ST')):
            self._send('OK\r\r>')
        else:
            self.sent.append(command)
            lines = self.responses.get(command, ['NO DATA'])
            self._send(''.join(line + '\r' for line in lines) + '\r>')

    def broadcast(self, line):
        self._send(line + '\r')

    def close(self):
        if hasattr(self, '_connection'):
            self._connection.close()
        self._server.close()


@pytest.fixture
def adapter():
    adapter = KLineAdapter()
    yield adapter
    adapter.close()


def test_parse_channel():
    assert parse_channel('/dev/ttyUSB0') == ('/dev/ttyUSB0', DEFAULT_PROTOCOL)
    assert parse_channel('/dev/ttyUSB0@3') == ('/dev/ttyUSB0', 3)
    assert parse_channel('socket://192.168.0.10:35000@4') == ('socket://192.168.0.10:35000', 4)
    with pytest.raises(can.CanInitializationError):
        parse_channel('/dev/ttyUSB0@9')  # Not a K-Line protocol
    with pytest.raises(can.CanInitializationError):
        parse_channel('/dev/ttyUSB0@iso')


def test_init_commands():
    assert init_commands(3) == ('ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATAL', 'ATSP3')
    assert init_commands(DEFAULT_PROTOCOL)[-1] == f'ATSP{DEFAULT_PROTOCOL}'
    assert set(KLINE_PROTOCOLS) == {3, 4, 5}


def test_fd_rejected():
    with pytest.raises(can.CanInterfaceNotImplementedError):
        Elm327KLineBus('socket://127.0.0.1:1', fd=True)


def test_elm327_kline_bus(adapter):
    """The python-can interface: the chosen K-Line protocol is selected, and monitored and sent frames are
    can.Messages with the three header bytes in the extended id."""
    adapter.responses['010C'] = ['48 6B 10 41 0C 0C 6B']  # Engine speed
    bus = Elm327KLineBus(f'{adapter.url}@3')  # ISO 9141-2
    try:
        assert bus.kline_protocol == 3
        for command in init_commands(3):
            assert command in adapter.commands

        deadline = time.monotonic() + 2  # The bus attaches a frame consumer, so the adapter monitors
        while not adapter.monitoring:
            assert time.monotonic() < deadline, 'Did not start monitoring'
            time.sleep(0.01)

        adapter.broadcast('68 6A F1 41 0D 37')  # A frame on the bus
        msg = bus.recv(2)
        assert msg.is_extended_id and msg.arbitration_id == 0x686AF1 and msg.data == bytes.fromhex('410d37')

        bus.send(can.Message(arbitration_id=0x686AF1, is_extended_id=True, data=bytes.fromhex('010C')))
        assert '010C' in adapter.sent
        response = bus.recv(2)
        assert response.arbitration_id == 0x486B10 and response.data == bytes.fromhex('410c0c6b')
    finally:
        bus.shutdown()
