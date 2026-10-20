"""Regenerate the screenshots of the documentation (docs/assets/screenshots).

Usage: QT_QPA_PLATFORM=offscreen uv run python tools/docs_screenshots.py

Drives the application with simulated buses, ECUs and LIN slaves; no hardware is needed.
"""

import asyncio
import math
import os
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import can
import qasync
from PyQt6 import sip
from PyQt6.QtCore import Q_ARG, QMetaObject, QObject, QSettings, QUrl
from PyQt6.QtQuick import QQuickWindow

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'docs' / 'assets' / 'screenshots'
DATA = ROOT / 'tests' / 'data'
EXAMPLES = ROOT / 'examples' / 'simulation'

# Render with OpenGL like on a desktop (the default offscreen renderer cannot draw the blur of the glass dialogs)
os.environ.setdefault('QT_QUICK_BACKEND', 'rhi')
os.environ.setdefault('QSG_RHI_BACKEND', 'opengl')

# Use throwaway settings, so the screenshots do not depend on (or change) the user's
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, tempfile.mkdtemp())

from can_explorer.core.bushealth import ERR_BUSOFF, ERR_CNT, ERR_CRTL, ERR_PROT, ERR_RESTARTED  # noqa: E402
from can_explorer.core import updates  # noqa: E402
from can_explorer.core.canopen_tools import CanOpenNodeSimulator, ObjectDictionary  # noqa: E402
from can_explorer.core.j1939_tools import (  # noqa: E402
    PGN_ADDRESS_CLAIMED,
    PGN_DM1,
    PGN_DM2,
    Dtc,
    J1939Name,
    J1939Sender,
    encode_dm,
)
from can_explorer.core.reverse.markers import Marker  # noqa: E402
from can_explorer.core.session import BusSession  # noqa: E402
from can_explorer.core.simulation import LinSlaveSimulator, UdsEcuSimulator, UdsServer  # noqa: E402
from can_explorer.core.sources import BusConfig, LiveSource  # noqa: E402
from can_explorer.main_application import MainApplication  # noqa: E402

app = MainApplication()
loop = qasync.QEventLoop(app)
asyncio.set_event_loop(loop)
window = sip.cast(app._qml_engine.rootObjects()[0], QQuickWindow)
window.resize(1440, 900)
manager = app.connection_manager
warnings = []
app._qml_engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))


def items(type_name: str):
    found = window.findChildren(QObject)
    stack = [window.contentItem()]
    while stack:
        item = stack.pop()
        found.append(item)
        stack.extend(item.childItems())
    return [item for item in found if type_name in item.metaObject().className()]


def show_page(page: str):
    """Select a view of the current connection, like clicking it in the sidebar."""
    view = [view for view in items('ConnectionView') if view.isVisible()][0]
    view.setProperty('page', page)


async def shot(name: str, delay: float = 0.4):
    await asyncio.sleep(delay)
    window.grabWindow().save(str(OUT / f'{name}.png'))
    print('saved', name)


def error_frame(timestamp: float, classes: int, data) -> can.Message:
    return can.Message(timestamp=timestamp, arbitration_id=classes, is_error_frame=True, data=bytes(data))


async def wait_idle(diagnostics):
    while diagnostics.busy:
        await asyncio.sleep(0.02)


async def mqtt_scenario(connection, view):
    """The MQTT dialog while publishing to a local broker (the one of the tests), on a tidy port if one is free
    (never to a broker running on this computer)."""
    sys.path.insert(0, str(ROOT / 'tests' / 'unit'))
    from mqtt_broker import MqttBroker

    for port in (1883, 1884, 1885, 0):
        broker = MqttBroker(port=port)
        try:
            broker.start()
            break
        except OSError:
            continue
    mqtt = connection.mqtt
    error = mqtt.start(
        {
            'broker': f'mqtt://localhost:{broker.port}',
            'username': 'dashboard',
            'prefix': 'canexplorer',
            'signals': True,
            'frames': False,
            'retain': True,
            'interval': 0.5,
            'allow_send': True,
            'allowlist': 'EngineControl 7DF',
        }
    )
    assert not error, error
    while mqtt.state != 'connected':
        await asyncio.sleep(0.05)
    await asyncio.sleep(2.2)  # Some values published, and the statistics updated
    dialog = [o for o in view.findChildren(QObject) if o.metaObject().className().startswith('MqttDialog')][0]
    dialog.open()
    await shot('mqtt')
    dialog.close()
    mqtt.stop()
    broker.stop()


