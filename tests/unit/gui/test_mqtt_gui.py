import asyncio
import json
import uuid
from pathlib import Path

import can
import pytest
from PyQt6.QtCore import QSettings, QUrl

from can_explorer.gui.app_state import AppState
from can_explorer.gui.connection_manager import ConnectionManager

EXAMPLES = Path(__file__).resolve().parents[3] / 'examples' / 'simulation'


async def until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, 'Timed out'
        await asyncio.sleep(0.02)


@pytest.fixture
def connection(qapp, tmp_path):
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    manager = ConnectionManager(app_state=AppState(settings))
    channel = f'mqtt-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Vehicle', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[0]
    yield connection, channel, manager
    manager.close_all()


async def test_publishing_a_connection(connection, mqtt_broker):
    connection, channel, manager = connection
    mqtt = connection.mqtt
    assert not mqtt.enabled and mqtt.state == '' and mqtt.options['broker'] == 'mqtt://localhost:1883'
    broker = f'mqtt://127.0.0.1:{mqtt_broker.port}'
    assert 'Name the messages' in mqtt.start({'broker': broker, 'allow_send': True, 'allowlist': ''})
    assert 'Invalid MQTT broker' in mqtt.start({'broker': 'http://x'})
    options = {'broker': broker, 'password': 'secret', 'frames': True, 'allow_send': True, 'allowlist': '7DF, 123'}
    assert mqtt.start(options) == ''
    assert mqtt.enabled and mqtt.state == 'waiting for the connection'  # Not connected yet
    stored = manager.app_state.mqtt_options()
    assert stored['broker'] == broker and stored['allowlist'] == '7DF, 123' and 'password' not in stored

    connection.load_database(QUrl.fromLocalFile(str(EXAMPLES / 'vehicle.dbc')))
    connection.open()
    await until(lambda: mqtt.state == 'connected')
    assert mqtt.topic == 'canexplorer/Vehicle'
    await asyncio.sleep(0.1)  # Subscribed to the send topic
    peer = can.Bus(interface='virtual', channel=channel)
    try:
        peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([0x40, 0x1F, 120, 2, 0, 0, 0, 0])))
        await until(lambda: mqtt_broker.messages('canexplorer/Vehicle/signals/EngineData/EngineSpeed'))
        assert json.loads(mqtt_broker.messages('canexplorer/Vehicle/signals/EngineData/EngineSpeed')[0][1]) == 2000
        await until(lambda: mqtt_broker.messages('canexplorer/Vehicle/signals/EngineData/GearPosition'))
        assert mqtt_broker.messages('canexplorer/Vehicle/signals/EngineData/GearPosition')[0][1] == b'"Second"'
        await until(lambda: mqtt_broker.messages('canexplorer/Vehicle/frames'))

        mqtt_broker.publish('canexplorer/Vehicle/send', '{"id": "7DF", "data": "02 01 0D"}')
        loop = asyncio.get_running_loop()
        received = await loop.run_in_executor(None, lambda: peer.recv(3))
        assert received is not None and received.arbitration_id == 0x7DF
        await until(lambda: 'sent' in mqtt.statistics and ', 1 sent' in mqtt.statistics)
    finally:
        peer.shutdown()

    connection.close()  # Disconnected: offline, and back online when connected again
    await until(lambda: mqtt_broker.retained.get('canexplorer/Vehicle/status') == b'offline')
    assert mqtt.enabled and mqtt.state == 'waiting for the connection'
    connection.open()
    await until(lambda: mqtt.state == 'connected')
    mqtt.stop()
    assert not mqtt.enabled and mqtt.statistics == ''
    await until(lambda: mqtt_broker.retained.get('canexplorer/Vehicle/status') == b'offline')
