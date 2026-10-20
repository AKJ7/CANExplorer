import json
import time
import urllib.error
import urllib.request
import uuid

import can
import pytest
from click.testing import CliRunner

from can_explorer.core.remote import RemoteBus, parse_channel
from can_server.cli.cli import cli
from can_server.core.server import BusSpec, ServerThread


def receive(bus, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = bus.recv(timeout=0.05)
        if msg is not None:
            return msg
    raise AssertionError('Timed out waiting for a frame')


@pytest.fixture
def server():
    channel = f'remote-{uuid.uuid4().hex}'
    thread = ServerThread([BusSpec('vehicle', 'virtual', channel, 250_000)], token='secret')
    port = thread.start()
    peer = can.Bus(interface='virtual', channel=channel)  # A node on the server's bus
    yield port, peer
    peer.shutdown()
    thread.stop()


def test_channels_and_bus_specs():
    assert parse_channel('pi:9000/vehicle?token=abc') == ('ws://pi:9000/bus/vehicle?token=abc', 'vehicle')
    assert parse_channel('pi/vehicle', receive_own_messages=True) == ('ws://pi:8765/bus/vehicle?own=1', 'vehicle')
    assert parse_channel('wss://example.org:443/bus/body')[0] == 'wss://example.org:443/bus/body'
    with pytest.raises(ValueError, match='expected HOST:PORT/BUS'):
        parse_channel('pi:8765')
    spec = BusSpec.parse('body=socketcan:can1:500000:2000000')
    assert (spec.name, spec.interface, spec.channel, spec.fd, spec.data_bitrate) == (
        'body',
        'socketcan',
        'can1',
        True,
        2_000_000,
    )
    assert BusSpec.parse('v=virtual:x').bitrate == 500_000
    with pytest.raises(ValueError):
        BusSpec.parse('socketcan:can0')
    assert 'canexplorer_remote' in can.interfaces.VALID_INTERFACES


def test_frames_both_ways(server):
    port, peer = server
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/buses?token=secret') as response:
        buses = json.load(response)
    assert buses[0]['bus'] == 'vehicle' and buses[0]['bitrate'] == 250_000
    with pytest.raises(urllib.error.HTTPError, match='401'):
        urllib.request.urlopen(f'http://127.0.0.1:{port}/buses')

    first = can.Bus(interface='canexplorer_remote', channel=f'127.0.0.1:{port}/vehicle?token=secret')
    second = RemoteBus(f'127.0.0.1:{port}/vehicle?token=secret', receive_own_messages=True)
    try:
        assert (first.info['bus'], first.info['bitrate']) == ('vehicle', 250_000)
        peer.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=[1, 2, 3]))
        for bus in (first, second):
            msg = receive(bus)
            assert (msg.arbitration_id, bytes(msg.data), msg.is_rx, msg.channel) == (
                0x123,
                b'\x01\x02\x03',
                True,
                'vehicle',
            )
        first.send(can.Message(arbitration_id=0x18FEF100, is_extended_id=True, is_fd=False, data=bytes(8)))
        at_peer = receive(peer)
        assert at_peer.arbitration_id == 0x18FEF100 and at_peer.is_extended_id
        seen = receive(second)  # Another client sees it as received from the bus
        assert seen.arbitration_id == 0x18FEF100 and seen.is_rx
        second.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=[2, 1, 0]))
        own = receive(second)  # receive_own_messages: echoed as transmitted
        assert own.arbitration_id == 0x7DF and not own.is_rx
        assert receive(first).arbitration_id == 0x7DF
        assert first.state == can.BusState.ACTIVE
    finally:
        first.shutdown()
        second.shutdown()


def test_connection_errors(server):
    port, _ = server
    with pytest.raises(can.CanInitializationError, match='needs a token'):
        RemoteBus(f'127.0.0.1:{port}/vehicle')
    with pytest.raises(can.CanInitializationError, match="no bus 'chassis'"):
        RemoteBus(f'127.0.0.1:{port}/chassis?token=secret')
    with pytest.raises(can.CanInitializationError, match='Could not connect'):
        RemoteBus('127.0.0.1:1/vehicle', timeout=2)


