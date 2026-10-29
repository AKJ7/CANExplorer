import socket
import threading
import time

import can
import pytest

from can_explorer.core.gvret import (
    BusParameters,
    GvretBus,
    GvretFrame,
    GvretParser,
    TimestampClock,
    encode_frame,
    parse_channel,
    parse_parameters,
    setup_command,
)


class GvretDevice:
    """A GVRET device with two buses behind a TCP socket (like ESP32RET on WiFi): answers the queries, records
    commands and sent frames, and sends frames on request."""

    def __init__(self, parameters=None, answer=True, banner=b'ESP32RET console\r\n'):
        self.parameters = parameters or [BusParameters(True, False, 500_000), BusParameters(False, False, 250_000)]
        self.answer, self.banner = answer, banner
        self.received = bytearray()
        self.setups = []
        self.sent = []  # (bus, id, extended, data) of frames the host sent
        self.connection = None
        self._server = socket.create_server(('127.0.0.1', 0))
        self.url = f'socket://127.0.0.1:{self._server.getsockname()[1]}'
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            self.connection, _ = self._server.accept()
        except OSError:
            return
        self.connection.sendall(self.banner)
        buffer = bytearray()
        while True:
            try:
                data = self.connection.recv(4096)
            except OSError:
                return
            if not data:
                return
            self.received += data
            buffer += data
            self._handle(buffer)

    def _handle(self, buffer):
        while buffer:
            if buffer[0] == 0xE7:
                del buffer[:1]
                continue
            if buffer[0] != 0xF1 or len(buffer) < 2:
                return
            command = buffer[1]
            if command == 0x00:  # A frame to send: id 4, bus, length, data, checksum
                if len(buffer) < 8 or len(buffer) < 9 + buffer[7]:
                    return
                identifier = int.from_bytes(buffer[2:6], 'little')
                length = buffer[7]
                self.sent.append(
                    (buffer[6], identifier & 0x1FFFFFFF, bool(identifier >> 31), bytes(buffer[8 : 8 + length]))
                )
                del buffer[: 9 + length]
            elif command == 0x05:
                if len(buffer) < 10:
                    return
                self.setups.append(bytes(buffer[2:10]))
                del buffer[:10]
            else:
                del buffer[:2]
                if not self.answer:
                    continue
                if command == 0x0C:
                    self.connection.sendall(bytes([0xF1, 0x0C, 2]))
                elif command == 0x07:
                    self.connection.sendall(bytes([0xF1, 0x07, 0x8A, 0x02, 1, 0, 0, 0]))  # Build 650
                elif command == 0x06:
                    payload = b''.join(
                        bytes([int(p.enabled) | (int(p.listen_only) << 4)]) + p.bitrate.to_bytes(4, 'little')
                        for p in self.parameters
                    )
                    self.connection.sendall(bytes([0xF1, 0x06]) + payload)

    def receive(self, bus, identifier, data, extended=False, timestamp_us=1_000_000):
        """A frame the device received on one of its buses, sent to the host."""
        flags = identifier | (1 << 31 if extended else 0)
        packet = (
            bytes([0xF1, 0x00])
            + timestamp_us.to_bytes(4, 'little')
            + flags.to_bytes(4, 'little')
            + bytes([len(data) | (bus << 4)])
            + bytes(data)
            + b'\x00'
        )
        self.connection.sendall(packet)

    def close(self):
        if self.connection is not None:
            self.connection.close()
        self._server.close()


@pytest.fixture
def device():
    device = GvretDevice()
    yield device
    device.close()


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Timed out'
        time.sleep(0.01)


