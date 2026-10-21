import asyncio

import pytest
from PyQt6.QtCore import QUrl

from can_explorer.core.doip import DoipEntitySimulator
from can_explorer.core.flashing import FlashImage, Segment, intel_hex_text
from can_explorer.core.simulation import UdsServer
from can_explorer.gui.doip import SIMULATED, DoipController


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
async def doip(qapp):
    controller = DoipController()
    yield controller
    controller.shutdown()
    await asyncio.sleep(0.05)


async def connected(controller, options):
    assert controller.connect_entity(options) == ''
    await wait_until(lambda: not controller.busy)
    return controller.is_open


async def test_diagnostics_over_the_simulated_entity(doip):
    assert doip.entities[-1]['host'] == SIMULATED and doip.entities[-1]['simulated']
    diagnostics = doip.diagnostics
    assert diagnostics.read_did('F190') == 'Connect first'
    assert (diagnostics.addressing, diagnostics.transport_name, diagnostics.target) == ('doip', 'DoIP', '1001')

    assert await connected(doip, {'host': SIMULATED, 'tester_address': '0E80'})
    assert doip.simulated and doip.connection_text == 'simulated entity · entity 0x1000 · tester 0x0E80'
    assert doip.log[-1]['text'] == 'Connected to the simulated entity: routing activated by entity 0x1000'
    assert doip.connect_entity({'host': SIMULATED}) == 'Disconnect first'

    assert diagnostics.read_did('F190') == ''
    await wait_until(lambda: not diagnostics.busy)
    assert diagnostics.history[-1]['description'] == 'VIN = WDB1234567890ABCD'
    assert diagnostics.read_dtcs('FF') == ''
    await wait_until(lambda: not diagnostics.busy)
    assert [dtc['code'] for dtc in diagnostics.dtcs] == ['P0123-45', 'U0100-00']

    assert diagnostics.set_target('2000') == '' and diagnostics.addressing_text == 'target address 0x2000'
    assert diagnostics.read_did('F190') == ''
    await wait_until(lambda: not diagnostics.busy)
    assert diagnostics.history[-1]['error']
    assert 'unknown target address' in diagnostics.history[-1]['description']
    assert diagnostics.set_target('1FFFF') == 'Target address out of range: 0x1FFFF'
    assert diagnostics.set_target('1001') == ''

    diagnostics.set_tester_present(True)
    assert diagnostics.tester_present
    count = len(doip.log)
    assert doip.query_status() == ''
    await wait_until(lambda: len(doip.log) >= count + 2)
    assert [entry['text'] for entry in doip.log[-2:]] == [
        'Entity status: gateway, 1 of 2 TCP sockets open, diagnostic messages up to 65535 bytes',
        'Diagnostic power mode: ready',
    ]

    doip.disconnect_entity()
    await wait_until(lambda: not doip.is_open)
    assert not diagnostics.tester_present and doip.connection_text == ''
    assert diagnostics.read_did('F190') == 'Connect first'


async def test_discovery_connection_errors_and_lost_connections(doip):
    simulator = DoipEntitySimulator(targets={0x1001: UdsServer()})
    await simulator.start()
    try:
        assert doip.discover(f'{simulator.host}') == ''
        assert doip.discover('') == 'Looking for DoIP entities already'
        await wait_until(lambda: not doip.discovering, timeout=5.0)
        # Discovery asks port 13400, where the simulator does not listen: only the simulated entity is listed
        assert doip.entities[-1]['simulated']

        assert doip.connect_entity({'host': ''}).startswith('Enter the address')
        assert (
            doip.connect_entity({'host': '127.0.0.1', 'tester_address': 'xyz'}) == "Invalid hex tester address: 'xyz'"
        )
        assert not await connected(doip, {'host': simulator.host, 'port': simulator.port, 'tester_address': '0001'})
        assert doip.log[-1] == {'text': 'Routing activation refused: unknown source address', 'error': True}

        assert await connected(doip, {'host': simulator.host, 'port': simulator.port})
        assert doip.connection_text == f'127.0.0.1:{simulator.port} · entity 0x1000 · tester 0x0E00'
        await simulator.stop()
        await wait_until(lambda: not doip.is_open)
        assert doip.log[-1] == {'text': 'The DoIP entity closed the connection', 'error': True}
    finally:
        await simulator.stop()


async def test_flashing_over_doip(doip, tmp_path):
    firmware = bytes(range(256)) * 8
    path = tmp_path / 'app.hex'
    path.write_text(intel_hex_text(FlashImage([Segment(0x8000, firmware)])))
    flashing = doip.flashing
    assert flashing.load_file(QUrl.fromLocalFile(str(path)), '') == ''
    assert flashing.start({}) == 'Connect first'
    assert await connected(doip, {'host': SIMULATED})
    assert flashing.start({'erase': False}) == ''
    await wait_until(lambda: not flashing.running, timeout=10.0)
    assert flashing.state == 'done', flashing.error
    assert doip._simulator.targets[0x1001].memory == {0x8000: firmware}


async def test_doip_tool_renders_without_warnings(qapp, qtbot):
    from PyQt6.QtCore import QCoreApplication, QEvent

    from can_explorer.gui.application_metadata import ApplicationMetadata
    from can_explorer.gui.connection_manager import ConnectionManager
    from can_explorer.main_application import create_qml_engine

    async def settle(predicate=lambda: True):
        await wait_until(lambda: (qapp.processEvents(), predicate())[1])
        for _ in range(5):
            await asyncio.sleep(0.01)
            qapp.processEvents()

    warnings = []
    manager = ConnectionManager(parent=qapp)
    engine = create_qml_engine(
        {'metadata': ApplicationMetadata(parent=qapp), 'connectionManager': manager},
        lambda errors: warnings.extend(error.toString() for error in errors),
    )
    root = engine.rootObjects()[0]
    root.setProperty('tool', 'doip')
    await settle()
    doip = manager.doip
    assert doip.connect_entity({'host': SIMULATED}) == ''
    await settle(lambda: doip.is_open)
    assert doip.diagnostics.read_did('F190') == ''
    await settle(lambda: not doip.diagnostics.busy)
    doip.disconnect_entity()
    await settle(lambda: not doip.is_open)
    root.setProperty('tool', '')
    await settle()
    manager.close_all()
    await settle()
    root.deleteLater()
    engine.deleteLater()
    manager.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert warnings == []
