import asyncio
import uuid

import can
import pytest
from PyQt6.QtCore import QCoreApplication, QEvent

from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.main_application import create_qml_engine


async def wait_until(predicate, timeout=4.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


async def recv(peer, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        msg = peer.recv(0)
        if msg is not None:
            return msg
        await asyncio.sleep(0.01)
    return None


@pytest.fixture
def manager(qapp):
    manager = ConnectionManager()
    yield manager
    manager.close_all()


def open_connection(manager, name, channel):
    manager.add_connection({'name': name, 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    return connection


@pytest.fixture
async def two_buses(manager):
    a_channel, b_channel = f'gwa-{uuid.uuid4().hex}', f'gwb-{uuid.uuid4().hex}'
    open_connection(manager, 'A', a_channel)
    open_connection(manager, 'B', b_channel)
    peer_a = can.Bus(interface='virtual', channel=a_channel)
    peer_b = can.Bus(interface='virtual', channel=b_channel)
    yield manager.gateway, peer_a, peer_b
    peer_a.shutdown()
    peer_b.shutdown()


async def test_lists_connections_and_validates_rules(two_buses):
    gateway, _, _ = two_buses
    assert [c['name'] for c in gateway.connections] == ['A', 'B']
    assert all(c['can_transmit'] for c in gateway.connections)
    assert gateway.add_rule({'source': '', 'destination': 'B'}).startswith('Choose')
    assert 'different' in gateway.add_rule({'source': 'A', 'destination': 'A'})
    assert 'Filter' in gateway.add_rule({'source': 'A', 'destination': 'B', 'filter': 'zzz'})
    assert 'Invalid hex new ID' in gateway.add_rule({'source': 'A', 'destination': 'B', 'new_id': 'xyz'})
    assert gateway.rules == []


async def test_forwards_and_counts(two_buses):
    gateway, peer_a, peer_b = two_buses
    assert gateway.add_rule({'name': 'bridge', 'source': 'A', 'destination': 'B', 'filter': '100-1FF'}) == ''
    gateway.set_enabled(True)
    assert gateway.running and gateway.rules[0]['active']
    peer_a.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b'\x01\x02'))
    peer_a.send(can.Message(arbitration_id=0x500, is_extended_id=False, data=b'\xff'))  # Filtered out
    msg = await recv(peer_b)
    assert msg is not None and msg.arbitration_id == 0x123
    await wait_until(lambda: gateway.rules[0]['forwarded'] == 1)
    assert gateway.rules[0]['dropped'] == 0
    gateway.reset_counters()
    assert gateway.rules[0]['forwarded'] == 0


async def test_rule_enable_remove_and_disable_stops_forwarding(two_buses):
    gateway, peer_a, peer_b = two_buses
    gateway.add_rule({'source': 'A', 'destination': 'B'})
    gateway.set_enabled(True)
    gateway.set_rule_enabled(0, False)
    assert not gateway.rules[0]['enabled']
    peer_a.send(can.Message(arbitration_id=0x200, is_extended_id=False, data=b'\x00'))
    assert await recv(peer_b, 0.3) is None  # Disabled rule forwards nothing
    gateway.set_rule_enabled(0, True)
    peer_a.send(can.Message(arbitration_id=0x200, is_extended_id=False, data=b'\x00'))
    assert await recv(peer_b) is not None
    assert gateway.remove_rule(0) == '' and gateway.rules == []
    gateway.clear_rules()


async def test_rule_becomes_inactive_when_connection_closes(two_buses):
    gateway, peer_a, _ = two_buses
    gateway.add_rule({'source': 'A', 'destination': 'B'})
    gateway.set_enabled(True)
    assert gateway.rules[0]['active']
    gateway._manager.connections[1].close()  # Close B
    gateway._manager.connections_changed.emit()
    assert not gateway.rules[0]['active']


async def test_gateway_tool_renders_without_warnings(manager, qapp):
    a_channel, b_channel = f'r-{uuid.uuid4().hex}', f'r-{uuid.uuid4().hex}'
    warnings = []
    engine = create_qml_engine(
        {'metadata': ApplicationMetadata(parent=qapp), 'connectionManager': manager},
        lambda errors: warnings.extend(error.toString() for error in errors),
    )

    async def settle(predicate=lambda: True):
        await wait_until(lambda: (qapp.processEvents(), predicate())[1])
        for _ in range(4):
            await asyncio.sleep(0.01)
            qapp.processEvents()

    open_connection(manager, 'A', a_channel)
    open_connection(manager, 'B', b_channel)
    peer_a = can.Bus(interface='virtual', channel=a_channel)
    root = engine.rootObjects()[0]
    root.setProperty('tool', 'gateway')
    await settle()
    gateway = manager.gateway
    assert gateway.add_rule({'source': 'A', 'destination': 'B', 'filter': '100-1FF', 'new_id': '400'}) == ''
    gateway.set_enabled(True)
    await settle()
    peer_a.send(can.Message(arbitration_id=0x111, is_extended_id=False, data=b'\x01'))
    await settle(lambda: gateway.rules[0]['forwarded'] == 1)
    root.setProperty('tool', '')
    await settle()
    peer_a.shutdown()
    manager.close_all()
    await settle()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert warnings == []
