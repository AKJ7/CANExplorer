import asyncio
import uuid

import can
import pytest
from PyQt6.QtCore import QUrl

from can_explorer.gui.connection_manager import ConnectionManager


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def manager(qapp):
    manager = ConnectionManager()
    yield manager
    manager.close_all()


async def test_live_connection(manager, data_dir, tmp_path):
    channel = f'gui-{uuid.uuid4().hex}'
    errors = []
    manager.error_occurred.connect(errors.append)
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel, 'bitrate': '500000'})
    connection = manager.connections[0]
    connection.open()
    assert connection.connected and connection.can_transmit and not connection.is_replay

    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.dbc')))
    connection.start_recording(QUrl.fromLocalFile(str(tmp_path / 'log.blf')))
    assert connection.recording and connection.database_file.endswith('example.dbc')

    peer = can.Bus(interface='virtual', channel=channel)
    peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([0, 0x10, 90, 1, 0, 0, 0, 0])))
    assert connection.send('7DF', '02 01 0D', False, False, 0, False, False) == ''
    assert 'Invalid hex' in connection.send('xyz', '', False, False, 0, False, False)
    assert 'At most 8' in connection.send('100', ' '.join(['00'] * 9), False, False, 0, False, False)
    await wait_until(lambda: connection.session.trace.total == 2)
    peer.shutdown()

    connection._refresh()
    assert connection.frame_count == 2
    assert connection.trace.rowCount() == 2 and connection.overview.rowCount() == 2
    infos = {connection.trace.display(row, 3): connection.trace.display(row, 6) for row in range(2)}
    assert infos['100'].startswith('EngineData: EngineSpeed=1024 rpm')
    assert infos['7DF'] == ''

    connection.set_filter('7DF')
    assert connection.trace.rowCount() == 1 and connection.filter_error == ''
    connection.set_filter('bogus?')
    assert connection.filter_error and connection.trace.rowCount() == 1

    connection.set_paused(True)
    connection.clear()
    assert connection.trace.rowCount() == 0 and connection.frame_count == 0

    connection.close()
    assert not connection.connected and not connection.recording
    assert errors == []


async def test_replay_connection(manager, tmp_path):
    path = tmp_path / 'replay.asc'
    with can.Logger(path) as logger:
        for i in range(5):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=float(i) / 100))
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'CANopen', False)
    connection = manager.connections[0]
    assert connection.is_replay and not connection.can_transmit and connection.protocol == 'CANopen'
    await wait_until(lambda: connection.state == 'finished')
    connection._refresh()
    assert connection.frame_count == 5
    manager.remove_connection(0)
    assert manager.connections == []


async def test_errors_are_reported(manager):
    errors = []
    manager.error_occurred.connect(errors.append)
    manager.add_connection({'name': 'Broken', 'interface': 'NoSuchBus', 'channel': 'x'})
    manager.connections[0].open()
    assert errors and not manager.connections[0].connected
