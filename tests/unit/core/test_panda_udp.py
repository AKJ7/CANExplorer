import socket
import struct
import threading

import can
import pytest

from can_explorer.core import panda_udp
from can_explorer.core.panda_udp import (
    PandaFrame,
    PandaUdpBus,
    encode_record,
    parse_channel,
    parse_records,
)
from tests.unit.waiting import wait_until as wait_by_deadline


class FakePanda:
    """A WiFi panda's UDP server on localhost: records the subscriptions and streams records to the subscriber."""

    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(0.05)
        self.port = self.socket.getsockname()[1]
        self.received = []
        self.subscriber = None
        self._closed = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._closed:
            try:
                data, address = self.socket.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.received.append(data)
            self.subscriber = address  # Like proxy.c: answer whoever sent last

    def stream(self, *frames, extra=b''):
        self.socket.sendto(b''.join(encode_record(frame) for frame in frames) + extra, self.subscriber)

    def close(self):
        self._closed = True
        self._thread.join(1)
        self.socket.close()


@pytest.fixture
def panda():
    device = FakePanda()
    yield device
    device.close()


def frame(identifier, data, bus=0, extended=False, sent=False, bus_time=0):
    return PandaFrame(identifier, extended, bus, sent, bus_time, data)


def test_parse_channel():
    assert parse_channel('192.168.0.10') == ('192.168.0.10', 1338, None)
    assert parse_channel('192.168.0.10:1400@2') == ('192.168.0.10', 1400, 2)
    assert parse_channel('panda.local@1') == ('panda.local', 1338, 1)
    assert parse_channel('') == ('192.168.0.10', 1338, None)
    assert parse_channel(None) == ('192.168.0.10', 1338, None)
    with pytest.raises(ValueError):
        parse_channel('192.168.0.10@x')
    with pytest.raises(ValueError):
        parse_channel('192.168.0.10:99999')


def test_parse_records_matches_the_panda_layout():
    # Built by hand from panda's parse_can_buffer: f1 = id << 21 (standard) or id << 3 | 4 (extended);
    # f2 = length | bus << 4 | time << 16
    standard = struct.pack('<II', 0x123 << 21, 3 | (1 << 4) | (0xBEEF << 16)) + b'\x01\x02\x03'.ljust(8, b'\x00')
    extended = struct.pack('<II', (0x18DAF110 << 3) | 4, 8 | (2 << 4)) + bytes(range(8))
    sent = struct.pack('<II', 0x7E0 << 21, 2 | (0x80 << 4)) + b'\x10\x20'.ljust(8, b'\x00')
    assert list(parse_records(standard + extended + sent + b'\x00' * 5)) == [
        PandaFrame(0x123, False, 1, False, 0xBEEF, b'\x01\x02\x03'),
        PandaFrame(0x18DAF110, True, 2, False, 0, bytes(range(8))),
        PandaFrame(0x7E0, False, 0, True, 0, b'\x10\x20'),
    ]


def test_parse_records_skips_invalid_length():
    assert list(parse_records(struct.pack('<II', 0x100 << 21, 12) + bytes(8))) == []


@pytest.mark.parametrize(
    'item',
    [
        frame(0x7FF, b'', bus=2, bus_time=0xFFFF),
        frame(0x1FFFFFFF, bytes(range(8)), extended=True),
        frame(0x0, b'\xff', sent=True, bus=1),
    ],
)
def test_encode_parse_round_trip(item):
    assert list(parse_records(encode_record(item))) == [item]


async def test_subscribes_receives_all_buses_and_keeps_alive(panda):
    bus = PandaUdpBus(f'127.0.0.1:{panda.port}', bitrate=500_000, keepalive_interval=0.05)
    try:
        assert await wait_by_deadline(lambda: panda.subscriber is not None)
        assert panda.received[0] == b'hello'
        panda.stream(frame(0x123, b'\x01\x02', bus=0), frame(0x18FEF100, b'\xaa' * 8, bus=2, extended=True))
        first, second = bus.recv(2), bus.recv(2)
        assert (first.arbitration_id, first.data, first.channel, first.is_extended_id) == (0x123, b'\x01\x02', 0, False)
        assert (second.arbitration_id, second.channel, second.is_extended_id) == (0x18FEF100, 2, True)
        assert first.is_rx and first.timestamp > 0
        assert await wait_by_deadline(lambda: len(panda.received) >= 3)  # Resubscribes periodically
        assert bus.recv(0) is None
    finally:
        bus.shutdown()