def test_channels_and_encoding():
    assert parse_channel('COM5') == ('COM5', 0) and parse_channel('/dev/ttyACM0@1') == ('/dev/ttyACM0', 1)
    assert parse_channel('socket://192.168.4.1:23@1') == ('socket://192.168.4.1:23', 1)
    assert parse_channel('weird@name') == ('weird@name', 0)
    frame = encode_frame(can.Message(arbitration_id=0x18FEF100, is_extended_id=True, data=[1, 2, 3]), 1)
    assert frame == bytes.fromhex('f1 00 00f1fe98 01 03 010203 00')
    with pytest.raises(can.CanOperationError, match='data frames only'):
        encode_frame(can.Message(arbitration_id=0x100, is_remote_frame=True), 0)
    assert BusParameters(True, True, 500_000).encode() == (0xE0000000 | 500_000).to_bytes(4, 'little')
    assert BusParameters(False, False, 250_000).encode() == (0x80000000 | 250_000).to_bytes(4, 'little')
    assert len(setup_command([BusParameters(True, False, 1), BusParameters(True, False, 2)])) == 10


def test_parser_handles_split_messages_noise_and_unknown_commands():
    frame = bytes.fromhex('f1 00 40420f00 23010000 22 aaf1 00')  # Bus 2, 2 bytes, a data byte 0xF1
    stream = (
        b'GVRET console\r\n' + bytes([0xF1, 0x0C, 3]) + bytes([0xF1, 0x7E]) + frame + bytes([0xF1, 0x09, 0xDE, 0xAD])
    )
    parser = GvretParser()
    messages = []
    for i in range(len(stream)):  # Byte by byte, as a slow serial port delivers them
        messages += list(parser.feed(stream[i : i + 1]))
    assert messages == [
        (0x0C, 3),
        (0x00, GvretFrame(1_000_000, 0x123, False, 2, b'\xaa\xf1')),
        (0x09, b'\xde\xad'),
    ]
    assert parse_parameters(
        bytes([0x11]) + (500_000).to_bytes(4, 'little') + bytes([0]) + (125_000).to_bytes(4, 'little')
    ) == [
        BusParameters(True, True, 500_000),
        BusParameters(False, False, 125_000),
    ]


def test_timestamps_unwrap():
    clock = TimestampClock()
    start = clock(0xFFFF0000)
    assert clock(0xFFFF0000 + 500_000 - (1 << 32)) - start == pytest.approx(0.5)  # Wrapped around
    assert clock(0x00080000) - start > 0.5


def test_bus_receives_and_sends(device):
    bus = can.Bus(interface='gvret', channel=device.url, bitrate=250_000, receive_own_messages=True)
    try:
        assert isinstance(bus, GvretBus) and bus.info.build == 650 and bus.info.buses == 2
        # Bus 0 configured; bus 1 keeps its settings
        wait_until(lambda: device.setups)
        assert device.setups == [
            (0xC0000000 | 250_000).to_bytes(4, 'little') + (0x80000000 | 250_000).to_bytes(4, 'little')
        ]
        assert device.received.startswith(b'\xe7\xe7')

        device.receive(1, 0x200, [9])  # Another bus: not ours
        device.receive(0, 0x18DAF110, [1, 2, 3, 4, 5, 6, 7, 8], extended=True, timestamp_us=2_000_000)
        device.receive(0, 0x123, [], timestamp_us=2_250_000)
        first, second = bus.recv(1), bus.recv(1)
        assert (first.arbitration_id, first.is_extended_id, bytes(first.data), first.is_rx) == (
            0x18DAF110,
            True,
            bytes(range(1, 9)),
            True,
        )
        assert (second.arbitration_id, second.dlc) == (0x123, 0)
        assert second.timestamp - first.timestamp == pytest.approx(0.25)
        assert abs(first.timestamp - time.time()) < 5

        bus.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=[2, 1, 0]))
        echo = bus.recv(1)
        assert (echo.arbitration_id, echo.is_rx) == (0x7DF, False)  # receive_own_messages
        wait_until(lambda: device.sent)
        assert device.sent == [(0, 0x7DF, False, b'\x02\x01\x00')]
    finally:
        bus.shutdown()


def test_bus_one_listen_only(device):
    bus = can.Bus(interface='gvret', channel=f'{device.url}@1', listen_only=True)
    try:
        wait_until(lambda: device.setups)
        # Bus 1 enabled in listen only mode at its configured bitrate; bus 0 unchanged
        assert device.setups == [
            (0xC0000000 | 500_000).to_bytes(4, 'little') + (0xE0000000 | 250_000).to_bytes(4, 'little')
        ]
        device.receive(0, 0x100, [1])
        device.receive(1, 0x101, [2])
        msg = bus.recv(1)
        assert (msg.arbitration_id, msg.channel) == (0x101, 1)
    finally:
        bus.shutdown()


