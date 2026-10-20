import asyncio
import json
from pathlib import Path

import can
import pytest

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.mqtt import (
    MqttBridge,
    MqttSettings,
    SendRequestError,
    json_value,
    parse_send_request,
    topic_part,
)

EXAMPLES = Path(__file__).resolve().parents[3] / 'examples' / 'simulation'


@pytest.fixture
def database():
    return open_signal_database(EXAMPLES / 'vehicle.dbc')


def engine_data(database, speed, temperature=80, gear='First'):
    definition = next(m for m in database.messages() if m.name == 'EngineData')
    data = definition.encode({'EngineSpeed': speed, 'CoolantTemp': temperature, 'GearPosition': gear})
    return can.Message(arbitration_id=definition.frame_id, is_extended_id=False, data=data)


async def until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, 'Timed out'
        await asyncio.sleep(0.01)


def payloads(broker, topic):
    return [
        json.loads(payload) if payload[:1] in b'[{"-0123456789' else payload.decode()
        for _, payload in broker.messages(topic)
    ]


def test_helpers(database):
    assert topic_part('Body/CAN #1+') == 'Body_CAN _1_' and topic_part('') == '_'
    assert json_value(True) == 1 and json_value(2.5) == 2.5 and json_value(object()) != ''
    settings = MqttSettings.from_dict({'host': 'broker', 'port': 8883, 'unknown': 1})
    assert settings.host == 'broker' and MqttSettings.from_dict(settings.to_dict()) == settings


def test_send_requests(database):
    frame = parse_send_request(b'{"message": "EngineData", "signals": {"EngineSpeed": 1500}}', database, ['enginedata'])
    assert frame.arbitration_id == 0x100 and database.decode(frame)['EngineSpeed'] == 1500
    frame = parse_send_request(b'{"id": "7DF", "data": "02 01 0D"}', database, ['7df'])
    assert frame.arbitration_id == 0x7DF and not frame.is_extended_id and frame.data == b'\x02\x01\x0d'
    frame = parse_send_request(b'{"id": 418053374, "data": [1, 2]}', None, ['*'])
    assert frame.is_extended_id and frame.data == b'\x01\x02'
    for payload, allowlist, error in [
        (b'{"id": "123", "data": ""}', ['7DF'], 'not in the allow list'),
        (b'{"message": "VehicleSpeed"}', ['EngineData'], 'not in the allow list'),
        (b'{"message": "Nope"}', ['*'], 'Unknown message'),
        (b'{"message": "EngineData"}', [], 'not in the allow list'),
        (b'{"message": "EngineData"}', ['100'], None),  # By identifier
        (b'not json', ['*'], 'Invalid JSON'),
        (b'[1]', ['*'], 'JSON object'),
        (b'{"x": 1}', ['*'], 'Expected "message" or "id"'),
        (b'{"id": "zz"}', ['*'], 'Invalid hex'),
    ]:
        if error is None:
            assert parse_send_request(payload, database, allowlist).arbitration_id == 0x100
            continue
        with pytest.raises(SendRequestError, match=error):
            parse_send_request(payload, database, allowlist)
    with pytest.raises(SendRequestError, match='Load a signal database'):
        parse_send_request(b'{"message": "EngineData"}', None, ['*'])


async def test_publishing(mqtt_broker, database):
    settings = MqttSettings(host='127.0.0.1', port=mqtt_broker.port, frames=True, interval=0.05, retain=True)
    changes = []
    bridge = MqttBridge(settings, 'Vehicle/1', lambda: database, on_change=lambda: changes.append(bridge.state))
    bridge.on_message(engine_data(database, 100))  # Not started: ignored
    bridge.start()
    await until(lambda: bridge.state == 'connected')
    assert mqtt_broker.retained['canexplorer/Vehicle_1/status'] == b'online'
    for speed in (1000, 1000, 1200):
        bridge.on_message(engine_data(database, speed))
        await asyncio.sleep(0.12)
    await until(lambda: len(mqtt_broker.messages('canexplorer/Vehicle_1/signals/EngineData/EngineSpeed')) == 2)
    assert payloads(mqtt_broker, 'canexplorer/Vehicle_1/signals/EngineData/EngineSpeed') == [1000, 1200]
    assert payloads(mqtt_broker, 'canexplorer/Vehicle_1/signals/EngineData/GearPosition') == ['First']
    assert json.loads(mqtt_broker.retained['canexplorer/Vehicle_1/signals/EngineData/EngineSpeed']) == 1200
    frames = [frame for batch in payloads(mqtt_broker, 'canexplorer/Vehicle_1/frames') for frame in batch]
    assert [frame[1] for frame in frames] == [0x100] * 3 and len(frames[0]) == 5
    assert 'connecting' in changes and 'connected' in changes
    bridge.stop()
    await until(lambda: mqtt_broker.retained['canexplorer/Vehicle_1/status'] == b'offline')
    assert bridge.state == 'stopped' and not bridge.running


async def test_sending_and_reconnecting(mqtt_broker, database, monkeypatch):
    monkeypatch.setattr('can_explorer.core.mqtt.RECONNECT_DELAYS', (0.1,))
    sent = []
    settings = MqttSettings(
        host='127.0.0.1', port=mqtt_broker.port, signals=False, allow_send=True, send_allowlist=['EngineData']
    )
    bridge = MqttBridge(settings, 'bench', lambda: database, send=sent.append)
    bridge.start()
    await until(lambda: bridge.state == 'connected')
    await asyncio.sleep(0.1)  # Subscribed
    mqtt_broker.publish('canexplorer/bench/send', '{"message": "EngineData", "signals": {"EngineSpeed": 900}}')
    mqtt_broker.publish('canexplorer/bench/send', '{"id": "7DF", "data": "02 01 0D"}')
    await until(lambda: bridge.sent == 1 and bridge.rejected == 1)
    assert database.decode(sent[0])['EngineSpeed'] == 900
    errors = payloads(mqtt_broker, 'canexplorer/bench/errors')
    assert errors == [{'error': 'Identifier 7DF is not in the allow list'}]
    bridge.on_message(engine_data(database, 1))  # Signals are off: nothing published
    await asyncio.sleep(0.2)
    assert not mqtt_broker.messages('canexplorer/bench/signals/#')

    mqtt_broker.drop_clients()  # The connection drops: the will says offline, and the bridge reconnects
    await until(lambda: mqtt_broker.connections == 2 and bridge.state == 'connected')
    assert (b'offline') in [payload for _, payload in mqtt_broker.messages('canexplorer/bench/status')]
    assert mqtt_broker.retained['canexplorer/bench/status'] == b'online'
    bridge.stop()


async def test_unreachable_broker_and_missing_package(monkeypatch):
    pytest.importorskip('aiomqtt')
    monkeypatch.setattr('can_explorer.core.mqtt.RECONNECT_DELAYS', (0.1,))
    bridge = MqttBridge(MqttSettings(host='127.0.0.1', port=1), 'x', lambda: None)
    bridge.start()
    await until(lambda: 'retrying' in bridge.state)
    bridge.stop()
    import builtins

    real_import = builtins.__import__

    def without_aiomqtt(name, *args, **kwargs):
        if name == 'aiomqtt':
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_aiomqtt)
    bridge = MqttBridge(MqttSettings(), 'x', lambda: None)
    bridge.start()
    await until(lambda: 'aiomqtt' in bridge.state)
    await until(lambda: not bridge._thread.is_alive())  # Ended, with its event loop closed
    bridge.stop()  # Still stops cleanly
    assert bridge.state == 'stopped'
