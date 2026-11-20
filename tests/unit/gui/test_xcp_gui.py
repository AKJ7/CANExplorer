import asyncio
import uuid
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QUrl

from can_explorer.core.ccp import CcpSlaveSimulator
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.xcp import XcpSlaveSimulator
from can_explorer.core.xcp_slave import example_memory
from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.gui.help import HelpController
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
    slave = XcpSlaveSimulator(session, request_id=0x7E0, response_id=0x7E1, memory=example_memory())
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
        {
            'metadata': ApplicationMetadata(parent=qapp),
            'connectionManager': manager,
            'helpController': HelpController(parent=qapp),
        },
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
    assert xcp.open_editor('FuelMap') == ''
    await settle(lambda: xcp.editor.get('name') == 'FuelMap')
    xcp.close_editor()
    await settle()
    xcp.set_acquisition('EngineSpeed', 1)
    xcp.set_plotted('EngineSpeed', True)
    await settle()
    xcp.disconnect_slave()
    await settle(lambda: not xcp.connected)
    view.setProperty('page', 'dashboard')
    await settle()
    manager.close_all()
    await settle()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert warnings == []


async def connect_with_a2l(manager, channel):
    connection = open_connection(manager, channel)
    xcp = connection.xcp
    assert xcp.load_a2l(QUrl.fromLocalFile(str(a2l_path()))) == ''
    settings = xcp.a2l_settings()
    assert settings == {'protocol': 'XCP', 'transport': 'CAN', 'extended': False, 'request_id': '7E0',
                        'response_id': '7E1'}  # fmt: skip
    assert xcp.connect_slave(settings) == ''
    await wait_until(lambda: xcp.connected and not xcp.busy)
    return connection, xcp


async def test_example_values_daq_and_plot(manager, channel, xcp_slave):
    connection, xcp = await connect_with_a2l(manager, channel)
    assert xcp.info['EPK'] == 'matches the A2L' and xcp.info['ECU id'] == 'example'
    assert [e['name'] for e in xcp.events] == ['10ms', '100ms']
    await xcp._poll_once()
    rows = {row['name']: row for row in xcp.variables}
    assert rows['WheelSpeeds']['value'] == '50, 50.1, 49.9, 50.05 km/h' and rows['WheelSpeeds']['structured']
    assert rows['FanMode']['value'] == 'Auto' and rows['BrakePressed']['value'] == '1'
    assert rows['FuelMap']['structured'] and not rows['VariantCode']['structured']

    xcp.set_acquisition('EngineSpeed', 1)  # The 10ms event
    xcp.set_plotted('EngineSpeed', True)
    assert 'XCP.EngineSpeed' in connection.tracked_signals
    assert xcp.start_daq() == ''
    await wait_until(lambda: xcp.daq_running and not xcp.busy)
    xcp_slave.memory[0x1000:0x1002] = (8000).to_bytes(2, 'little')
    series = connection.session.signals.series['XCP.EngineSpeed']
    await wait_until(lambda: series.last is not None and series.last[1] == 2000.0)
    assert xcp_slave.slave.daq_lists and xcp_slave.slave.daq_lists[0].running
    xcp.stop_daq()
    await wait_until(lambda: not xcp.daq_running and not xcp.busy)
    xcp.set_plotted('EngineSpeed', False)
    assert 'XCP.EngineSpeed' not in connection.tracked_signals
    xcp.disconnect_slave()
    await wait_until(lambda: not xcp.connected)


async def test_editor_writes_maps(manager, channel, xcp_slave):
    connection, xcp = await connect_with_a2l(manager, channel)
    assert xcp.open_editor('FuelMap') == ''
    await wait_until(lambda: xcp.editor.get('name') == 'FuelMap')
    editor = xcp.editor
    assert editor['x'] == ['1000', '2000', '4000', '6000'] and editor['y'] == ['20', '50', '80']
    assert editor['x_editable'] and not editor['y_editable']  # The load axis is a common axis
    rows = [list(row) for row in editor['rows']]
    rows[0][0] = '12.5'
    assert xcp.write_editor(rows, editor['x'], editor['y']) == ''
    await wait_until(lambda: xcp_slave.memory[0x220A] == 25)
    await wait_until(lambda: xcp.editor['rows'][0][0] == '12.5')  # Read back into the editor
    assert xcp.write_variable('IgnitionCurve', '1, 2, 3, 4, 5') == ''
    await wait_until(lambda: xcp_slave.memory[0x210C:0x210E] == (10).to_bytes(2, 'little'))
    assert xcp.write_variable('FanMode', 'On') == ''
    await wait_until(lambda: xcp_slave.memory[0x2008] == 2)
    assert xcp.write_variable('IdleTarget', '99999') == ''
    await wait_until(lambda: any('above its maximum' in entry['text'] for entry in xcp.log))


