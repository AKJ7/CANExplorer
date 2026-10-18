import uuid

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent

from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.main_application import create_qml_engine


@pytest.fixture
def window(qapp, qtbot):
    warnings = []
    metadata = ApplicationMetadata(parent=qapp)
    manager = ConnectionManager(parent=qapp)
    engine = create_qml_engine(
        {'metadata': metadata, 'connectionManager': manager},
        lambda errors: warnings.extend(error.toString() for error in errors),
    )
    yield engine, manager, warnings
    # Tear down while the application exists: views of open connections destroyed after the
    # QApplication (at interpreter exit) crash Qt
    while manager.connections:
        manager.remove_connection(0)
    for root in engine.rootObjects():
        root.deleteLater()
    engine.deleteLater()
    manager.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def test_main_window_loads_without_warnings(window, qtbot):
    engine, _, warnings = window
    assert len(engine.rootObjects()) == 1
    qtbot.wait(100)
    assert warnings == []


def test_connection_views_render_without_warnings(window, qtbot):
    engine, manager, warnings = window
    manager.add_connection(
        {'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}', 'protocol': 'J1939'}
    )
    qtbot.wait(100)
    manager.remove_connection(0)
    qtbot.wait(100)
    assert warnings == []


def test_dashboard_pages_and_themes_render_without_warnings(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    qtbot.wait(50)
    stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
    while not stack[-1].metaObject().className().startswith('ConnectionView'):
        item = stack.pop()
        stack.extend(item.childItems())
    view = stack[-1]
    pages = [page['key'] for page in view.property('pages').toVariant()]
    assert pages == ['dashboard', 'trace', 'overview', 'signals', 'diagnostics', 'simulation', 'tests']
    for page in pages:
        view.setProperty('page', page)
        qtbot.wait(30)
    for theme in manager.app_state.themes:
        manager.app_state.set_theme(theme)
        qtbot.wait(30)
    manager.app_state.set_theme('Dark')
    manager.error_occurred.emit('Something failed')
    qtbot.wait(50)
    assert warnings == []


async def test_playback_bars_render_without_warnings(window, qapp, tmp_path):
    import asyncio

    import can
    from PyQt6.QtCore import QUrl

    engine, manager, warnings = window
    path = tmp_path / 'log.asc'
    with can.Logger(path) as logger:
        for i in range(200):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=i * 0.01))
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 1.0)
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    bus = manager.connections[-1]
    bus.open()
    assert bus.bus_replay.start(QUrl.fromLocalFile(str(path)), 1.0, True, '') == ''
    for _ in range(20):
        await asyncio.sleep(0.01)
        qapp.processEvents()
    manager.connections[0].replay.set_paused(True)
    bus.bus_replay.stop()
    for _ in range(10):
        await asyncio.sleep(0.01)
        qapp.processEvents()
    assert warnings == []


def test_database_editor_renders_without_warnings(window, qtbot, data_dir, tmp_path):
    import shutil

    engine, manager, warnings = window
    root = engine.rootObjects()[0]
    root.setProperty('tool', 'database-editor')
    qtbot.wait(50)
    editor = manager.database_editor
    path = tmp_path / 'complex.dbc'
    shutil.copy(data_dir / 'complex.dbc', path)
    assert editor.open_path(str(path)) == ''
    qtbot.wait(50)
    for message in editor.messages[:4]:
        editor.select_message(message['name'])
        for signal in editor.signals[:3]:
            editor.select_signal(signal['name'])
            qtbot.wait(10)
    assert editor.add_message() == '' and editor.add_signal() == ''
    assert editor.update_message({'is_fd': True, 'length': 64}) == ''
    qtbot.wait(50)
    editor.new_database()
    qtbot.wait(30)
    root.setProperty('tool', '')
    qtbot.wait(30)
    assert warnings == []


async def test_tests_view_renders_while_running(window, qapp):
    import asyncio
    from pathlib import Path

    from PyQt6.QtCore import QUrl

    examples = Path(__file__).resolve().parents[3] / 'examples'
    engine, manager, warnings = window
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    connection = manager.connections[-1]
    connection.open()
    tests = connection.tests
    assert tests.load_script(QUrl.fromLocalFile(str(examples / 'testing' / 'vehicle_tests.py'))) == ''
    view = engine.rootObjects()[0]
    view.setProperty('selectedIndex', 0)
    for _ in range(5):
        await asyncio.sleep(0.01)
        qapp.processEvents()
    connection_view = next(
        item for item in _visual_items(view) if item.metaObject().className().startswith('ConnectionView')
    )
    connection_view.setProperty('page', 'tests')
    assert tests.run_case('GearboxTests') == ''  # Fails without database and simulation: renders errors too
    for _ in range(60):
        await asyncio.sleep(0.02)
        qapp.processEvents()
        if not tests.running:
            break
    tests.show_case('GearboxTests.obd_vehicle_speed_request')
    for _ in range(5):
        await asyncio.sleep(0.01)
        qapp.processEvents()
    assert warnings == []