def test_bus_without_settings_leaves_the_device_as_it_is(device):
    bus = can.Bus(interface='gvret', channel=device.url)
    try:
        time.sleep(0.1)
        assert device.setups == []
    finally:
        bus.shutdown()


def test_lost_connection_is_reported(device):
    bus = can.Bus(interface='gvret', channel=device.url)
    try:
        device.connection.shutdown(socket.SHUT_RDWR)
        device.connection.close()
        with pytest.raises(can.CanOperationError, match='lost'):
            bus.recv(2)
    finally:
        bus.shutdown()


def test_opening_errors():
    silent = GvretDevice(answer=False)
    try:
        with pytest.raises(can.CanInitializationError, match='No GVRET device answered'):
            can.Bus(interface='gvret', channel=silent.url, timeout=0.3)
    finally:
        silent.close()
    device = GvretDevice()
    try:
        with pytest.raises(can.CanInitializationError, match='has 2 buses'):
            can.Bus(interface='gvret', channel=f'{device.url}@2')
    finally:
        device.close()
    with pytest.raises(can.CanInitializationError, match='Could not open'):
        can.Bus(interface='gvret', channel='/dev/does-not-exist')
    with pytest.raises(can.CanInterfaceNotImplementedError, match='classic CAN'):
        can.Bus(interface='gvret', channel='COM1', fd=True)


async def test_connection_through_a_session(device):
    import asyncio

    from can_explorer.core.session import BusSession
    from can_explorer.core.sources import BusConfig, LiveSource

    session = BusSession(LiveSource(BusConfig('gvret', device.url, bitrate=125_000)))
    session.start()
    try:
        await asyncio.sleep(0.2)
        device.receive(0, 0x321, [7, 7])
        deadline = time.monotonic() + 2
        while session.trace.total < 1:
            assert time.monotonic() < deadline, 'Timed out'
            await asyncio.sleep(0.01)
        session.send(can.Message(arbitration_id=0x55, is_extended_id=False, data=[1]))
        await asyncio.sleep(0.2)
        assert device.sent == [(0, 0x55, False, b'\x01')]
        assert device.setups[0][:4] == (0xC0000000 | 125_000).to_bytes(4, 'little')
    finally:
        session.stop()


def test_keepalives_are_sent_periodically(device):
    """The bus sends GVRET keepalives (F1 09) on its own, as SavvyCAN does, so the device keeps streaming."""
    bus = can.Bus(interface='gvret', channel=device.url, keepalive_interval=0.05)
    try:
        wait_until(lambda: device.received.count(b'\xf1\x09') >= 3)
        count_before = device.received.count(b'\xf1\x09')
        assert count_before >= 3  # Several over time, not a one-off
    finally:
        bus.shutdown()
    # No keepalives are sent after shutdown
    sent = device.received.count(b'\xf1\x09')
    time.sleep(0.15)
    assert device.received.count(b'\xf1\x09') == sent


def test_no_keepalive_when_disabled(device):
    bus = can.Bus(interface='gvret', channel=device.url, keepalive_interval=0)
    try:
        wait_until(lambda: device.setups or device.received)
        time.sleep(0.1)
        assert device.received.count(b'\xf1\x09') == 0
    finally:
        bus.shutdown()


def test_full_receive_queue_drops_frames_instead_of_growing(device):
    """A consumer that never reads must not make the queue grow without limit: frames are dropped and counted."""
    bus = can.Bus(interface='gvret', channel=device.url, queue_size=5, keepalive_interval=0)
    try:
        for i in range(30):
            device.receive(0, 0x100 + i, [i])
        wait_until(lambda: bus.dropped > 0, timeout=3.0)
        assert bus._queue.qsize() <= 5  # Bounded: it never grows past the limit
        assert bus.dropped >= 20  # The rest were dropped, not buffered forever
    finally:
        bus.shutdown()
