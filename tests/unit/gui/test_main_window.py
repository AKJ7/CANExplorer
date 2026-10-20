import uuid

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QObject

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
    assert pages == [
        'dashboard',
        'trace',
        'overview',
        'signals',
        'health',
        'reverse',
        'diagnostics',
        'flash',
        'simulation',
        'tests',
    ]
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


async def test_reverse_engineering_page_renders_without_warnings(window, qapp, qtbot, tmp_path, data_dir):
    """The bit grid, byte table and bit field form with traffic, a notch, a plotted field and a field added to the
    database editor, and the tabs of the analysis tools with results."""
    import asyncio

    from PyQt6 import sip
    from PyQt6.QtCore import QObject, QUrl
    from PyQt6.QtQuick import QQuickWindow

    from can_explorer.core.decoding import open_signal_database
    from can_explorer.core.reverse.synthetic import generate

    engine, manager, warnings = window
    path = tmp_path / 'synthetic.asc'
    generate(open_signal_database(data_dir / 'complex.dbc'), duration=2.0).write(path)
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 0.0)
    connection = manager.connections[-1]
    for _ in range(50):
        await asyncio.sleep(0.01)
        qapp.processEvents()
    stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
    while not stack[-1].metaObject().className().startswith('ConnectionView'):
        item = stack.pop()
        stack.extend(item.childItems())
    view = stack[-1]
    view.setProperty('page', 'reverse')
    qtbot.wait(300)
    tools = connection.reverse
    assert tools.active and tools.identifiers
    tools.select('100')
    tools.notch()
    tools.select_bits(0, 15)
    qtbot.wait(300)
    assert tools.plot_field() == ''
    view.setProperty('page', 'signals')
    qtbot.wait(100)
    editor = manager.database_editor
    assert editor.add_found_signal(tools.signal_definition, '') == ''
    assert editor.current_message == 'Message_100' and editor.current_signal == 'Signal_0'
    qtbot.wait(100)

    # The tabs of the automatic analysis, reference signals and markers, with results
    async def finished(tool):
        for _ in range(500):
            if not tool.running:
                return
            await asyncio.sleep(0.01)
            qapp.processEvents()
        raise AssertionError('The tool did not finish')

    view.setProperty('page', 'reverse')
    tabs = view.findChild(QObject, 'reverseTabs')
    tools.analysis.run(False)
    await finished(tools.analysis)
    assert tools.analysis.results
    tabs.setProperty('currentIndex', 1)
    qtbot.wait(100)
    connection.load_database(QUrl.fromLocalFile(str(data_dir / 'complex.dbc')))
    tools.reference.find('signal:VehicleSpeed.WheelSpeed', 0.2)
    await finished(tools.reference)
    tabs.setProperty('currentIndex', 2)
    qtbot.wait(100)
    assert tools.markers.add('door') == ''
    tools.markers.find('', 0.5, 1.5)
    await finished(tools.markers)
    tabs.setProperty('currentIndex', 3)
    qtbot.wait(100)
    tools.analysis.use(0)
    tabs.setProperty('currentIndex', 0)
    qtbot.wait(100)
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

    # A LIN database: frames and signals, nodes, encoding types and schedule tables
    shutil.copy(data_dir / 'full.ldf', tmp_path / 'body.ldf')
    editor.new_database()  # No unsaved changes to ask about
    assert editor.open_path(str(tmp_path / 'body.ldf')) == ''
    qtbot.wait(50)
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    stack = [sip.cast(root, QQuickWindow).contentItem()]
    tabs = None
    while stack:
        item = stack.pop()
        if 'TabBar' in item.metaObject().className() and item.property('count') == 4:
            tabs = item
            break
        stack.extend(item.childItems())
    assert tabs is not None and tabs.isVisible()
    editor.select_message('Door_Frm1')
    for index in range(4):
        tabs.setProperty('currentIndex', index)
        qtbot.wait(40)
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
    # Move by as much as fits next to the (centred) dialog: its size depends on the platform's fonts
    dx = -min(200, int(x) - 1)
    dy = min(100, int(view.height() - y - dialog.property('height')) - 1)
    assert dx < -20 and dy > 20, (x, y, dialog.property('width'), dialog.property('height'))
    _drag(qtbot, view, (int(x) + 60, int(y) + 30), (int(x) + 60 + dx, int(y) + 30 + dy))
    assert (round(dialog.property('x') - x), round(dialog.property('y') - y)) == (dx, dy)
    _drag(qtbot, view, (int(x) + 60 + dx, int(y) + 30 + dy), (-500, -500))  # Stays inside the window
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


