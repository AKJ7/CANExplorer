import asyncio
import uuid

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent

from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.main_application import create_qml_engine


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def channel():
    return f'udsscan-gui-{uuid.uuid4().hex}'


def open_connection(manager, channel):
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    return connection


@pytest.fixture
async def ecu(channel):
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    server = UdsServer(dids={0xF190: b'WDB123', 0xF18C: b'SN-0001'}, slow_routines=())
    simulator = UdsEcuSimulator(session, server=server)
    simulator.start()
    yield server
    simulator.stop()
    session.stop()


@pytest.fixture
def manager(qapp):
    manager = ConnectionManager()
    yield manager
    manager.close_all()


async def run_scan(scan, options):
    assert scan.start(options) == ''
    await wait_until(lambda: not scan.running)
    return {row['identifier']: row for row in scan.results}


async def test_scan_requires_a_connection(manager, channel, ecu):
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    assert scan.transport_name == 'ISO-TP'
    assert not scan.running and scan.state == ''


async def test_data_identifier_scan(manager, channel, ecu):
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    rows = await run_scan(scan, {'kind': 'dids', 'start': 'F188', 'end': 'F192', 'timeout_ms': 150})
    assert scan.state == 'done' and scan.scanned == 0xF192 - 0xF188 + 1
    labels = ' '.join(rows)
    assert '0xF190' in labels and '0xF18C' in labels
    positive = [row for row in rows.values() if row['category'] == 'positive']
    assert any(row['response'].startswith('62 F1 90') for row in positive)


async def test_service_scan_and_inspect_bridges_to_diagnostics(manager, channel, ecu):
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    rows = await run_scan(scan, {'kind': 'services', 'known_only': True, 'timeout_ms': 150})
    assert any('0x22' in label for label in rows) and any('0x27' in label for label in rows)
    # Inspect re-sends a found request through the Diagnostics view
    did_request = next(row['request'] for label, row in rows.items() if '0x22' in label)
    assert scan.inspect(did_request) == ''
    await wait_until(lambda: not connection.diagnostics.busy)
    assert connection.diagnostics.history


async def test_stop_and_clear(manager, channel, ecu):
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    assert scan.start({'kind': 'dids', 'start': '0000', 'end': 'FFFF', 'timeout_ms': 50}) == ''
    await wait_until(lambda: scan.scanned > 0)
    scan.stop()
    await wait_until(lambda: not scan.running)
    assert scan.state == 'stopped' and scan.scanned < 0x10000
    scan.clear()
    assert scan.found == 0 and scan.results == [] and scan.state == ''


async def test_sweep_needs_opt_in_and_validates(manager, channel, ecu):
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    assert scan.start({'kind': 'sweep', 'template': '10 00', 'position': 1}) == (
        'This scan can change the ECU state; enable "Allow state-changing probes" to run it'
    )
    assert 'outside the template' in scan.start(
        {'kind': 'sweep', 'template': '10', 'position': 1, 'allow_state_changing': True}
    )
    rows = await run_scan(
        scan,
        {
            'kind': 'sweep',
            'template': '10 00',
            'position': 1,
            'start': '01',
            'end': '03',
            'allow_state_changing': True,
            'timeout_ms': 150,
        },
    )
    assert len(rows) == 3  # Sessions 1–3 all answer positively


async def test_scan_errors_before_connecting(manager, channel):
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    scan = manager.connections[-1].uds_scan
    assert scan.start({'kind': 'dids'}) == 'Connect first'


async def test_uds_scan_page_renders_without_warnings(manager, channel, ecu, qapp):
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
    view.setProperty('page', 'uds-scan')
    await settle()
    scan = connection.uds_scan
    assert scan.start({'kind': 'dids', 'start': 'F188', 'end': 'F192', 'timeout_ms': 150}) == ''
    await settle(lambda: not scan.running)
    assert scan.found > 0
    view.setProperty('page', 'dashboard')
    await settle()
    manager.close_all()
    await settle()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert warnings == []


async def test_fuzz_mode_runs_and_opt_in(manager, channel, ecu):
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    assert any(k['value'] == 'fuzz' for k in scan.kinds)
    # Fuzzing is state-changing, so it needs the opt-in
    assert scan.start({'kind': 'fuzz', 'count': 40}) == (
        'This scan can change the ECU state; enable "Allow state-changing probes" to run it'
    )
    assert (
        scan.start({'kind': 'fuzz', 'count': 40, 'fuzz_seed': '1', 'timeout_ms': 150, 'allow_state_changing': True})
        == ''
    )
    await wait_until(lambda: not scan.running)
    assert scan.state == 'done' and scan.scanned >= 40
    # The hardened simulated ECU never hangs, so no unresponsive anomaly is reported
    assert not any(row['category'] == 'unresponsive' for row in scan.results)


async def test_fuzz_mode_validates_seed(manager, channel, ecu):
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    assert 'reproducibility seed must be a number' in scan.start(
        {'kind': 'fuzz', 'fuzz_seed': 'abc', 'allow_state_changing': True}
    )


async def test_scan_stops_promptly_interrupting_a_long_request(manager, channel):
    """Stopping a scan interrupts the in-flight request instead of waiting out its timeout, so a scan against an
    unresponsive bus (or an ECU looping on 'response pending') can always be stopped at once."""
    connection = open_connection(manager, channel)
    scan = connection.uds_scan  # No ECU on the bus: the first request would block for the full 5 s timeout
    assert scan.start({'kind': 'services', 'timeout_ms': 5000, 'delay_ms': 0}) == ''
    await wait_until(lambda: scan.running, timeout=2)
    await asyncio.sleep(0.2)

    loop = asyncio.get_running_loop()
    started = loop.time()
    scan.stop()
    await wait_until(lambda: not scan.running, timeout=3)
    assert loop.time() - started < 2.0  # Interrupted, not waiting the 5 s timeout
    assert scan.state == 'stopped'


async def test_stop_notifies_that_running_became_false(manager, channel):
    """After stopping, the controller emits `changed` while `running` is False, so the Stop button returns to
    Start (the running state is not left stale)."""
    connection = open_connection(manager, channel)
    scan = connection.uds_scan
    running_at_emit = []
    scan.changed.connect(lambda: running_at_emit.append(scan.running))
    assert scan.start({'kind': 'services', 'timeout_ms': 5000, 'delay_ms': 0}) == ''
    await wait_until(lambda: scan.running, timeout=2)
    await asyncio.sleep(0.2)
    scan.stop()
    await wait_until(lambda: not scan.running, timeout=3)
    await asyncio.sleep(0.05)  # Let the task's done-callback run
    assert running_at_emit and running_at_emit[-1] is False