def test_lost_connection_is_reported():
    channel = f'remote-{uuid.uuid4().hex}'
    thread = ServerThread([BusSpec('vehicle', 'virtual', channel)])
    port = thread.start()
    bus = RemoteBus(f'127.0.0.1:{port}/vehicle')
    thread.stop()
    try:
        with pytest.raises(can.CanOperationError, match='lost'):
            for _ in range(50):
                bus.recv(timeout=0.1)
        with pytest.raises(can.CanOperationError, match='Not connected'):
            bus.send(can.Message(arbitration_id=1, data=[0]))
    finally:
        bus.shutdown()


def test_cli_validates_buses():
    result = CliRunner().invoke(cli, ['--bus', 'nonsense'])
    assert result.exit_code == 2 and 'expected NAME=INTERFACE:CHANNEL' in result.output
    assert 'RemoteBus' in CliRunner().invoke(cli, ['--help']).output


def test_protocol_versions_must_match(server, monkeypatch):
    import can_explorer.core.remote as remote

    port, _ = server
    monkeypatch.setattr(remote, 'PROTOCOL_VERSION', remote.PROTOCOL_VERSION + 1)  # A newer client
    with pytest.raises(can.CanInitializationError, match='another protocol version'):
        RemoteBus(f'127.0.0.1:{port}/vehicle?token=secret')


def test_server_publishes_to_mqtt(mqtt_broker):
    from pathlib import Path

    from can_explorer.core.mqtt import MqttSettings

    database = Path(__file__).resolve().parents[3] / 'examples' / 'simulation' / 'vehicle.dbc'
    channel = f'mqtt-{uuid.uuid4().hex}'
    settings = MqttSettings(
        host='127.0.0.1', port=mqtt_broker.port, frames=True, interval=0.05, allow_send=True, send_allowlist=['7DF']
    )
    server = ServerThread([BusSpec('vehicle', 'virtual', channel)], mqtt=settings, databases={'vehicle': database})
    server.start()
    peer = can.Bus(interface='virtual', channel=channel)
    try:
        hub = server.server.hubs['vehicle']
        assert hub.bus is not None and hub.keep_open  # Open without WebSocket clients
        mqtt_broker.wait_for('canexplorer/vehicle/status')
        time.sleep(0.2)  # Subscribed to the send topic
        peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([0x10, 0x27, 0, 0, 80, 1, 0, 0])))
        topic, payload = mqtt_broker.wait_for('canexplorer/vehicle/signals/EngineData/EngineSpeed')[0]
        assert json.loads(payload) == 2500  # 0x2710 × 0.25 rpm
        assert json.loads(mqtt_broker.wait_for('canexplorer/vehicle/frames')[0][1])[0][1] == 0x100
        mqtt_broker.publish('canexplorer/vehicle/send', '{"id": "7DF", "data": "02 01 0D"}')
        received = receive(peer)
        assert received.arbitration_id == 0x7DF and received.data == b'\x02\x01\x0d'
    finally:
        peer.shutdown()
        server.stop()


def test_cli_mqtt_options(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ['--bus', 'a=virtual:x', '--mqtt-frames'])
    assert result.exit_code != 0 and 'need --mqtt' in result.output
    result = runner.invoke(cli, ['--bus', 'a=virtual:x', '--mqtt', 'http://broker'])
    assert result.exit_code != 0 and 'Invalid MQTT broker' in result.output
    result = runner.invoke(cli, ['--bus', 'a=virtual:x', '--mqtt', 'broker', '--database', f'b={tmp_path}'])
    assert result.exit_code != 0 and 'No such file' in result.output
    dbc = tmp_path / 'x.dbc'
    dbc.write_text('VERSION ""\n')
    result = runner.invoke(cli, ['--bus', 'a=virtual:x', '--mqtt', 'broker', '--database', f'b={dbc}'])
    assert result.exit_code != 0 and "unknown bus 'b'" in result.output