def test_j1939_page_renders_without_warnings(window, qtbot):
    import can
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    from can_explorer.core.j1939_tools import PGN_ADDRESS_CLAIMED, PGN_DM1, Dtc, J1939Name, build_can_id, encode_dm

    engine, manager, warnings = window
    manager.add_connection(
        {'name': 'Truck', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}', 'protocol': 'J1939'}
    )
    network = manager.connections[-1].j1939.network
    for can_id, data in (
        (build_can_id(PGN_ADDRESS_CLAIMED, 0x00), J1939Name((1 << 63) | (123 << 21) | 4567).to_bytes()),
        (build_can_id(PGN_DM1, 0x00), encode_dm([Dtc(110, 0, 3), Dtc(100, 1, 1)], {'MIL': 'on', 'AWL': 'on'})),
        (build_can_id(PGN_ADDRESS_CLAIMED, 0xFE), J1939Name(5).to_bytes()),
        (build_can_id(0xF004, 0x03), bytes(8)),
    ):
        network.on_message(can.Message(arbitration_id=can_id, is_extended_id=True, data=data))
    qtbot.wait(50)
    stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
    while not stack[-1].metaObject().className().startswith('ConnectionView'):
        item = stack.pop()
        stack.extend(item.childItems())
    view = stack[-1]
    assert 'j1939' in [page['key'] for page in view.property('pages').toVariant()]
    view.setProperty('page', 'j1939')
    qtbot.wait(600)  # One refresh of the node list
    stack = [view]
    while not stack[-1].metaObject().className().startswith('J1939View'):
        item = stack.pop()
        stack.extend(item.childItems())
    page = stack[-1]
    page.setProperty('selected', 0)
    qtbot.wait(50)
    page.setProperty('selected', 3)
    qtbot.wait(50)
    assert warnings == []
    manager.remove_connection(0)
    qtbot.wait(50)
    assert warnings == []


def test_canopen_page_renders_without_warnings(window, qtbot):
    from pathlib import Path

    import can
    from PyQt6 import sip
    from PyQt6.QtCore import QUrl
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    manager.add_connection(
        {'name': 'Machine', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}', 'protocol': 'CANopen'}
    )
    canopen = manager.connections[-1].canopen
    eds = Path(__file__).parents[3] / 'examples' / 'canopen' / 'io_module.eds'
    assert canopen.load_dictionary(QUrl.fromLocalFile(str(eds))) == ''
    for can_id, data in (
        (0x705, [0x05]),
        (0x085, [0x10, 0x42, 0x09, 0, 0, 0, 0, 0]),
        (0x185, [5, 0xB0, 4, 0xA2, 0xFE]),
    ):
        canopen.network.on_message(can.Message(arbitration_id=can_id, is_extended_id=False, data=data))
    canopen.set_node(5)
    stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
    while not stack[-1].metaObject().className().startswith('ConnectionView'):
        item = stack.pop()
        stack.extend(item.childItems())
    view = stack[-1]
    view.setProperty('page', 'canopen')
    qtbot.wait(600)
    stack = [view]
    while not stack[-1].metaObject().className().startswith('TabBar'):
        item = stack.pop()
        stack.extend(item.childItems())
    stack[-1].setProperty('currentIndex', 1)  # PDOs
    qtbot.wait(100)
    assert warnings == []
    manager.remove_connection(0)
    qtbot.wait(50)
    assert warnings == []