async def ethercat_scenario():
    """The EtherCAT tool on the simulated network: an output terminal's objects, SDO transfers and an emergency."""
    ethercat = manager.ethercat

    async def done():
        await asyncio.sleep(0.05)
        while ethercat.busy:
            await asyncio.sleep(0.02)

    window.setProperty('tool', 'ethercat')
    ethercat.open_network('simulated')
    await done()
    ethercat.request_state(0x04, False)  # The input terminal to Safe-Op
    await done()
    ethercat.select(3)
    for operation in (
        ethercat.read_dictionary,
        lambda: ethercat.sdo_read('1018', '02'),
        lambda: ethercat.sdo_write('7000', '01', '-1200'),
        lambda: ethercat.sdo_write('8000', '02', '5000'),  # Out of range: the terminal sends an emergency
        lambda: ethercat.sdo_write('1008', '00', 'Renamed'),
        ethercat.read_pdo_configuration,
    ):
        operation()
        await done()
    await ethercat._poll_states(ethercat._master)
    await shot('ethercat')
    tabs = [tab for tab in items('TabBar') if hasattr(tab, 'isVisible') and tab.isVisible()][0]
    tabs.setProperty('currentIndex', 1)
    await shot('ethercat-pdos')
    tabs.setProperty('currentIndex', 0)
    ethercat.close_network()
    await done()
    window.setProperty('tool', '')


async def physical_io_scenario():
    """The Physical I/O tool on the simulated device: two outputs switched, inputs wired back to them."""
    io = manager.physical_io
    window.setProperty('tool', 'physical-io')
    io.open_device('simulated')
    while not io.is_open:
        await asyncio.sleep(0.02)
    for operation in (
        lambda: io.configure_line(0, True, 'none', False),
        lambda: io.configure_line(1, True, 'none', False),
        lambda: io.configure_line(4, False, 'none', False),
        lambda: io.configure_line(5, False, 'down', False),
        lambda: io.configure_line(6, False, 'up', True),
        lambda: io.set_output(0, True),
    ):
        operation()
        await asyncio.sleep(0.05)
    await asyncio.sleep(3)  # History for the sparklines
    await shot('physical-io')
    io.close_device()
    window.setProperty('tool', '')


async def doip_scenario():
    """The DoIP tool connected to the simulated entity: VIN, session, DTCs and the entity status."""
    doip = manager.doip
    diagnostics = doip.diagnostics
    window.setProperty('tool', 'doip')
    doip.connect_entity({'host': 'simulated'})
    while doip.busy or not doip.is_open:
        await asyncio.sleep(0.02)
    for operation in (
        lambda: diagnostics.read_did('F190'),
        lambda: diagnostics.session_control(0x03),
        lambda: diagnostics.read_did('F18C'),
        lambda: diagnostics.read_dtcs('FF'),
    ):
        operation()
        while diagnostics.busy:
            await asyncio.sleep(0.02)
    doip.query_status()
    await shot('doip', 0.6)
    doip.disconnect_entity()
    await asyncio.sleep(0.2)
    window.setProperty('tool', '')


