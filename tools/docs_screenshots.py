"""Regenerate the screenshots of the documentation (docs/assets/screenshots).

Usage: QT_QPA_PLATFORM=offscreen uv run python tools/docs_screenshots.py

Drives the application with simulated buses, ECUs and LIN slaves; no hardware is needed.
"""

import asyncio
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

import qasync
from PyQt6 import sip
from PyQt6.QtCore import QObject, QSettings, QUrl
from PyQt6.QtQuick import QQuickWindow

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'docs' / 'assets' / 'screenshots'
DATA = ROOT / 'tests' / 'data'
EXAMPLES = ROOT / 'examples' / 'simulation'

# Use throwaway settings, so the screenshots do not depend on (or change) the user's
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, tempfile.mkdtemp())

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


async def wait_idle(diagnostics):
    while diagnostics.busy:
        await asyncio.sleep(0.02)


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

    show_page('dashboard')
    await shot('dashboard', 1.0)
    show_page('trace')
    await shot('trace')
    show_page('overview')
    connection.overview.sort_by(3)
    connection.overview.sort_by(3)  # Count, descending
    await shot('overview')
    show_page('signals')
    await shot('signals', 1.0)
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
    simulation.stop()
    ecu.stop()
    ecu_session.stop()

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
    lin.stop_schedule()
    simulator.close()
    slaves.stop()

    for name, dialog_name in (('convert-database', 'ConvertDatabaseDialog'), ('preferences', 'PreferencesDialog')):
        dialog = items(dialog_name)[0]
        dialog.open()
        await shot(name)
        dialog.close()

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