def test_compare_tool_renders_without_warnings(window, qtbot, data_dir):
    import can
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    from can_explorer.core.compare import compare, read_recording
    from can_explorer.core.decoding import open_signal_database

    engine, manager, warnings = window
    controller = manager.compare

    def frames(count, changed):
        return [
            can.Message(
                timestamp=i * 0.01, arbitration_id=0x100, is_extended_id=False, data=[i if i < changed else 0, 0, 90]
            )
            for i in range(count)
        ]

    a, b = read_recording('a.asc', frames(40, 40)), read_recording('b.blf', frames(30, 10))
    controller.set_recording('a', a)
    controller.set_recording('b', b)
    controller._database = open_signal_database(data_dir / 'example.dbc')
    controller._comparison = compare(a, b, database=controller._database)
    controller.result_changed.emit()
    window_item = sip.cast(engine.rootObjects()[0], QQuickWindow)
    window_item.setProperty('tool', 'compare')
    qtbot.wait(100)
    stack = [window_item.contentItem()]
    while not stack[-1].metaObject().className().startswith('TabBar'):
        item = stack.pop()
        stack.extend(item.childItems())
    stack[-1].setProperty('currentIndex', 1)  # Signals
    qtbot.wait(100)
    window_item.setProperty('tool', '')
    qtbot.wait(50)
    assert warnings == []


