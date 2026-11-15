import asyncio
import uuid

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent

from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.gui.help import HelpController
from can_explorer.main_application import create_qml_engine


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def channel():
    return f'periodic-gui-{uuid.uuid4().hex}'


@pytest.fixture
def manager(qapp):
    manager = ConnectionManager()
    yield manager
    manager.close_all()


def open_connection(manager, channel):
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    return connection


async def start_ecu(channel, periodic_id=None):
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    server = UdsServer(dids={0xF190: b'WDB1234567890ABCD', 0x0100: b'\x0b\xb8\x5a', 0xF203: b'\x42\x43'})
    simulator = UdsEcuSimulator(session, server=server, periodic_id=periodic_id)
    simulator.start()
    return session, server, simulator


async def run(diagnostics, start):
    assert start() == ''
    await wait_until(lambda: not diagnostics.busy)
    return diagnostics.history[-1]


async def test_periodic_data_on_its_own_can_id(manager, channel):
    session, server, simulator = await start_ecu(channel, periodic_id=0x5E8)
    connection = open_connection(manager, channel)
    diagnostics = connection.diagnostics
    try:
        assert diagnostics.listening_available and not diagnostics.listening
        await run(diagnostics, lambda: diagnostics.session_control(3))
        entry = await run(diagnostics, lambda: diagnostics.define_dynamic('F201', '0100:1:2 F190:1:1'))
        assert entry['description'] == 'DID 0xF201 defined from 2 DID source(s)'
        assert server.dynamic_dids[0xF201] == [('did', 0x0100, 1, 2), ('did', 0xF190, 1, 1)]
        assert diagnostics.define_dynamic('F201', '0100:x') != ''
        entry = await run(diagnostics, lambda: diagnostics.start_periodic('F201', 3, '5E8'))
        assert entry['description'] == 'Periodic data sendAtFastRate: 0xF201, on CAN id 0x5E8'
        assert diagnostics.periodic_running and not diagnostics.listening  # UUDT frames need no listening
        await wait_until(lambda: diagnostics.periodic_rows and diagnostics.periodic_rows[0]['count'] >= 3)
        row = diagnostics.periodic_rows[0]
        # The UUDT frame's padding is part of the data: a CAN frame carries no length
        assert row['did'] == 'F201' and row['data'].startswith('0B B8 57') and row['source'] == 'own CAN id'
        assert float(row['rate'].split()[0]) > 0
        entry = await run(diagnostics, diagnostics.stop_periodic)
        assert entry['description'] == 'Periodic data stopped' and not diagnostics.periodic_running
        assert server.periodic == {} and diagnostics.periodic_rows  # The last values stay
        assert (await run(diagnostics, lambda: diagnostics.clear_dynamic('')))['positive']
        assert server.dynamic_dids == {}
        assert (
            diagnostics.start_periodic('1234', 3, '') == 'DID 0x1234 is not a periodic data identifier (0xF200-0xF2FF)'
        )
    finally:
        simulator.stop()
        session.stop()


async def test_periodic_data_and_events_on_the_diagnostic_channel(manager, channel, qapp):
    session, server, simulator = await start_ecu(channel)
    connection = open_connection(manager, channel)
    diagnostics = connection.diagnostics
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

    try:
        await settle(lambda: connection.connected)
        from PyQt6 import sip
        from PyQt6.QtQuick import QQuickWindow

        stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
        view = None
        while stack:
            item = stack.pop()
            if item.metaObject().className().startswith('ConnectionView'):
                view = item
                break
            stack.extend(item.childItems())
        view.setProperty('page', 'diagnostics')
        await settle()

        await run(diagnostics, lambda: diagnostics.session_control(3))
        # Empty CAN id: 0x6A messages on the response id, so listening is turned on
        entry = await run(diagnostics, lambda: diagnostics.start_periodic('03', 2, ''))
        assert entry['positive'] and diagnostics.listening
        await settle(lambda: diagnostics.periodic_rows and diagnostics.periodic_rows[0]['count'] >= 2)
        row = diagnostics.periodic_rows[0]
        assert (row['did'], row['data'], row['source']) == ('F203', '42 43', 'diagnostic channel')
        # Periodic messages do not flood the History, and requests still get their own answers meanwhile
        assert not any(entry['unsolicited'] for entry in diagnostics.history)
        assert (await run(diagnostics, lambda: diagnostics.read_did('F190')))[
            'description'
        ] == 'VIN = WDB1234567890ABCD'
        await run(diagnostics, diagnostics.stop_periodic)

        # An event-triggered response arriving between requests shows up in the History
        await run(diagnostics, lambda: diagnostics.raw_request('86 03 02 01 00 22 01 00'))
        await run(diagnostics, lambda: diagnostics.raw_request('86 05 02'))
        server.dids[0x0100] = b'\x00\x01'
        server.did_changed(0x0100)
        await settle(lambda: diagnostics.history[-1]['unsolicited'])
        event = diagnostics.history[-1]
        assert event['request_description'] == 'Unsolicited (sent by the ECU)' and event['response'] == '62 01 00 00 01'
        assert event['positive'] and event['request'] == ''

        assert diagnostics.set_listening(False) == '' and not diagnostics.listening
        assert (await run(diagnostics, lambda: diagnostics.read_did('F190')))['positive']  # Back to own transports
        view.setProperty('page', 'dashboard')
        await settle()
    finally:
        simulator.stop()
        session.stop()
        manager.close_all()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert warnings == []


async def test_unsolicited_answers_during_requests_reach_the_history(manager, channel):
    session, server, simulator = await start_ecu(channel)
    connection = open_connection(manager, channel)
    diagnostics = connection.diagnostics
    handle = server.handle

    def event_first(request, functional=False):
        responses = handle(request, functional)
        if request == bytes([0x22, 0xF1, 0x90]):
            # An event-triggered response for another DID arrives just before the answer
            return [bytes([0x62, 0x01, 0x00, 0x0B, 0xB8])] + responses
        return responses

    server.handle = event_first
    try:
        entry = await run(diagnostics, lambda: diagnostics.read_did('F190'))
        # The answer is matched by its DID, the other response is listed as unsolicited (listening is off)
        assert entry['positive'] and entry['description'] == 'VIN = WDB1234567890ABCD'
        unsolicited = [e for e in diagnostics.history if e['unsolicited']]
        assert [e['response'] for e in unsolicited] == ['62 01 00 0B B8']
        assert diagnostics.set_listening(True) == '' and diagnostics.set_listening(True) == ''
        # Flashing and scans get a transport of the shared receiver while listening
        from can_explorer.core.uds import UdsClient
        from can_explorer.core.uds_shared import SharedTransport

        transport = diagnostics.create_transport()
        assert isinstance(transport, SharedTransport)
        assert diagnostics.set_listening(False) == 'The connection is in use (flashing or a scan)'
        assert await UdsClient(transport).read_data_by_identifier(0xF190) == b'WDB1234567890ABCD'
        transport.close()
        assert diagnostics.set_listening(False) == '' and not diagnostics.listening
    finally:
        simulator.stop()
        session.stop()
