import asyncio
import threading
from collections import deque

import can
import pytest

from can_explorer.core.session import BusSession, SessionState
from can_explorer.core.sources import MAX_BATCH, BusConfig, FileSource, LiveSource, ThreadBridge


async def test_thread_bridge_batches_and_preserves_order():
    bridge = ThreadBridge(asyncio.get_running_loop())
    count = 3 * MAX_BATCH + 17

    def produce():
        for i in range(count):
            bridge.put(i)

    thread = threading.Thread(target=produce)
    thread.start()
    received, batches = [], 0
    while len(received) < count:
        batch = await asyncio.wait_for(bridge.get_batch(), 2)
        assert 0 < len(batch) <= MAX_BATCH
        received.extend(batch)
        batches += 1
    thread.join()
    assert received == list(range(count))
    assert batches < count / 10  # Items were actually batched


class EndlessSource:
    """Test source whose producer thread pushes messages as fast as possible until closed."""

    description = 'endless'
    can_send = False
    echoes_tx = False
    bitrates = None
    bus_state = None

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        self._bridge = ThreadBridge(loop)
        self._running = True
        threading.Thread(target=self._produce, daemon=True).start()

    def _produce(self) -> None:
        msg = can.Message(arbitration_id=1, is_extended_id=False)
        while self._running:
            self._bridge.put(msg)

    def close(self) -> None:
        self._running = False

    async def batches(self):
        while self._running:
            yield await self._bridge.get_batch()


async def test_session_does_not_starve_the_event_loop():
    """A producer that never pauses must not keep the session pump from yielding to other tasks."""
    session = BusSession(EndlessSource(), buffer_size=1_000)
    session.start()
    ticks = 0
    for _ in range(20):
        await asyncio.sleep(0)
        ticks += 1
    session.stop()
    assert ticks == 20 and session.trace.total > 0


async def test_large_file_is_replayed_completely_in_order(tmp_path):
    path = tmp_path / 'large.blf'
    with can.Logger(path) as logger:
        for i in range(12_345):
            logger.on_message_received(can.Message(arbitration_id=i % 0x800, is_extended_id=False, timestamp=i * 1e-3))
    session = BusSession(FileSource(path, realtime=False), buffer_size=20_000)
    session.start()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while session.state is not SessionState.FINISHED:
        assert loop.time() < deadline
        await asyncio.sleep(0.01)
    assert [frame.message.arbitration_id for frame in session.trace] == [i % 0x800 for i in range(12_345)]


async def test_closing_a_replay_stops_the_reader(tmp_path):
    path = tmp_path / 'slow.asc'
    with can.Logger(path) as logger:
        for i in range(100):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=float(i)))
    source = FileSource(path, realtime=True)
    session = BusSession(source)
    session.start()
    # The first frame comes at once, the next only a second later: close in between. Waiting for the first frame
    # instead of a fixed time keeps the test reliable on slow (CI) machines.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while session.trace.total == 0:
        assert loop.time() < deadline, 'The first frame was not replayed'
        await asyncio.sleep(0.01)
    session.stop()
    assert session.state is SessionState.CLOSED
    reader = source._thread
    await asyncio.to_thread(reader.join, 2)
    assert not reader.is_alive()  # The reader thread ended
    await asyncio.sleep(0.2)
    assert session.trace.total == 1  # No frames after closing


@pytest.mark.parametrize('realtime', [True, False])
async def test_replay_restart(tmp_path, realtime):
    path = tmp_path / 'short.asc'
    with can.Logger(path) as logger:
        for i in range(3):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=i * 0.01))
    session = BusSession(FileSource(path, realtime=realtime))
    for expected in (3, 6):
        session.start()
        while session.state is not SessionState.FINISHED:
            await asyncio.sleep(0.01)
        assert session.trace.total == expected