def _visual_items(window):
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    stack = [sip.cast(window, QQuickWindow).contentItem()]
    while stack:
        item = stack.pop()
        yield item
        stack.extend(item.childItems())


def _drag(qtbot, window, start, end, steps=8):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest

    QTest.mouseMove(window, QPoint(*start))
    QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*start))
    for step in range(1, steps + 1):
        x = start[0] + (end[0] - start[0]) * step // steps
        y = start[1] + (end[1] - start[1]) * step // steps
        QTest.mouseMove(window, QPoint(x, y))
        qtbot.wait(10)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*end))
    qtbot.wait(30)


def test_sidebar_resizing_and_dialog_dragging(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtCore import QObject, QPoint, Qt
    from PyQt6.QtQuick import QQuickWindow
    from PyQt6.QtTest import QTest

    engine, manager, warnings = window
    view = sip.cast(engine.rootObjects()[0], QQuickWindow)
    view.resize(1200, 800)
    view.show()
    qtbot.wait(100)
    sidebar = next(item for item in _visual_items(view) if item.metaObject().className().startswith('Sidebar'))
    width = round(sidebar.width())
    assert width == 232

    _drag(qtbot, view, (width, 400), (width + 80, 400))
    assert round(sidebar.width()) == 312 and manager.app_state.sidebar_width() == 312
    _drag(qtbot, view, (312, 400), (20, 400))  # Not narrower than the minimum
    assert round(sidebar.width()) == manager.app_state.min_sidebar_width
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(180, 400))
    qtbot.wait(30)
    assert round(sidebar.width()) == 232 and manager.app_state.sidebar_width() == 232

    dialog = next(o for o in view.findChildren(QObject) if o.metaObject().className().startswith('PreferencesDialog'))
    dialog.open()
    qtbot.wait(200)
    x, y = dialog.property('x'), dialog.property('y')
    _drag(qtbot, view, (int(x) + 60, int(y) + 30), (int(x) - 140, int(y) + 130))
    assert (round(dialog.property('x') - x), round(dialog.property('y') - y)) == (-200, 100)
    _drag(qtbot, view, (int(x) - 80, int(y) + 130), (-500, -500))  # Stays inside the window
    assert dialog.property('x') >= 0 and dialog.property('y') >= 0
    dialog.close()
    qtbot.wait(200)
    dialog.open()  # Centred again
    qtbot.wait(200)
    assert (round(dialog.property('x')), round(dialog.property('y'))) == (round(x), round(y))
    dialog.close()
    qtbot.wait(100)
    assert warnings == []