async def reverse_engineering_scenario():
    """A minute of traffic of an unknown vehicle (synthetic, from tests/data/reverse.dbc): the door switch toggles
    after a notch and is marked each time, a GPS log gives the vehicle speed."""
    from can_explorer.core.decoding import open_signal_database
    from can_explorer.core.reverse.synthetic import Behaviour, generate

    def full(period):
        return Behaviour('sine', period=period, amplitude=0.95)

    behaviours = {
        'Powertrain.Checksum': Behaviour('checksum'),
        'Powertrain.Counter': Behaviour('counter'),
        'Powertrain.EngineSpeed': full(20),
        'Powertrain.Torque': full(23),
        'Powertrain.Throttle': Behaviour('ramp', period=15, amplitude=0.95),
        'Body.VehicleSpeed': full(27),
        'Body.SteeringAngle': full(31),
        'Body.DoorOpen': Behaviour('constant'),
        'Battery.Temperature': Behaviour('sine', period=40, amplitude=0.95),
    }
    for group in range(3):
        behaviours[f'Battery.CellA{group}'] = full(7 + 2 * group)
        behaviours[f'Battery.CellB{group}'] = full(9 + 2 * group)
    start = time.time() - 60
    traffic = generate(open_signal_database(DATA / 'reverse.dbc'), 60, behaviours, seed=1, start=start)
    door_toggles = [start + seconds for seconds in (32, 38, 44, 50, 56)]

    manager.add_connection(
        {'name': 'Test vehicle', 'interface': 'VirtualBus', 'channel': f'docs-{uuid.uuid4().hex[:6]}'}
    )
    connection = manager.connections[-1]
    connection.open()
    window.setProperty('selectedIndex', len(manager.connections) - 1)
    show_page('reverse')
    await asyncio.sleep(0.3)
    reverse = connection.reverse
    view = [view for view in items('ConnectionView') if view.isVisible()][0]
    tabs = view.findChild(QObject, 'reverseTabs')

    # The traffic arrives (the door switch, byte 4 bit 0 of 500, toggling at the marked moments)
    door = 0
    for msg in traffic.messages:
        if msg.timestamp >= start + 30 and not reverse.notched:
            reverse.notch()
        while door_toggles and msg.timestamp >= door_toggles[0]:
            door ^= 1
            door_toggles.pop(0)
            reverse.markers._markers.append(Marker(msg.timestamp + 0.3, 'door opened' if door else 'door closed'))
        if msg.arbitration_id == 0x500:
            msg.data[4] = (msg.data[4] & 0xFE) | door
        connection.session._dispatch(msg)
    connection._refresh()
    connection.session.bus_statistics.reset()  # The frames arrived all at once
    connection._sample_bus_statistics()
    # After the notch only the door switch stands out; the vehicle speed (Motorola, bytes 0-1) as bit field
    reverse.select('500')
    reverse.set_field({'byte_order': 'big_endian'})
    reverse.select_bits(7, 8)
    reverse.set_field({'scale': 0.01, 'unit': 'km/h', 'name': 'VehicleSpeed'})
    await shot('reverse-engineering', 0.6)

    reverse.analysis.run(False)
    while reverse.analysis.running:
        await asyncio.sleep(0.05)
    tabs.setProperty('currentIndex', 1)
    await shot('reverse-analysis')

    gps = Path(tempfile.mkdtemp()) / 'gps.csv'
    rows = [f'{stamp - start:.3f},{raw * 0.01:.1f}' for stamp, raw in traffic.raw['Body.VehicleSpeed'][::50]]
    gps.write_text('time_s,speed [km/h]\n' + '\n'.join(rows))
    reverse.reference.open_csv(QUrl.fromLocalFile(str(gps)))
    reverse.reference.find('csv:0', 1.0)
    while reverse.reference.running:
        await asyncio.sleep(0.05)
    tabs.setProperty('currentIndex', 2)
    await shot('reverse-reference')

    reverse.markers.find('', 0.5, 1.0)
    while reverse.markers.running:
        await asyncio.sleep(0.05)
    tabs.setProperty('currentIndex', 3)
    await shot('reverse-markers')

    tabs.setProperty('currentIndex', 0)
    manager.remove_connection(len(manager.connections) - 1)
    window.setProperty('selectedIndex', 0)
    await asyncio.sleep(0.2)