async def test_seed_and_key_from_a_file(manager, channel, tmp_path):
    from can_explorer.core.xcp import RESOURCE_CALPAG

    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    slave = XcpSlaveSimulator(session, memory=example_memory(), protected=RESOURCE_CALPAG)
    try:
        key = tmp_path / 'key.py'
        key.write_text('def compute_key(level, seed):\n    return bytes(b ^ 0xFF for b in seed)\n')
        connection, xcp = await connect_with_a2l(manager, channel)
        assert xcp.info['locked'] == 'CAL/PAG'
        assert xcp.write_variable('IdleTarget', '900') == ''
        await wait_until(lambda: not xcp.busy)
        assert slave.memory[0x2000:0x2002] == (3200).to_bytes(2, 'little')  # Still locked
        xcp.set_key_source(str(key))
        assert xcp.write_variable('IdleTarget', '900') == ''  # Unlocks, then writes
        await wait_until(lambda: slave.memory[0x2000:0x2002] == (3600).to_bytes(2, 'little'))
    finally:
        slave.stop()
        session.stop()


async def test_pages_checksum_and_datasets(manager, channel, xcp_slave, tmp_path):
    connection, xcp = await connect_with_a2l(manager, channel)
    assert xcp.checksum('2000', '256') == ''
    await wait_until(lambda: any('matches the uploaded data' in entry['text'] for entry in xcp.log))

    path = tmp_path / 'values.dcm'
    assert xcp.save_dataset(QUrl.fromLocalFile(str(path))) == ''
    await wait_until(lambda: path.exists() and not xcp.busy)
    assert 'KENNFELD FuelMap 4 3' in path.read_text(encoding='latin-1')
    assert xcp.load_dataset(QUrl.fromLocalFile(str(path))) == ''
    await wait_until(lambda: not xcp.busy and any('values differ' in e['text'] for e in xcp.log))
    assert xcp.differences == []
    path.write_text(path.read_text(encoding='latin-1').replace('WERT 800', 'WERT 850'), encoding='latin-1')
    assert xcp.load_dataset(QUrl.fromLocalFile(str(path))) == ''
    await wait_until(lambda: xcp.differences and not xcp.busy)
    assert xcp.differences == [{'name': 'IdleTarget', 'file': '850', 'ecu': '800'}]
    assert xcp.write_dataset() == ''
    await wait_until(lambda: xcp_slave.memory[0x2000:0x2002] == (3400).to_bytes(2, 'little'))
    await wait_until(lambda: not xcp.busy and xcp.differences == [])

    assert xcp.set_page('1') == ''  # The reference page still holds the original values
    await wait_until(lambda: xcp.info.get('page') == '1')
    assert xcp.copy_page(1, 0) == ''
    await wait_until(lambda: not xcp.busy)
    assert xcp.set_page('0') == ''
    await wait_until(lambda: xcp.info.get('page') == '0' and not xcp.busy)
    assert xcp_slave.memory[0x2000:0x2002] == (3200).to_bytes(2, 'little')


async def test_xcp_on_udp(manager):
    from can_explorer.core.xcp_ethernet import XcpEthernetSlave

    server = XcpEthernetSlave(memory=example_memory())
    port = await server.start()
    manager.add_connection({'name': 'ECU', 'interface': 'VirtualBus', 'channel': f'udp-{uuid.uuid4().hex}'})
    xcp = manager.connections[-1].xcp  # Ethernet does not need the CAN connection to be open
    try:
        assert xcp.connect_slave({'protocol': 'XCP', 'transport': 'UDP', 'host': '127.0.0.1', 'port': port}) == ''
        await wait_until(lambda: xcp.connected and not xcp.busy)
        assert xcp.info['transport'] == 'UDP' and xcp.info['MAX_CTO / DTO'] == '255 / 255'
        assert xcp.read_address('1000', 2) == ''
        await wait_until(lambda: any('70 17' in entry['text'] for entry in xcp.log))
        assert xcp.connect_slave({'protocol': 'CCP', 'transport': 'UDP'}) == 'Disconnect first'
        xcp.disconnect_slave()
        await wait_until(lambda: not xcp.connected and not xcp.busy)
    finally:
        await server.stop()
    assert xcp.connect_slave({'protocol': 'CCP', 'transport': 'TCP'}) == 'CCP runs on CAN only'


async def test_details_summaries_and_editor_labels(manager, channel, xcp_slave):
    connection, xcp = await connect_with_a2l(manager, channel)
    details = {row['label']: row for row in xcp.details}
    assert details['EPK']['level'] == 'ok' and details['locked'] == {'label': 'locked', 'value': 'none', 'level': 'ok'}
    # Curves, maps and axes are read once on connect and summarised in the table
    rows = {row['name']: row for row in xcp.variables}
    assert rows['IgnitionCurve']['value'] == '5 points · 5 … 25 deg'
    assert rows['FuelMap']['value'] == '4 × 3 · 10 … 75 %'
    assert rows['LoadAxis']['value'] == '20, 50, 80 %' and rows['VariantCode']['value'] == 'EU5-1234'
    await xcp._poll_once()  # Polling waits for any running exchange instead of interleaving with it
    assert {row['name']: row for row in xcp.variables}['EngineSpeed']['value'] == '1500 rpm'
    assert xcp.open_editor('FuelMap') == ''
    await wait_until(lambda: xcp.editor.get('name') == 'FuelMap')
    editor = xcp.editor
    assert editor['limits'] == '0 … 100' and editor['description'] == 'Injection over engine speed and load'
    assert editor['x_name'] == 'EngineSpeed [rpm]' and editor['y_name'] == 'EngineLoad [%]'