async def test_trace_tools_and_record_dialog_render(window, qapp, tmp_path):
    import asyncio

    import can
    from PyQt6 import sip
    from PyQt6.QtCore import QObject, QPoint, Qt, QUrl
    from PyQt6.QtQuick import QQuickWindow
    from PyQt6.QtTest import QTest

    engine, manager, warnings = window
    view = sip.cast(engine.rootObjects()[0], QQuickWindow)
    view.resize(1300, 850)
    view.show()

    async def settle(steps=10):
        for _ in range(steps):
            await asyncio.sleep(0.01)
            qapp.processEvents()

    path = tmp_path / 'trace.asc'
    with can.Logger(path) as logger:
        for i in range(200):
            logger.on_message_received(
                can.Message(arbitration_id=0x100 + i % 4, data=bytes([i % 256]), timestamp=i * 0.01)
            )
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 0.0)
    connection = manager.connections[-1]
    await settle(30)
    connection_view = next(i for i in _visual_items(view) if i.metaObject().className().startswith('ConnectionView'))
    connection_view.setProperty('page', 'trace')
    await settle()

    # Find with the keyboard, bookmark the result, show the bookmarks
    QTest.keyClick(view, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    for character in '103 data=0B':
        QTest.keyClick(view, character)
    QTest.keyClick(view, Qt.Key.Key_Return)
    await settle(20)
    assert connection.trace_tools.status.startswith('Row 12 ')
    QTest.keyClick(view, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    await settle()
    assert connection.trace_tools.bookmark_count == 1
    find_bar = next(i for i in _visual_items(view) if i.metaObject().className().startswith('TraceFindBar'))
    popup = next(o for o in find_bar.findChildren(QObject) if o.metaObject().className().startswith('QQuickPopup'))
    popup.setProperty('visible', True)
    await settle()
    popup.setProperty('visible', False)

    # Context menu with a right click on a row
    table = next(i for i in _visual_items(view) if i.metaObject().className() == 'QQuickTableView' and i.isVisible())
    from PyQt6.QtCore import QPointF

    point = table.mapToScene(QPointF(table.width() / 2, 40)).toPoint()
    QTest.mouseClick(view, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(point.x(), point.y()))
    await settle()
    QTest.keyClick(view, Qt.Key.Key_Escape)

    # The record dialog with its trigger section
    dialog = next(
        o for o in connection_view.findChildren(QObject) if o.metaObject().className().startswith('RecordDialog')
    )
    dialog.open()
    await settle(20)
    dialog.close()
    await settle()
    assert warnings == []


def test_workspace_flow_in_the_window(window, qtbot, tmp_path):
    import json

    from PyQt6 import sip
    from PyQt6.QtCore import QObject, QUrl
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    view = sip.cast(engine.rootObjects()[0], QQuickWindow)
    view.show()
    channel = f'qml-{uuid.uuid4().hex}'
    for name in ('First', 'Second'):
        manager.add_connection({'name': name, 'interface': 'VirtualBus', 'channel': f'{channel}-{name}'})
    workspace = tmp_path / 'bench.cxw'
    assert manager.save_workspace(QUrl.fromLocalFile(str(workspace)), 1) == ''
    qtbot.wait(50)
    assert view.title().startswith('bench')

    # Opening a recent workspace while connections are open asks first
    actions = engine.singletonInstance('CanExplorer', 'Actions')
    actions.openRecentWorkspace.emit(0)
    qtbot.wait(100)
    dialog = next(
        o
        for o in view.findChildren(QObject)
        if o.property('title') == 'Open workspace' and o.metaObject().className().startswith('Dialog')
    )
    assert dialog.property('visible')
    dialog.accept()
    qtbot.wait(100)
    assert [connection.name for connection in manager.connections] == ['First', 'Second']
    assert view.property('currentIndex') == 1  # The connection selected when it was saved

    # Save Workspace (Ctrl+S) saves to the same file
    view.setProperty('selectedIndex', 0)
    actions.property('saveWorkspace').trigger()
    qtbot.wait(50)
    assert json.loads(workspace.read_text())['current'] == 0
    assert warnings == []


def test_lin_connection_views_render_without_warnings(window, qtbot, data_dir):
    from PyQt6.QtCore import QUrl

    engine, manager, warnings = window
    manager.add_connection(
        {'name': 'LIN', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}', 'bus': 'LIN'}
    )
    connection = manager.connections[-1]
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.ldf')))
    qtbot.wait(150)
    assert warnings == []
    manager.remove_connection(0)
    qtbot.wait(50)
    assert warnings == []


def test_database_bus_selector_and_description_render_without_warnings(window, qtbot, data_dir):
    from PyQt6.QtCore import QUrl

    engine, manager, warnings = window
    manager.add_connection({'name': 'CAN', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    connection = manager.connections[-1]
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.arxml')))
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'example.cdd')))
    qtbot.wait(150)
    connection.set_database_bus('Chassis')
    qtbot.wait(50)
    assert warnings == []


def test_simulation_panels_render(window, qtbot, data_dir, tmp_path):
    from pathlib import Path

    from PyQt6.QtCore import QUrl

    examples = Path(__file__).resolve().parents[3] / 'examples' / 'simulation'
    engine, manager, warnings = window
    manager.add_connection({'name': 'Sim', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    simulation = manager.connections[-1].simulation
    manager.connections[-1].load_database(QUrl.fromLocalFile(str(examples / 'vehicle.dbc')))
    assert simulation.add_panel(QUrl.fromLocalFile(str(examples / 'engine_panel.qml'))) == ''
    broken = tmp_path / 'broken.qml'
    broken.write_text('import QtQuick 2.15\nItem { property var panel; width: undefined_name }\nsyntax error')
    assert simulation.add_panel(QUrl.fromLocalFile(str(broken))) == ''
    qtbot.wait(200)
    assert warnings == []
    errors = [line for line in simulation.console if line['level'] == 'error']
    assert len(errors) == 1 and errors[0]['source'] == 'panel broken' and 'broken.qml' in errors[0]['text']