async def scenario():
    OUT.mkdir(parents=True, exist_ok=True)
    await shot('welcome', 0.5)

    dialog = items('NewConnectionDialog')[0]
    dialog.open()
    await shot('new-connection')
    dialog.close()

    # CAN connection with the example simulation as traffic source
    channel = f'docs-{uuid.uuid4().hex[:6]}'
    manager.add_connection({'name': 'Vehicle', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[0]
    connection.open()
    connection.load_database(QUrl.fromLocalFile(str(EXAMPLES / 'vehicle.dbc')))
    simulation = connection.simulation
    simulation.load_script(QUrl.fromLocalFile(str(EXAMPLES / 'engine_simulation.py')))
    simulation.add_panel(QUrl.fromLocalFile(str(EXAMPLES / 'engine_panel.qml')))
    simulation.start()
    simulation.panel.set_variable('ignition', True)
    simulation.panel.set_variable('target_speed', 2400)
    recording = Path(tempfile.mkdtemp()) / 'vehicle.blf'
    connection.start_recording(QUrl.fromLocalFile(str(recording)))
    connection.track_signal('EngineData', 'EngineSpeed', 'rpm')
    connection.track_signal('VehicleSpeed', 'WheelSpeed', 'km/h')
    connection.track_signal('EngineData', 'CoolantTemp', 'degC')
    await asyncio.sleep(1.5)
    simulation.panel.set_variable('target_speed', 3600)
    await asyncio.sleep(1.5)

    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session.start()
    ecu = UdsEcuSimulator(
        ecu_session,
        server=UdsServer(dids={0xF100: bytes([0x0F, 0xA0, 90, 2]), 0xF190: b'WDB1234567890ABCD', 0x123: bytes(4)}),
    )
    ecu.start()

    connection.stop_recording()
    show_page('dashboard')
    await shot('dashboard', 1.0)
    show_page('trace')
    await shot('trace')

    # Searching the trace and bookmarking the result, through the find bar's functions (QTest key presses would
    # process events inside this coroutine, which qasync does not allow)
    connection.set_paused(True)
    find_bar = [bar for bar in items('TraceFindBar') if bar.isVisible()][0]
    field = [child for child in find_bar.childItems() if 'TextField' in child.metaObject().className()][0]
    field.setProperty('text', 'EngineData.EngineSpeed > 3500')
    QMetaObject.invokeMethod(find_bar, 'focusSearch')
    QMetaObject.invokeMethod(find_bar, 'find', Q_ARG('QVariant', True))
    await asyncio.sleep(0.4)
    QMetaObject.invokeMethod(find_bar, 'toggleBookmarks')
    await shot('trace-search')
    view = [view for view in items('ConnectionView') if view.isVisible()][0]
    record_dialog = [o for o in view.findChildren(QObject) if o.metaObject().className().startswith('RecordDialog')][0]
    record_dialog.open()
    await shot('record-dialog')
    record_dialog.close()
    await mqtt_scenario(connection, view)
    connection.set_paused(False)
    show_page('overview')
    connection.overview.sort_by(3)
    connection.overview.sort_by(3)  # Count, descending
    await shot('overview')
    show_page('signals')
    # Engine and vehicle speed in one chart, a zoomed range and both cursors
    plot_view = connection.plot_view
    plot_view.move_to_chart('VehicleSpeed.WheelSpeed', 0)
    plot_view.fit_all()
    start, end = plot_view.time_range()
    plot_view.set_cursor(0, start + (end - start) * 0.3)
    plot_view.set_cursor(1, start + (end - start) * 0.75)
    await shot('signals', 1.0)
    plot_view.clear_cursors()
    plot_view.follow_live()
    show_page('simulation')
    await shot('simulation')

    diagnostics = connection.diagnostics
    connection.load_database(QUrl.fromLocalFile(str(DATA / 'example.cdd')))
    for start in (
        lambda: diagnostics.session_control(3),
        lambda: diagnostics.read_did('F100'),
        lambda: diagnostics.read_did('F190'),
        lambda: diagnostics.read_dtcs('FF'),
        lambda: diagnostics.read_did('1234'),
    ):
        start()
        await wait_idle(diagnostics)
    show_page('diagnostics')
    await shot('diagnostics')
    # Flashing the example firmware into the simulated ECU
    flashing = connection.flashing
    flashing.load_file(QUrl.fromLocalFile(str(ROOT / 'examples' / 'flashing' / 'firmware.hex')), '')
    flashing.start({})  # The options of the form
    while flashing.running:
        await asyncio.sleep(0.05)
    show_page('flash')
    await shot('flashing')
    # Test automation: the example tests against the simulation
    tests = connection.tests
    tests.load_script(QUrl.fromLocalFile(str(ROOT / 'examples' / 'testing' / 'vehicle_tests.py')))
    ecu.stop()  # The diagnostics test is skipped without an ECU
    tests.run_selected()
    while tests.running:
        await asyncio.sleep(0.1)
    tests.show_case('EngineTests.speed_follows_the_target')
    show_page('tests')
    await shot('tests')

    simulation.stop()
    ecu_session.stop()

    # Replaying the recording on the bus, and as a log file
    simulation.panel.set_variable('ignition', False)
    connection.clear()
    assert connection.bus_replay.start(QUrl.fromLocalFile(str(recording)), 1.0, True, '') == ''
    show_page('trace')
    await shot('bus-replay', 1.5)
    connection.bus_replay.dismiss()
    manager.open_log_file(QUrl.fromLocalFile(str(recording)), 'Raw', 1.0)
    await asyncio.sleep(1.2)
    manager.connections[-1].replay.set_paused(True)
    await shot('replay')
    manager.remove_connection(len(manager.connections) - 1)

    # LIN connection with simulated slaves
    lin_channel = f'docs-lin-{uuid.uuid4().hex[:6]}'
    manager.add_connection({'name': 'Body LIN', 'interface': 'VirtualBus', 'channel': lin_channel, 'bus': 'LIN'})
    lin = manager.connections[1]
    lin.open()
    lin.load_database(QUrl.fromLocalFile(str(DATA / 'example.ldf')))
    slaves = BusSession(LiveSource(BusConfig('virtual', lin_channel, bus_type='LIN')))
    slaves.start()
    counter = iter(range(10**6))
    simulator = LinSlaveSimulator(
        slaves, {0x10: lambda: bytes([next(counter) % 200, 0x2C, 0x01]), 0x20: bytes([0x42])}, server=UdsServer()
    )
    lin.set_master_signal('SeatHeating', 'High')
    lin.start_schedule('NormalTable')
    await asyncio.sleep(1.0)
    show_page('trace')
    await shot('lin-trace')
    show_page('lin')
    await shot('lin-schedule')
    # The vehicle bus and the LIN bus on one timeline
    simulation.start()
    await asyncio.sleep(1.0)
    manager.multi_bus.set_selected('Vehicle', True)
    manager.multi_bus.set_selected('Body LIN', True)
    window.setProperty('tool', 'multi-bus')
    await shot('multi-bus', 0.8)
    window.setProperty('tool', '')
    simulation.stop()
    lin.stop_schedule()
    simulator.close()
    slaves.stop()

    # J1939 network with an engine reporting trouble codes, a transmission, brakes and a node without a claim
    truck_channel = f'docs-j1939-{uuid.uuid4().hex[:6]}'
    manager.add_connection({'name': 'Truck', 'interface': 'VirtualBus', 'channel': truck_channel, 'protocol': 'J1939'})
    truck = manager.connections[-1]
    truck.open()
    ecus = BusSession(LiveSource(BusConfig('virtual', truck_channel)))
    ecus.start()
    ecu_sender = J1939Sender(ecus)
    names = {0x00: (0, 0, 123, 4567), 0x03: (3, 0, 123, 812), 0x0B: (11, 0, 68, 99001), 0x17: (23, 1, 274, 51)}
    for address, (function, instance, manufacturer, identity) in names.items():
        name = J1939Name((1 << 63) | (function << 40) | (instance << 35) | (manufacturer << 21) | identity)
        await ecu_sender.send(PGN_ADDRESS_CLAIMED, name.to_bytes(), source=address, priority=6)
    await ecu_sender.send(PGN_ADDRESS_CLAIMED, J1939Name((1 << 63) | (23 << 40) | (274 << 21) | 52).to_bytes(), 0xFE)
    for _ in range(5):
        await ecu_sender.send(0xF004, bytes([0xF0, 0x7D, 0x7D, 0x60, 0x3B, 0x00, 0xF0, 0x7D]), source=0x00, priority=3)
        await ecu_sender.send(0xF005, bytes([0x7D, 0x00, 0x00, 0x7D, 0x03, 0xFF, 0xFF, 0xFF]), source=0x03, priority=3)
        await ecu_sender.send(0xFEBF, bytes(8), source=0x0B)
        await ecu_sender.send(0xFEF1, bytes([0xFF, 0x00, 0x50, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]), source=0x21)
        await asyncio.sleep(0.05)
    dm1 = encode_dm([Dtc(110, 0, 3), Dtc(100, 1, 1)], {'MIL': 'on', 'AWL': 'on'})
    await ecu_sender.send(PGN_DM1, dm1, source=0x00)  # 10 bytes: BAM
    await ecu_sender.send(PGN_DM2, encode_dm([Dtc(190, 2, 4), Dtc(94, 18, 1), Dtc(3216, 16, 2)]), source=0x00)
    await ecu_sender.send(PGN_DM1, encode_dm([Dtc(521, 31, 1)], {'AWL': 'on'}), source=0x0B)
    truck.j1939.request_address_claims()
    show_page('j1939')
    await asyncio.sleep(0.7)
    [page for page in items('J1939View') if page.isVisible()][0].setProperty('selected', 0)
    await shot('j1939', 0.6)
    ecus.stop()

    # CANopen network: two simulated I/O modules
    machine_channel = f'docs-canopen-{uuid.uuid4().hex[:6]}'
    manager.add_connection(
        {'name': 'Machine', 'interface': 'VirtualBus', 'channel': machine_channel, 'protocol': 'CANopen'}
    )
    machine = manager.connections[-1]
    machine.open()
    slaves = BusSession(LiveSource(BusConfig('virtual', machine_channel)))
    slaves.start()
    eds = ROOT / 'examples' / 'canopen' / 'io_module.eds'
    modules = [CanOpenNodeSimulator(slaves, node_id, ObjectDictionary.from_file(eds), 200) for node_id in (5, 6, 12)]
    for module in modules:
        module.start()
    modules[0].set_value(0x6401, 1, 2471)
    modules[0].set_value(0x6000, 1, 0b10110001)
    canopen = machine.canopen
    canopen.load_dictionary(QUrl.fromLocalFile(str(eds)))
    canopen.set_node(5)
    await asyncio.sleep(0.3)
    canopen.nmt(0x01, True)
    canopen.set_node(12)
    canopen.nmt(0x02, False)
    canopen.set_node(5)
    modules[1].emergency(0x4210, 0x09, b'\x5a')
    modules[1].emergency(0x0000, 0x00)
    modules[1].emergency(0x8130, 0x11)
    for start in (
        lambda: canopen.sdo_read('1018', '04'),
        lambda: canopen.sdo_read('1008', '00'),
        lambda: canopen.sdo_write('1017', '00', '200'),
        lambda: canopen.sdo_write('1000', '00', '0'),
        lambda: canopen.sdo_read('2000', '00'),
    ):
        start()
        while canopen.busy:
            await asyncio.sleep(0.02)
    show_page('canopen')
    await asyncio.sleep(0.7)
    await shot('canopen')
    tabs = [tab for tab in items('TabBar') if hasattr(tab, 'isVisible') and tab.isVisible()][0]
    tabs.setProperty('currentIndex', 1)
    canopen.send_sync()
    await shot('canopen-pdos', 0.7)
    for module in modules:
        module.stop()
    slaves.stop()

    # Comparing the recording with a variant: lower engine speed after 2 s, vehicle speed every 2nd frame, one more ID
    variant = recording.with_name('vehicle_variant.asc')
    with can.LogReader(str(recording)) as reader, can.Logger(str(variant)) as writer:
        first = None
        count = 0
        for msg in reader:
            first = msg.timestamp if first is None else first
            if msg.arbitration_id == 0x100 and msg.timestamp - first > 2.0:
                raw = int.from_bytes(msg.data[:2], 'little') * 9 // 10
                msg.data[:2] = raw.to_bytes(2, 'little')
            if msg.is_extended_id:
                count += 1
                if count % 2:
                    continue
            writer.on_message_received(msg)
            if msg.arbitration_id == 0x100 and count % 50 == 0:
                writer.on_message_received(
                    can.Message(timestamp=msg.timestamp, arbitration_id=0x7DF, is_extended_id=False, data=[2, 1, 0x0D])
                )
    compare_tool = manager.compare
    compare_tool.open_file('a', QUrl.fromLocalFile(str(recording)))
    compare_tool.open_file('b', QUrl.fromLocalFile(str(variant)))
    compare_tool.load_database(QUrl.fromLocalFile(str(EXAMPLES / 'vehicle.dbc')))
    while compare_tool.busy or not compare_tool.has_result:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    while compare_tool.busy:
        await asyncio.sleep(0.05)
    window.setProperty('tool', 'compare')
    await shot('compare')
    compare_tabs = [tab for tab in items('TabBar') if hasattr(tab, 'isVisible') and tab.isVisible()][0]
    compare_tabs.setProperty('currentIndex', 1)
    await shot('compare-signals')
    window.setProperty('tool', '')

    await ethercat_scenario()
    await physical_io_scenario()
    await doip_scenario()

    await reverse_engineering_scenario()

    # Bus health: ten minutes of a bus with rising error counters, an error passive period and a bus off
    health = connection.health
    health.reset()
    now = time.time()
    for second in range(600):
        stamp = now - 600 + second
        load = 0.32 + 0.08 * math.sin(second / 40) + (0.15 if 300 <= second < 420 else 0)
        if 240 <= second < 420 and second % 6 == 0:
            tec = min(255, (second - 240) * 1)
            classes = ERR_PROT | ERR_CNT | (ERR_CRTL if tec >= 96 else 0)
            problem = 0x20 if tec >= 128 else 0x08 if tec >= 96 else 0
            health.on_message(error_frame(stamp, classes, [0, problem, 0x04 | 0x80, 0, 0, 0, tec, 3]))
        if second == 420:
            health.on_message(error_frame(stamp, ERR_BUSOFF | ERR_CNT, [0, 0, 0, 0, 0, 0, 255, 3]))
        if second == 440:
            health.on_message(error_frame(stamp, ERR_RESTARTED | ERR_CNT, [0, 0x40, 0, 0, 0, 0, 0, 0]))
        if 420 <= second < 440:
            load = 0.0
        health.sample(stamp, load * 2000, 40, load, None)
    connection.bus_health.set_window(600)
    window.setProperty('selectedIndex', 0)  # The Vehicle connection
    show_page('health')
    await shot('bus-health', 0.8)

    # The command palette
    palette = [
        item for item in window.findChildren(QObject) if item.metaObject().className().startswith('CommandPalette')
    ][0]
    QMetaObject.invokeMethod(palette, 'open')
    await asyncio.sleep(0.3)
    field = [child for child in palette.findChildren(QObject) if 'TextField' in child.metaObject().className()][0]
    field.setProperty('text', 'con')
    await shot('command-palette', 0.5)
    QMetaObject.invokeMethod(palette, 'close')

    # The update dialog, with a sample release (the screenshots cannot wait for a real one)
    update_controller = manager.updates
    update_controller._release = updates.Release(
        '1.1.0',
        'v1.1.0',
        'CANExplorer 1.1.0',
        '## Added\n\n- **Signal plots**: markers for trace bookmarks\n- **J1939**: DM4 freeze frames\n\n'
        '## Fixed\n\n- Replays of BLF files with CAN FD frames on Windows',
        'https://github.com/AKJ7/CANExplorer/releases/tag/v1.1.0',
        False,
        '2026-11-02',
        {f'CANExplorer-1.1.0-linux-{updates.machine()}.AppImage': '', 'SHA256SUMS.txt': ''},
    )
    update_controller._kind = updates.APPIMAGE  # As installed from the AppImage, which can update itself
    update_controller._set_state('available')
    update_dialog = items('UpdateDialog')[0]
    update_dialog.open()
    await shot('update', 0.6)
    update_dialog.close()
    update_controller._release = None
    update_controller._set_state('idle')

    for name, dialog_name in (('convert-database', 'ConvertDatabaseDialog'), ('preferences', 'PreferencesDialog')):
        dialog = items(dialog_name)[0]
        dialog.open()
        await shot(name)
        dialog.close()

    # Database editor, on a copy of the example database
    database = Path(tempfile.mkdtemp()) / 'vehicle.dbc'
    database.write_bytes((EXAMPLES / 'vehicle.dbc').read_bytes())
    editor = manager.database_editor
    editor.open_path(str(database))
    editor.select_message('EngineData')
    editor.select_signal('CoolantTemp')
    window.setProperty('tool', 'database-editor')
    await shot('database-editor')
    # The whole editor with the bit layout, a Motorola signal overlapping another one
    editor.update_signal({'byte_order': 'big_endian', 'start': 19, 'length': 12})
    window.resize(1440, 1400)
    await shot('database-editor-layout')
    window.resize(1440, 900)
    # A LIN database
    lin_database = Path(tempfile.mkdtemp()) / 'body.ldf'
    lin_database.write_bytes((DATA / 'full.ldf').read_bytes())
    editor.new_database()
    editor.open_path(str(lin_database))
    editor.select_message('Door_Frm1')
    editor.select_signal('Temperature')
    await shot('database-editor-lin')
    window.setProperty('tool', '')

    manager.close_all()
    if warnings:
        print('QML warnings:', warnings, file=sys.stderr)
    app.quit()


async def main():
    failed = False

    async def guarded():
        nonlocal failed
        try:
            await scenario()
        except BaseException:
            traceback.print_exc()
            failed = True
            app.quit()

    asyncio.ensure_future(guarded())
    closed = asyncio.Event()
    app.aboutToQuit.connect(closed.set)
    await closed.wait()
    return failed


if __name__ == '__main__':
    os.chdir(ROOT)
    with loop:
        sys.exit(1 if loop.run_until_complete(main()) or warnings else 0)