def test_livesource_socketcand_host_and_port(mocker):
    config = BusConfig('socketcand', 'can0', host='192.168.1.10', port=29536)
    source = LiveSource(config)
    assert source.description == 'socketcand:192.168.1.10:29536/can0 @ 500000 bit/s'

    mock_bus = mocker.patch('can.Bus')
    mocker.patch('can.Notifier')
    loop = asyncio.new_event_loop()
    try:
        source.open(loop)
        mock_bus.assert_called_once()
        _, kwargs = mock_bus.call_args
        assert kwargs['host'] == '192.168.1.10'
        assert kwargs['port'] == 29536
        assert kwargs['channel'] == 'can0'
    finally:
        loop.close()


def test_livesource_socketcand_defaults(mocker):
    config = BusConfig('socketcand', 'can0')
    source = LiveSource(config)
    mock_bus = mocker.patch('can.Bus')
    mocker.patch('can.Notifier')
    loop = asyncio.new_event_loop()
    try:
        source.open(loop)
        mock_bus.assert_called_once()
        _, kwargs = mock_bus.call_args
        assert kwargs['host'] == '127.0.0.1'
        assert kwargs['port'] == 29536
    finally:
        loop.close()


def _handshake_bus(mocker, *reads: bytes):
    from can.interfaces.socketcand import SocketCanDaemonBus

    sock = mocker.MagicMock()
    sock.recv.side_effect = list(reads)
    bus = object.__new__(SocketCanDaemonBus)
    bus.channel = 'can0'
    bus._SocketCanDaemonBus__socket = sock
    bus._SocketCanDaemonBus__tcp_tune = False
    bus._SocketCanDaemonBus__receive_buffer = ''
    bus._SocketCanDaemonBus__message_buffer = deque()
    return bus


def test_socketcand_patch_queues_frames_received_with_handshake(mocker):
    bus = _handshake_bus(mocker, b'< hi >', b'< ok >', b'< ok >< frame 123 1.000000 DEADBEEF >< frame 4')

    bus._expect_msg('< hi >')
    bus._expect_msg('< ok >')
    bus._expect_msg('< ok >')

    # The complete frame is delivered right away, without waiting for the next TCP read
    (message,) = bus._SocketCanDaemonBus__message_buffer
    assert message.arbitration_id == 0x123 and message.data == bytes.fromhex('DEADBEEF')
    assert message.channel == 'can0'
    # The incomplete one stays buffered for python-can to finish
    assert bus._SocketCanDaemonBus__receive_buffer == '< frame 4'


def test_socketcand_patch_waits_for_split_reply(mocker):
    bus = _handshake_bus(mocker, b'< o', b'k >')
    bus._expect_msg('< ok >')
    assert bus._SocketCanDaemonBus__receive_buffer == ''


def test_socketcand_patch_raises_on_error_reply(mocker):
    bus = _handshake_bus(mocker, b'< error could not open bus >')
    with pytest.raises(can.CanError, match='could not open bus'):
        bus._expect_msg('< ok >')


def test_socketcand_patch_raises_when_connection_closes(mocker):
    bus = _handshake_bus(mocker, b'')
    with pytest.raises(can.CanError, match='closed'):
        bus._expect_msg('< hi >')


def test_send_gives_a_slow_serial_adapter_enough_time():
    """A serial adapter (ELM327, GVRET, …) runs a multi-round-trip dialogue per frame, so LiveSource.send must
    allow more than a real CAN interface needs: a send slower than the old 0.1 s budget still succeeds, and the
    bus is given the generous timeout."""
    import time

    from can_explorer.core.sources import SEND_TIMEOUT

    assert SEND_TIMEOUT >= 1.0

    class SlowSerialBus:
        def __init__(self):
            self.timeout = None

        def send(self, msg, timeout=None):
            self.timeout = timeout
            time.sleep(0.2)  # Longer than the former 0.1 s send budget, well within SEND_TIMEOUT
            if timeout is not None and timeout < 0.2:
                raise can.CanOperationError('Sending timed out')

    source = LiveSource(BusConfig('elm327', 'socket://127.0.0.1:1'))
    source._bus = SlowSerialBus()  # Stand in for an opened adapter
    source.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, data=b'\x02\x10\x03'))
    assert source._bus.timeout == SEND_TIMEOUT