def test_multi_bus_tool_renders_without_warnings(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    for name in ('Powertrain', 'Body'):
        manager.add_connection({'name': name, 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    manager.multi_bus.set_selected('Body', True)
    window_item = sip.cast(engine.rootObjects()[0], QQuickWindow)
    window_item.setProperty('tool', 'multi-bus')
    qtbot.wait(150)
    window_item.setProperty('tool', '')
    qtbot.wait(50)
    assert warnings == []


async def test_ethercat_tool_renders_without_warnings(window, qapp):
    import asyncio

    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    ethercat = manager.ethercat
    window_item = sip.cast(engine.rootObjects()[0], QQuickWindow)
    window_item.setProperty('tool', 'ethercat')

    async def settle():
        for _ in range(10):
            await asyncio.sleep(0.01)
            qapp.processEvents()
        while ethercat.busy:
            await asyncio.sleep(0.01)
        qapp.processEvents()

    await settle()  # Closed: no slaves
    assert ethercat.open_network('simulated') == ''
    await settle()
    ethercat.select(3)
    assert ethercat.read_dictionary() == ''
    await settle()
    assert ethercat.read_pdo_configuration() == ''
    await settle()
    assert ethercat.sdo_write('8000', '02', '5000') == ''  # An emergency
    await settle()
    await ethercat._poll_states(ethercat._master)
    stack = [window_item.contentItem()]
    while not (stack[-1].metaObject().className().startswith('TabBar') and stack[-1].isVisible()):
        item = stack.pop()
        stack.extend(item.childItems())
    stack[-1].setProperty('currentIndex', 1)  # PDOs
    await settle()
    assert window_item.property('title') and ethercat.pdos and ethercat.emergencies
    ethercat.close_network()
    await settle()
    window_item.setProperty('tool', '')
    await settle()
    ethercat.shutdown()
    assert warnings == []


async def test_physical_io_tool_renders_without_warnings(window, qapp):
    import asyncio

    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    io = manager.physical_io
    window_item = sip.cast(engine.rootObjects()[0], QQuickWindow)
    window_item.setProperty('tool', 'physical-io')

    async def settle():
        for _ in range(10):
            await asyncio.sleep(0.01)
            qapp.processEvents()

    await settle()  # Closed
    assert io.open_device('simulated') == ''
    while not io.is_open:
        await asyncio.sleep(0.01)
    for line, output in ((0, True), (1, True), (4, False)):
        assert io.configure_line(line, output, 'up' if not output else 'none', False) == ''
        await settle()
    assert io.set_output(0, True) == ''
    for _ in range(5):
        await io._poll_once(io._io)
    await settle()
    assert io.lines[4]['level'] and len(io.analog) == 4
    io.close_device()
    await settle()
    window_item.setProperty('tool', '')
    await settle()
    io.shutdown()
    assert warnings == []


def test_bus_health_page_renders_without_warnings(window, qtbot):
    import time

    import can
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    from can_explorer.core.bushealth import ERR_BUSOFF, ERR_CNT, ERR_CRTL

    engine, manager, warnings = window
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    connection = manager.connections[-1]
    now = time.time()
    for second in range(120):
        if second == 50:
            connection.health.on_message(
                can.Message(arbitration_id=ERR_CRTL | ERR_CNT, is_error_frame=True, data=[0, 0x20, 0, 0, 0, 0, 130, 2])
            )
        if second == 80:
            connection.health.on_message(can.Message(arbitration_id=ERR_BUSOFF, is_error_frame=True, data=bytes(8)))
        connection.health.sample(now - 120 + second, 100, 10, 0.2, None)
    stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
    while not stack[-1].metaObject().className().startswith('ConnectionView'):
        item = stack.pop()
        stack.extend(item.childItems())
    view = stack[-1]
    view.setProperty('page', 'health')
    connection.bus_health.set_window(600)
    qtbot.wait(150)
    assert warnings == []


def test_command_palette_and_language(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    from can_explorer.gui.translations import TranslationManager

    engine, manager, warnings = window
    manager.add_connection({'name': 'Vehicle', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    qtbot.wait(50)
    root = sip.cast(engine.rootObjects()[0], QQuickWindow)
    js_root = engine.newQObject(root)

    def commands():
        array = js_root.property('paletteCommands').callWithInstance(js_root)
        return [
            (array.property(i).property('category').toString(), array.property(i).property('title').toString())
            for i in range(array.property('length').toInt())
        ]

    titles = set(commands())
    assert {('View', 'Bus health'), ('Tool', 'Compare recordings'), ('Theme', 'Dark')} <= titles
    assert {('Command', 'New Connection…'), ('Connection', 'Connect Vehicle'), ('Language', 'Deutsch')} <= titles

    palette = next(
        item for item in root.findChildren(QObject) if item.metaObject().className().startswith('CommandPalette')
    )
    js_palette = engine.newQObject(palette)
    js_palette.property('open').callWithInstance(js_palette)
    qtbot.wait(100)
    field = [child for child in palette.findChildren(QObject) if 'TextField' in child.metaObject().className()][0]
    field.setProperty('text', 'cmpr rec')
    qtbot.wait(50)
    matches = js_palette.property('matches')
    assert matches.property(0).property('title').toString() == 'Compare recordings'
    js_palette.property('run').callWithInstance(js_palette, [0])
    qtbot.wait(50)
    assert root.property('tool') == 'compare' and not palette.property('opened')

    # Switching the language retranslates the window at once
    translations = TranslationManager(QCoreApplication.instance())
    translations.set_language('de')
    engine.retranslate()
    qtbot.wait(50)
    assert ('Werkzeug', 'Aufzeichnungen vergleichen') in set(commands())
    translations.set_language('en')
    engine.retranslate()
    qtbot.wait(50)
    assert warnings == []


def test_combo_box_lists_show_their_texts(window, qtbot):
    """The styled ComboBox shows the texts of plain lists and of arrays of objects with a textRole."""
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickItem, QQuickWindow

    engine, manager, warnings = window
    root = sip.cast(engine.rootObjects()[0], QQuickWindow)
    dialog = next(
        item for item in root.findChildren(QObject) if item.metaObject().className().startswith('PreferencesDialog')
    )
    dialog.open()
    qtbot.wait(100)
    boxes = [item for item in dialog.findChildren(QObject) if 'ComboBox' in item.metaObject().className()]
    texts = []
    for box in boxes[:2]:  # Theme (a list of strings) and language (objects with textRole "name")
        popup = engine.newQObject(box).property('popup')
        popup.toQObject().setProperty('visible', True)
        qtbot.wait(100)
        stack = [sip.cast(popup.property('contentItem').toQObject(), QQuickItem)]
        shown = []
        while stack:
            item = stack.pop()
            if 'ItemDelegate' in item.metaObject().className():
                shown.append(item.property('text'))
            stack.extend(item.childItems())
        texts.append(set(shown))
        popup.toQObject().setProperty('visible', False)
    assert {'Dark', 'Light'} <= texts[0]
    assert texts[1] == {'System language', 'English', 'Deutsch'}
    dialog.close()
    assert warnings == []


def test_table_columns_can_be_resized(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtQuick import QQuickWindow
    from PyQt6.QtTest import QTest

    engine, manager, warnings = window
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    qtbot.wait(50)
    root = sip.cast(engine.rootObjects()[0], QQuickWindow)

    def items(start, class_prefix):
        found, stack = [], [start]
        while stack:
            item = stack.pop()
            if item.metaObject().className().startswith(class_prefix) and item.isVisible():
                found.append(item)
            stack.extend(item.childItems())
        return found

    view = items(root.contentItem(), 'ConnectionView')[0]
    for page in ('trace', 'overview'):
        view.setProperty('page', page)
        qtbot.wait(150)
        table = items(view, 'FrameTable')[0]
        js_table = engine.newQObject(table)

        def width(column, js_table=js_table):
            return js_table.property('columnWidth').callWithInstance(js_table, [column]).toNumber()

        handles = [item for item in items(table, 'QQuickItem') if item.objectName() == 'columnResizeHandle']
        handle = min(handles, key=lambda item: item.mapToScene(QPointF(0, 0)).x())  # The first column's
        before = width(0)
        start = handle.mapToScene(QPointF(handle.width() / 2, handle.height() / 2)).toPoint()
        QTest.mousePress(root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        for step in range(1, 13):
            QTest.mouseMove(root, start + QPoint(step * 5, 0))
            qtbot.wait(10)
        QTest.mouseRelease(root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start + QPoint(60, 0))
        qtbot.wait(50)
        assert abs(width(0) - (before + 60)) <= 6, (page, before, width(0))
        headers = [table.property('model').headerData(i, Qt.Orientation.Horizontal) for i in range(3)]
        assert not any(text.endswith(('▲', '▼')) for text in headers)  # Resizing does not sort
        start = handle.mapToScene(QPointF(handle.width() / 2, handle.height() / 2)).toPoint()
        QTest.mouseDClick(root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        qtbot.wait(50)
        assert width(0) == before  # A double click restores the configured width
    assert warnings == []


def test_update_dialog_renders_without_warnings(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    from can_explorer.core import updates

    engine, manager, warnings = window
    controller = manager.updates
    root = sip.cast(engine.rootObjects()[0], QQuickWindow)
    dialog = next(
        item for item in root.findChildren(QObject) if item.metaObject().className().startswith('UpdateDialog')
    )
    controller._release = updates.Release(
        '9.0.0',
        'v9.0.0',
        'CANExplorer 9.0.0',
        '## Added\n\n- **Everything**',
        'https://github.com',
        False,
        '2030-01-01',
    )
    for state in ('checking', 'current', 'available', 'downloading', 'installing', 'error'):
        controller._done, controller._total = 5_000_000, 10_000_000
        controller._set_state(state, 'Download failed' if state == 'error' else '')
        controller.progress_changed.emit()
        dialog.open()
        qtbot.wait(60)
        assert dialog.property('title') and dialog.property('visible')
    controller._set_state('available')
    dialog.close()
    qtbot.wait(50)  # The top bar shows the update button
    assert warnings == []
