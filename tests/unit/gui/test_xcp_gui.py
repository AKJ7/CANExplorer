import asyncio
import uuid
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QUrl

from can_explorer.core.ccp import CcpSlaveSimulator
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.xcp import XcpSlaveSimulator
from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.main_application import create_qml_engine

DATA = Path(__file__).resolve().parents[1] / 'data'
A2L = Path(__file__).resolve().parents[2] / 'examples' / 'simulation'  # fallback; real one below


async def wait_until(predicate, timeout=4.0):
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


@pytest.fixture
def channel():
    return f'xcp-gui-{uuid.uuid4().hex}'


def open_connection(manager, channel):
    manager.add_connection({'name': 'ECU', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    return connection


@pytest.fixture
async def xcp_slave(channel):
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    slave = XcpSlaveSimulator(session, request_id=0x7E0, response_id=0x7E1)
    yield slave
    slave.stop()
    session.stop()


def a2l_path() -> Path:
    return Path(__file__).resolve().parents[3] / 'examples' / 'calibration' / 'example.a2l'


async def test_connect_read_write_with_a2l(manager, channel, xcp_slave):
    connection = open_connection(manager, channel)
    xcp = connection.xcp
    assert xcp.connect_slave({'protocol': 'XCP', 'request_id': '7E0', 'response_id': '7E1'}) == ''
    await wait_until(lambda: xcp.connected)
    assert xcp.load_a2l(QUrl.fromLocalFile(str(a2l_path()))) == ''
    xcp_slave.memory[0x1000:0x1002] = (6000).to_bytes(2, 'little')
    await xcp._poll_once()  # The refresh timer does not tick reliably headless, so poll explicitly
    engine = next(v for v in xcp.variables if v['name'] == 'EngineSpeed')
    assert engine['value'] == '1500 rpm' and not engine['writable']

    assert xcp.write_variable('IdleTarget', '900') == ''
    await wait_until(lambda: xcp_slave.memory[0x2000:0x2002] == (3600).to_bytes(2, 'little'))  # 900 / 0.25
    assert xcp.write_variable('EngineSpeed', '1') == 'EngineSpeed is a measurement and cannot be written'

    xcp.disconnect_slave()
    await wait_until(lambda: not xcp.connected)


async def test_connect_requires_the_bus(manager, channel):
    manager.add_connection({'name': 'ECU', 'interface': 'VirtualBus', 'channel': channel})
    xcp = manager.connections[-1].xcp
    assert xcp.connect_slave({'protocol': 'XCP'}) == 'Connect to the bus first'


async def test_ccp_with_station_address(manager, channel):
    connection = open_connection(manager, channel)
    slave_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    slave_session.start()
    slave = CcpSlaveSimulator(slave_session, request_id=0x7E0, response_id=0x7E1, station_address=0x0039)
    try:
        xcp = connection.xcp
        assert (
            xcp.connect_slave(
                {
                    'protocol': 'CCP',
                    'request_id': '7E0',
                    'response_id': '7E1',
                    'station_address': '39',
                    'byte_order': 'big',
                }
            )
            == ''
        )
        await wait_until(lambda: xcp.connected)
        assert xcp.byte_order == 'big'
        xcp.disconnect_slave()
        await wait_until(lambda: not xcp.connected)
    finally:
        slave.stop()
        slave_session.stop()


async def test_read_and_write_raw_address(manager, channel, xcp_slave):
    connection = open_connection(manager, channel)
    xcp = connection.xcp
    xcp.connect_slave({'protocol': 'XCP'})
    await wait_until(lambda: xcp.connected)
    assert xcp.write_address('100', 'DE AD BE EF') == ''
    await wait_until(lambda: xcp_slave.memory[0x100:0x104] == bytes.fromhex('DEADBEEF'))
    count = len(xcp.log)
    assert xcp.read_address('100', 4) == ''
    await wait_until(lambda: len(xcp.log) > count)
    assert any('DE AD BE EF' in entry['text'] for entry in xcp.log)


async def test_xcp_page_renders_without_warnings(manager, channel, xcp_slave, qapp):
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

    connection = open_connection(manager, channel)
    await settle(lambda: connection.connected)
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    root = engine.rootObjects()[0]
    stack = [sip.cast(root, QQuickWindow).contentItem()]
    view = None
    while stack:
        item = stack.pop()
        if item.metaObject().className().startswith('ConnectionView'):
            view = item
            break
        stack.extend(item.childItems())
    assert view is not None
    view.setProperty('page', 'xcp')
    await settle()
    xcp = connection.xcp
    xcp.connect_slave({'protocol': 'XCP'})
    await settle(lambda: xcp.connected)
    assert xcp.load_a2l(QUrl.fromLocalFile(str(a2l_path()))) == ''
    await xcp._poll_once()
    await settle(lambda: any(v['value'] for v in xcp.variables))
    xcp.disconnect_slave()
    await settle(lambda: not xcp.connected)
    view.setProperty('page', 'dashboard')
    await settle()
    manager.close_all()
    await settle()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert warnings == []