async def test_single_bus_and_own_messages(panda):
    own = PandaUdpBus(f'127.0.0.1:{panda.port}@1', receive_own_messages=True, keepalive_interval=0)
    try:
        assert await wait_by_deadline(lambda: panda.subscriber is not None)
        panda.stream(frame(0x100, b'\x00', bus=0), frame(0x200, b'\x01', bus=1, sent=True), frame(0x300, b'', bus=1))
        echo, received = own.recv(2), own.recv(2)
        assert (echo.arbitration_id, echo.is_rx, echo.channel) == (0x200, False, 1)
        assert received.arbitration_id == 0x300 and received.is_rx
        assert own.recv(0.05) is None  # The bus 0 frame is filtered out
    finally:
        own.shutdown()


async def test_own_messages_skipped_by_default(panda):
    bus = PandaUdpBus(f'127.0.0.1:{panda.port}@1', keepalive_interval=0)
    try:
        assert await wait_by_deadline(lambda: panda.subscriber is not None)
        panda.stream(frame(0x200, b'\x01', bus=1, sent=True), frame(0x300, b'', bus=1))
        assert bus.recv(2).arbitration_id == 0x300
    finally:
        bus.shutdown()


async def test_ignores_datagrams_from_others(panda):
    bus = PandaUdpBus(f'127.0.0.1:{panda.port}', keepalive_interval=0)
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert await wait_by_deadline(lambda: panda.subscriber is not None)
        other.sendto(encode_record(frame(0x666, b'')), panda.subscriber)
        panda.stream(frame(0x123, b''))
        assert bus.recv(2).arbitration_id == 0x123
    finally:
        other.close()
        bus.shutdown()


def test_send_is_refused_and_fd_rejected(panda):
    bus = PandaUdpBus(f'127.0.0.1:{panda.port}', keepalive_interval=0)
    try:
        with pytest.raises(can.CanOperationError, match='receive only'):
            bus.send(can.Message(arbitration_id=0x123, data=b'\x01'))
    finally:
        bus.shutdown()
        bus.shutdown()  # Idempotent
    with pytest.raises(can.CanInterfaceNotImplementedError):
        PandaUdpBus(f'127.0.0.1:{panda.port}', fd=True)
    with pytest.raises(can.CanInitializationError):
        PandaUdpBus('127.0.0.1@bus')


def test_recv_after_shutdown_returns_none(panda):
    bus = PandaUdpBus(f'127.0.0.1:{panda.port}', keepalive_interval=0)
    bus.shutdown()
    assert bus._recv_internal(0.05) == (None, False)


def test_detect_offers_default_address_only_in_the_panda_network(monkeypatch):
    class Probe:
        def __init__(self, local):
            self.local = local

        def __call__(self, *args):
            return self

        def connect(self, address):
            assert address == ('192.168.0.10', 1338)

        def getsockname(self):
            return (self.local, 0)

        def close(self):
            pass

    monkeypatch.setattr(panda_udp.socket, 'socket', Probe('192.168.0.23'))
    assert PandaUdpBus._detect_available_configs() == [{'interface': 'panda_udp', 'channel': '192.168.0.10'}]
    monkeypatch.setattr(panda_udp.socket, 'socket', Probe('10.0.0.5'))
    assert PandaUdpBus._detect_available_configs() == []


def test_entry_point_is_registered():
    from importlib.metadata import entry_points

    names = {entry.name: entry.value for entry in entry_points(group='can.interface')}
    assert names.get('panda_udp') == 'can_explorer.core.panda_udp:PandaUdpBus'
