import socket
import threading
import uuid

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QObject

from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.gui.help import HelpController
from can_explorer.main_application import create_qml_engine


@pytest.fixture
def window(qapp, qtbot):
    warnings = []
    metadata = ApplicationMetadata(parent=qapp)
    manager = ConnectionManager(parent=qapp)
    help_controller = HelpController(parent=qapp)
    engine = create_qml_engine(
        {'metadata': metadata, 'connectionManager': manager, 'helpController': help_controller},
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
        'security',
        'reverse',
        'media',
        'diagnostics',
        'obd',
        'flash',
        'uds-scan',
        'xcp',
        'gmlan',
        'simulation',
        'restbus',
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
    tabs.setProperty('currentIndex', 4)  # Related signals
    tools.related.find(0.99, 0.5)
    await finished(tools.related)
    qtbot.wait(100)
    tabs.setProperty('currentIndex', 5)  # Reaction
    qtbot.wait(50)
    tools.reaction.start_baseline()
    tools.reaction.mark_action()
    tools.reaction.capture()
    for _ in range(200):
        if not tools.reaction.running:
            break
        await asyncio.sleep(0.01)
        qapp.processEvents()
    qtbot.wait(50)

    tools.analysis.use(0)
    tabs.setProperty('currentIndex', 0)
    qtbot.wait(100)

    # Temporal replay: the scrubber bar and the bit grid at a playback position render
    tools.start_replay()
    for _ in range(200):
        if tools.timeline.active:
            break
        await asyncio.sleep(0.01)
        qapp.processEvents()
    assert tools.timeline.active
    tools.timeline.set_position(0.5)
    tools.timeline.play()
    qtbot.wait(150)  # qtbot pumps Qt, so the playback QTimer fires here
    tools.timeline.pause()
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


def test_database_editor_search_by_sender_and_undo(window, qtbot, data_dir, tmp_path):
    import shutil

    from PyQt6.QtCore import QObject

    engine, manager, warnings = window
    root = engine.rootObjects()[0]
    root.setProperty('tool', 'database-editor')
    editor = manager.database_editor
    shutil.copy(data_dir / 'complex.dbc', tmp_path / 'complex.dbc')
    assert editor.open_path(str(tmp_path / 'complex.dbc')) == ''
    qtbot.wait(50)
    card = root.findChild(QObject, 'databaseMessagesCard')
    search = root.findChild(QObject, 'databaseSearch')
    messages = root.findChild(QObject, 'databaseMessageList')
    senders = root.findChild(QObject, 'databaseSenderList')
    assert messages.property('count') == 5

    # Searching a signal name finds its message
    search.setProperty('text', 'wheel')
    qtbot.waitUntil(lambda: messages.property('count') == 1)

    # The sender tree, filtered by the same search: Gateway > VehicleSpeed > WheelSpeed, unfolded
    card.setProperty('bySender', True)
    qtbot.waitUntil(lambda: senders.property('visible'))
    assert senders.property('count') == 3
    search.setProperty('text', '')
    qtbot.waitUntil(lambda: senders.property('count') == 7)  # 2 nodes and their 5 messages, folded
    tree = editor.sender_model
    tree.set_all(True)
    qtbot.waitUntil(lambda: senders.property('count') == 21)  # And the 14 signals
    qtbot.wait(50)

    # Edits in either list are undone and redone
    editor.select('FloatData', 'Ratio')
    assert editor.duplicate_signal() == '' and editor.can_undo
    qtbot.waitUntil(lambda: senders.property('count') == 22)
    assert editor.undo() == ''
    qtbot.waitUntil(lambda: senders.property('count') == 21)
    assert editor.redo() == '' and editor.dirty
    tree.set_all(False)
    qtbot.waitUntil(lambda: senders.property('count') == 2)
    card.setProperty('bySender', False)
    qtbot.wait(50)
    assert warnings == []


def test_database_editor_stays_fast_on_a_large_database(window, qtbot, tmp_path):
    """Regression: the "By sender" list (then ListView sections over an array) took 26 s for 100 messages of 16
    signals, and searching or editing as long, even while hidden. 300 messages now take well under a second."""
    import time

    import cantools
    from cantools.database.can import Database, Message, Node, Signal
    from PyQt6.QtCore import QObject

    database = Database()
    database.nodes.extend(Node(name) for name in ('ECU1', 'ECU2', 'ECU3'))
    for index in range(300):
        signals = [Signal(f'Sig{index}_{bit}', bit * 4, 4, receivers=['ECU2']) for bit in range(16)]
        database.messages.append(
            Message(0x100 + index, f'Message{index}', 8, signals, senders=[f'ECU{index % 3 + 1}'], strict=False)
        )
    database.refresh()
    cantools.database.dump_file(database, str(tmp_path / 'large.dbc'))

    engine, manager, warnings = window
    root = engine.rootObjects()[0]
    root.setProperty('tool', 'database-editor')
    editor = manager.database_editor
    card = root.findChild(QObject, 'databaseMessagesCard')
    search = root.findChild(QObject, 'databaseSearch')
    senders = root.findChild(QObject, 'databaseSenderList')

    def timed(action):
        start = time.perf_counter()
        action()
        qtbot.wait(1)
        return time.perf_counter() - start

    durations = {
        'open': timed(lambda: editor.open_path(str(tmp_path / 'large.dbc'))),
        'search': timed(lambda: search.setProperty('text', 'sig12')),
        'edit': timed(lambda: (editor.select('Message12', 'Sig12_3'), editor.update_signal({'unit': 'V'}))),
        'by sender': timed(lambda: card.setProperty('bySender', True)),
        'by sender search': timed(lambda: search.setProperty('text', 'sig2')),
        'by sender edit': timed(lambda: editor.update_signal({'unit': 'W'})),
    }
    assert senders.property('count') > 0
    assert max(durations.values()) < 3, durations  # Seconds; the quadratic list took minutes here
    card.setProperty('bySender', False)
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

    # The multiplexing controls of the signal form (switch, multiplexed signal with its switch values)
    editor.select_message('Multiplexed')
    assert editor.multiplexers == ['Mux']
    by_name = {signal['name']: signal for signal in editor.signals}
    assert by_name['Mux']['is_multiplexer'] and by_name['ValueA']['multiplexer_ids'] == '0'
    assert by_name['ValueA']['multiplexer_signal'] == 'Mux'
    for name in ('Mux', 'ValueA', 'ValueB'):
        editor.select_signal(name)
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


def test_multiplexer_editing_through_the_controller(window):
    _, manager, _ = window
    editor = manager.database_editor
    editor.new_database()
    assert editor.add_message() == '' and editor.update_message({'length': 8}) == ''

    # A switch signal, then multiplexed signals that follow it
    assert editor.add_signal() == ''
    assert editor.update_signal({'name': 'Mode', 'start': 0, 'length': 2}) == ''
    assert editor.update_signal({'is_multiplexer': True}) == ''
    assert editor.multiplexers == ['Mode']
    assert editor.add_signal() == ''
    assert editor.update_signal({'name': 'Val', 'start': 8, 'length': 8, 'multiplexer_ids': '0, 1'}) == ''

    rows = {signal['name']: signal for signal in editor.signals}
    assert rows['Mode']['is_multiplexer'] and not rows['Val']['is_multiplexer']
    assert rows['Val']['multiplexer_ids'] == '0, 1' and rows['Val']['multiplexer_signal'] == 'Mode'

    # An invalid change comes back as an error string from the slot, without raising
    assert editor.add_message() == '' and editor.update_message({'length': 8}) == ''
    assert editor.add_signal() == '' and editor.update_signal({'start': 0, 'length': 8}) == ''
    assert 'multiplexer' in editor.update_signal({'multiplexer_ids': '0'})


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


def test_sidebar_collapse(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtCore import Qt
    from PyQt6.QtQuick import QQuickWindow
    from PyQt6.QtTest import QTest

    engine, manager, warnings = window
    view = sip.cast(engine.rootObjects()[0], QQuickWindow)
    view.resize(1200, 800)
    view.show()
    qtbot.wait(100)
    sidebar = next(item for item in _visual_items(view) if item.metaObject().className().startswith('Sidebar'))
    assert round(sidebar.width()) == 232

    QTest.keyClick(view, Qt.Key.Key_Backslash, Qt.KeyboardModifier.ControlModifier)  # Collapse to the icon rail
    qtbot.waitUntil(lambda: round(sidebar.width()) == 64)
    assert manager.app_state.sidebar_collapsed() is True
    # The logo and the view icons stay visible on the rail (only the text labels are hidden).
    logo = next(item for item in _visual_items(view) if item.metaObject().className().startswith('Logo'))
    assert logo.isVisible() and logo.width() > 0

    QTest.keyClick(view, Qt.Key.Key_Backslash, Qt.KeyboardModifier.ControlModifier)  # Reveal, back to its width
    qtbot.waitUntil(lambda: round(sidebar.width()) == 232)
    assert manager.app_state.sidebar_collapsed() is False
    assert warnings == []


def test_help_panel_opens_and_renders(window, qtbot):
    engine, _, warnings = window
    help_controller = engine.rootContext().contextProperty('helpController')

    # A page with an admonition, images and cross-links exercises the Markdown rendering path.
    help_controller.open('user-guide/trace-and-overview.md')
    qtbot.wait(150)
    assert help_controller.property('visible') is True
    assert help_controller.property('title') == 'Trace and overview'

    help_controller.navigate('user-guide/ethernet.md')
    qtbot.wait(50)
    assert help_controller.property('can_go_back') is True
    help_controller.back()
    assert help_controller.property('path') == 'user-guide/trace-and-overview.md'

    help_controller.close()
    qtbot.wait(50)
    assert help_controller.property('visible') is False
    assert warnings == []


def test_welcome_page_on_title_click(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtQuick import QQuickWindow
    from PyQt6.QtTest import QTest

    engine, manager, warnings = window
    view = sip.cast(engine.rootObjects()[0], QQuickWindow)
    view.resize(1200, 800)
    view.show()
    qtbot.wait(100)
    root = engine.rootObjects()[0]
    welcome = next(item for item in _visual_items(view) if item.metaObject().className().startswith('WelcomeView'))

    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    qtbot.wait(100)
    assert not welcome.property('visible')  # The connection is shown, not the welcome page

    # Click the logo/title in the sidebar header
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 40))
    qtbot.wait(50)
    assert root.property('tool') == 'welcome' and welcome.property('visible')

    # Adding another connection leaves the welcome page and shows the new connection
    manager.add_connection({'name': 'Bus2', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    qtbot.wait(100)
    assert root.property('tool') == '' and not welcome.property('visible')
    assert warnings == []


def test_switch_connection_shortcuts(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtCore import Qt
    from PyQt6.QtQuick import QQuickWindow
    from PyQt6.QtTest import QTest

    engine, manager, warnings = window
    view = sip.cast(engine.rootObjects()[0], QQuickWindow)
    view.resize(1200, 800)
    view.show()
    for name in ('A', 'B', 'C'):
        manager.add_connection({'name': name, 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    qtbot.wait(100)
    view.setProperty('selectedIndex', 0)
    view.setProperty('tool', 'multi-bus')  # Switching also leaves any open tool

    ctrl = Qt.KeyboardModifier.ControlModifier
    QTest.keyClick(view, Qt.Key.Key_PageDown, ctrl)  # Next connection
    qtbot.waitUntil(lambda: view.property('selectedIndex') == 1)
    assert view.property('tool') == ''

    QTest.keyClick(view, Qt.Key.Key_PageDown, ctrl)
    qtbot.waitUntil(lambda: view.property('selectedIndex') == 2)

    QTest.keyClick(view, Qt.Key.Key_PageDown, ctrl)  # Wraps past the last to the first
    qtbot.waitUntil(lambda: view.property('selectedIndex') == 0)

    QTest.keyClick(view, Qt.Key.Key_PageUp, ctrl)  # Previous wraps to the last
    qtbot.waitUntil(lambda: view.property('selectedIndex') == 2)
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
    assert ('Command', 'Collapse the sidebar') in titles  # The Ctrl+\ shortcut is offered too

    # Connection-switching commands appear once there is more than one connection
    assert not {('Connection', 'Next connection'), ('Connection', 'Previous connection')} & titles
    manager.add_connection({'name': 'Trailer', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    qtbot.wait(50)
    assert {('Connection', 'Next connection'), ('Connection', 'Previous connection')} <= set(commands())

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
    boxes = [dialog.findChild(QObject, 'themeBox'), dialog.findChild(QObject, 'languageBox')]
    texts = []
    for box in boxes:  # Theme (a list of strings) and language (objects with textRole "name")
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
    assert texts[1] == {'System language', 'English', 'Deutsch', 'Français', '中文', '日本語'}
    dialog.close()
    assert warnings == []


def test_theme_picker_previews_until_accepted(window, qtbot):
    """Picking a theme previews it at once; Cancel goes back, OK keeps it. System resolves to Light or Dark."""
    from PyQt6 import sip
    from PyQt6.QtCore import Q_ARG, QMetaObject, Qt
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    state = manager.app_state
    state.set_theme('Dark')
    theme = engine.singletonInstance('CanExplorer', 'Theme')
    root = sip.cast(engine.rootObjects()[0], QQuickWindow)
    dialog = next(
        item for item in root.findChildren(QObject) if item.metaObject().className().startswith('PreferencesDialog')
    )
    theme_box = dialog.findChild(QObject, 'themeBox')

    def pick(name, box=theme_box):
        index = engine.newQObject(box).property('model').toVariant().index(name)
        box.setProperty('currentIndex', index)
        QMetaObject.invokeMethod(box, 'activated', Qt.ConnectionType.DirectConnection, Q_ARG(int, index))
        qtbot.wait(30)

    dialog.open()
    qtbot.wait(100)
    pick('Nord')
    assert theme.property('effectiveName') == 'Nord' and state.theme == 'Dark'
    assert theme.property('background').name() == '#2b303b'
    dialog.reject()
    qtbot.wait(100)
    assert theme.property('preview') == '' and theme.property('effectiveName') == 'Dark'

    dialog.open()
    qtbot.wait(100)
    pick('Cyberpunk')
    dialog.accept()
    qtbot.wait(100)
    assert state.theme == 'Cyberpunk' and theme.property('effectiveName') == 'Cyberpunk'
    # Text on Cyberpunk's bright cyan accent is black, not white
    assert theme.property('onAccent').name() == '#000000'

    state.set_theme('System')
    qtbot.wait(30)
    system_dark = QGuiApplication.styleHints().colorScheme() != Qt.ColorScheme.Light
    assert theme.property('effectiveName') == ('Dark' if system_dark else 'Light')

    # The themes of System are chosen per mode, from the light and the dark themes respectively
    light_box, dark_box = dialog.findChild(QObject, 'systemLightBox'), dialog.findChild(QObject, 'systemDarkBox')
    dialog.open()
    qtbot.wait(100)
    assert light_box.property('visible') and dark_box.property('visible')
    light_themes = engine.newQObject(light_box).property('model').toVariant()
    dark_themes = engine.newQObject(dark_box).property('model').toVariant()
    assert {'Light', 'Solarized Light', 'High Contrast Light'} <= set(light_themes)
    assert {'Dark', 'Tokyo Night', 'Night Vision'} <= set(dark_themes)
    assert not set(light_themes) & set(dark_themes) and 'System' not in light_themes + dark_themes
    pick('Gruvbox Light', light_box)
    pick('Tokyo Night', dark_box)
    assert theme.property('effectiveName') == ('Tokyo Night' if system_dark else 'Gruvbox Light')
    assert state.system_dark_theme == 'Dark'  # Only previewed so far
    dialog.accept()
    qtbot.wait(100)
    assert (state.system_light_theme, state.system_dark_theme) == ('Gruvbox Light', 'Tokyo Night')
    assert theme.property('effectiveName') == ('Tokyo Night' if system_dark else 'Gruvbox Light')

    state.set_system_themes('Light', 'Dark')
    state.set_theme('Dark')
    qtbot.wait(30)
    assert not light_box.property('visible') or not dialog.property('visible')
    assert warnings == []


async def test_status_dots_show_the_state_by_shape(window, qapp):
    """Connection states are told apart by the shape of the sidebar's StatusDot, with the state in the tooltip."""
    import asyncio

    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    async def settle():
        for _ in range(10):
            await asyncio.sleep(0.01)
            qapp.processEvents()

    engine, manager, warnings = window
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    await settle()

    def statuses():
        stack, found = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()], []
        while stack:
            item = stack.pop()
            if item.metaObject().className().startswith('StatusDot') and item.property('status'):
                found.append(item.property('status'))
            stack.extend(item.childItems())
        return found

    assert statuses() == ['idle']  # Added closed: a hollow ring
    connection = manager.connections[0]
    connection.open()
    await settle()
    assert statuses() == ['ok']  # Open: a filled circle
    connection.close()
    await settle()
    assert statuses() == ['idle']
    assert warnings == []


async def test_views_show_their_activity_in_the_sidebar(window, qapp):
    """A view doing something (here tester present in Diagnostics) gets a mark after it, explained in its tooltip."""
    import asyncio

    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    async def settle():
        for _ in range(10):
            await asyncio.sleep(0.01)
            qapp.processEvents()

    engine, manager, warnings = window
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    connection = manager.connections[0]
    connection.open()
    await settle()

    def views():
        stack, found = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()], {}
        while stack:
            item = stack.pop()
            if item.metaObject().className().startswith('NavItem') and item.isVisible():
                found[item.property('text')] = item
            stack.extend(item.childItems())
        return found

    assert views()['Diagnostics'].property('status') == ''
    connection.diagnostics.set_tester_present(True)
    await settle()
    diagnostics = views()['Diagnostics']
    assert diagnostics.property('status') == 'ok'
    assert diagnostics.property('activity').toVariant() == {'status': 'ok', 'text': 'Tester present'}
    connection.diagnostics.set_tester_present(False)
    await settle()
    assert views()['Diagnostics'].property('status') == ''
    assert warnings == []


async def test_tools_show_their_activity_in_the_sidebar(window, qapp):
    """The gateway warns while its rules wait for their connections, and shows it forwards once they are open."""
    import asyncio

    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    async def settle(rounds=10):
        for _ in range(rounds):
            await asyncio.sleep(0.02)
            qapp.processEvents()

    engine, manager, warnings = window
    for name in ('A', 'B'):
        manager.add_connection({'name': name, 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    gateway = manager.gateway
    assert gateway.add_rule({'source': 'A', 'destination': 'B'}) == ''

    def tool_item(text):
        stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
        while stack:
            item = stack.pop()
            if item.metaObject().className().startswith('NavItem') and item.property('text') == text:
                return item
            stack.extend(item.childItems())

    def gateway_item():
        return tool_item('Bus gateway')

    def activity(text):
        item = tool_item(text)
        value = item.property('activity')
        return item.property('status'), value.toVariant()['text'] if value is not None else ''

    await settle()
    assert gateway_item().property('status') == ''

    # The database editor is active while a database is open, and warns about unsaved changes
    assert activity('Database editor') == ('', '')
    manager.database_editor.new_database()
    await settle()
    assert activity('Database editor') == ('ok', 'Editing Untitled')

    # The multi-bus trace waits for its connections, merges while one is open, and shows when it is paused
    multi_bus = manager.multi_bus
    multi_bus.set_selected('A', True)
    await settle()
    assert activity('Multi-bus trace') == ('idle', 'Waiting for A to connect')
    manager.connections[0].open()
    await settle()
    assert activity('Multi-bus trace') == ('ok', 'Merging A')
    multi_bus.set_paused(True)
    await settle()
    assert activity('Multi-bus trace') == ('idle', 'Paused: A')
    multi_bus.set_paused(False)
    multi_bus.set_selected('A', False)
    manager.connections[0].close()
    await settle()
    assert activity('Multi-bus trace') == ('', '')
    gateway.set_enabled(True)
    try:
        await settle()
        item = gateway_item()
        assert item.property('status') == 'warning'
        assert item.property('activity').toVariant()['text'] == '1 of 1 rules waiting for their connections'
        for connection in manager.connections:
            connection.open()
        await settle(40)  # The gateway refreshes its rules periodically
        item = gateway_item()
        assert item.property('status') == 'ok'
        assert item.property('activity').toVariant()['text'] == 'Forwarding, rules: 1'
    finally:
        gateway.set_enabled(False)
        gateway.clear_rules()
    await settle()
    assert gateway_item().property('status') == ''
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


class _VpwSocketAdapter:
    """A minimal ELM327 (J1850 VPW) adapter behind a TCP socket, enough for a connection to open and monitor."""

    def __init__(self):
        self.echo = True
        self.monitoring = False
        self._server = socket.create_server(('127.0.0.1', 0))
        self.url = f'socket://127.0.0.1:{self._server.getsockname()[1]}'
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            self._connection, _ = self._server.accept()
        except OSError:
            return
        line = ''
        while True:
            try:
                data = self._connection.recv(4096)
            except OSError:
                return
            if not data:
                return
            for char in data.decode('ascii'):
                if self.monitoring:
                    self.monitoring = False
                    self._connection.sendall(b'\r>')
                elif char == '\r':
                    self._handle(line.replace(' ', '').upper())
                    line = ''
                else:
                    line += char

    def _handle(self, command):
        if self.echo:
            self._connection.sendall((command + '\r').encode('ascii'))
        if command == 'ATZ':
            self.echo = True
            self._connection.sendall(b'\rELM327 v1.5\r\r>')
        elif command == 'ATE0':
            self.echo = False
            self._connection.sendall(b'OK\r\r>')
        elif command == 'ATMA':
            self.monitoring = True
        else:
            self._connection.sendall(b'OK\r\r>')

    def close(self):
        if hasattr(self, '_connection'):
            self._connection.close()
        self._server.close()


def test_j1850_class2_connection_gates_its_page(window, qtbot):
    """A connection on the elm327_vpw interface opens in the app and exposes the gated J1850 / Class 2 page."""
    engine, manager, warnings = window
    adapter = _VpwSocketAdapter()
    try:
        manager.add_connection({'name': 'Class2', 'interface': 'elm327_vpw', 'channel': adapter.url})
        qtbot.wait(200)
        connection = manager.connections[0]
        assert connection.is_j1850 and connection.j1850 is not None
    finally:
        adapter.close()
    assert warnings == []


def test_about_dialog_confirms_the_copy_to_clipboard(window, qtbot):
    from PyQt6.QtGui import QGuiApplication

    engine, _, warnings = window
    root = engine.rootObjects()[0]
    dialog = next(o for o in root.findChildren(QObject) if o.metaObject().className().startswith('AboutDialog'))
    dialog.open()
    label = dialog.findChild(QObject, 'copiedLabel')
    button = next(o for o in dialog.findChildren(QObject) if o.property('text') == 'Copy to Clipboard')
    assert label.property('text') == 'Copied to clipboard' and not label.property('shown')
    QGuiApplication.clipboard().clear()
    button.clicked.emit()
    assert label.property('shown')
    assert QGuiApplication.clipboard().text().startswith('CANExplorer (v')
    qtbot.waitUntil(lambda: not label.property('shown'), timeout=5000)  # Hidden again after a moment
    button.clicked.emit()
    dialog.close()
    dialog.open()
    assert not label.property('shown')  # Not left over when the dialog is opened again
    dialog.close()
    assert warnings == []


async def test_table_rows_are_selected_by_clicks(window, qapp, tmp_path):
    """In the trace and the overview, a left or right click selects the row, Shift+click selects a range from the
    current row, Ctrl+click toggles a row, and a right click inside the selection keeps it for the context menu."""
    import asyncio

    import can
    from PyQt6 import sip
    from PyQt6.QtCore import QPoint, QPointF, Qt, QUrl
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
                can.Message(arbitration_id=0x100 + i % 8, data=bytes([i % 256]), timestamp=i * 0.01)
            )
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 0.0)
    await settle(30)
    connection_view = next(i for i in _visual_items(view) if i.metaObject().className().startswith('ConnectionView'))
    shift, control = Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.ControlModifier

    for page in ('trace', 'overview'):
        connection_view.setProperty('page', page)
        await settle()
        frame_table = next(
            i for i in _visual_items(view) if i.metaObject().className().startswith('FrameTable') and i.isVisible()
        )
        table = next(
            i for i in _visual_items(view) if i.metaObject().className() == 'QQuickTableView' and i.isVisible()
        )
        selection = frame_table.property('selectionModel')
        if page == 'trace':  # Auto-scroll would keep the trace at its end; the clicks address rows from the top
            auto_scroll = next(
                i
                for i in _visual_items(view)
                if i.metaObject().className().startswith('CheckBox')
                and i.property('text') == 'Auto-scroll'
                and i.isVisible()
            )
            auto_scroll.setProperty('checked', False)
        table.setProperty('contentY', 0)
        await settle()
        js_table = engine.newQObject(table)
        row_height = js_table.property('rowHeight').callWithInstance(js_table, [0]).toNumber()
        assert row_height > 0

        async def click(
            row,
            button=Qt.MouseButton.LeftButton,
            modifiers=Qt.KeyboardModifier.NoModifier,
            table=table,
            height=row_height,
        ):
            point = table.mapToScene(QPointF(table.width() / 2, (row + 0.5) * height)).toPoint()
            QTest.mouseClick(view, button, modifiers, QPoint(point.x(), point.y()))
            await settle()
            if button == Qt.MouseButton.RightButton:
                QTest.keyClick(view, Qt.Key.Key_Escape)  # Close the context menu
                await settle()

        def rows(selection=selection):
            return sorted({index.row() for index in selection.selectedIndexes()})

        await click(1, Qt.MouseButton.RightButton)
        assert rows() == [1], page
        await click(2)
        assert rows() == [2], page
        await click(5, modifiers=shift)
        assert rows() == [2, 3, 4, 5], page
        await click(4, Qt.MouseButton.RightButton)  # Inside the selection: kept for the menu
        assert rows() == [2, 3, 4, 5], page
        await click(0, modifiers=shift)  # From the same anchor, the other way
        assert rows() == [0, 1, 2], page
        await click(6, modifiers=control)
        assert rows() == [0, 1, 2, 6], page
        await click(1, modifiers=control)
        assert rows() == [0, 2, 6], page
        await click(7, Qt.MouseButton.RightButton)  # Outside the selection: selects that row
        assert rows() == [7], page

        # A sloppy click still counts: the pointer drifts past the drag threshold, or the button is held long
        start = table.mapToScene(QPointF(table.width() / 2, 3.5 * row_height)).toPoint()
        QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        for step in range(1, 6):
            QTest.mouseMove(view, start + QPoint(step * 6, 0))
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start + QPoint(30, 0))
        await settle()
        assert rows() == [3], page
        start = table.mapToScene(QPointF(table.width() / 2, 1.5 * row_height)).toPoint()
        QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        await settle(120)  # Past the long-press threshold
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        await settle()
        assert rows() == [1], page
    assert warnings == []


def test_security_manager_renders_without_warnings(window, qtbot, tmp_path, monkeypatch, secoc_arxml):
    from PyQt6 import sip
    from PyQt6.QtCore import QUrl
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    security = manager.security
    monkeypatch.setattr(security.store, 'path', tmp_path / 'keys.json')
    security.store._scrypt_cost = 16
    root = engine.rootObjects()[0]
    view = sip.cast(root, QQuickWindow)
    dialog = next(
        o for o in view.findChildren(QObject) if o.metaObject().className().startswith('SecurityManagerDialog')
    )
    dialog.open()  # No key store yet
    qtbot.wait(100)
    assert security.create('pw', 'pw') == ''
    assert security.add_secoc_key('Powertrain', bytes(16).hex(), '*', '') == ''
    assert security.add_macsec_sa('Zone', bytes(16).hex(), '') == ''
    qtbot.wait(50)
    for purpose in ('tls-client', 'tls-ca', 'ipsec', 'macsec', 'seed-key', 'secoc'):
        dialog.setProperty('purpose', purpose)
        qtbot.wait(30)
    security.lock()  # The unlock form
    qtbot.wait(50)
    dialog.close()
    qtbot.wait(100)

    # The DoIP TLS choices, the Ethernet decryption note and a connection's SecOC status
    root.setProperty('tool', 'doip')
    qtbot.wait(50)
    root.setProperty('tool', 'ethernet')
    qtbot.wait(50)
    root.setProperty('tool', '')
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': f'qml-{uuid.uuid4().hex}'})
    manager.connections[-1].load_database(QUrl.fromLocalFile(str(secoc_arxml)))
    qtbot.wait(100)
    assert manager.connections[-1].secoc_protections[0]['reason'] == 'the key store is locked'
    # The SecOC parts of the connection pages: the transmit box, the restbus badge, the key store buttons
    stack = [view.contentItem()]
    while not stack[-1].metaObject().className().startswith('ConnectionView'):
        item = stack.pop()
        stack.extend(item.childItems())
    connection_view = stack[-1]
    for page in ('trace', 'restbus', 'diagnostics', 'gmlan', 'dashboard'):
        connection_view.setProperty('page', page)
        qtbot.wait(40)
    assert warnings == []


async def test_trace_auto_scroll_continues_with_selected_rows(window, qapp):
    """Auto-scroll keeps following new frames while rows are selected; jumping to a row (find, bookmark) switches
    it off so the shown row stays in view."""
    import asyncio

    import can
    from PyQt6 import sip
    from PyQt6.QtCore import QModelIndex, QPoint, QPointF, Qt
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

    channel = f'qml-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[0]
    connection.open()
    connection_view = next(i for i in _visual_items(view) if i.metaObject().className().startswith('ConnectionView'))
    connection_view.setProperty('page', 'trace')
    await settle()
    frame_table = next(
        i for i in _visual_items(view) if i.metaObject().className().startswith('FrameTable') and i.isVisible()
    )
    table = next(i for i in _visual_items(view) if i.metaObject().className() == 'QQuickTableView' and i.isVisible())
    auto_scroll = next(
        i
        for i in _visual_items(view)
        if i.metaObject().className().startswith('CheckBox') and i.property('text') == 'Auto-scroll' and i.isVisible()
    )
    model = frame_table.property('model')

    async def send(count):
        with can.Bus(interface='virtual', channel=channel) as bus:
            for i in range(count):
                bus.send(can.Message(arbitration_id=0x100 + i % 8, data=bytes([i % 256])))
        for _ in range(100):
            await settle(2)
            if model.rowCount(QModelIndex()) >= expected:
                break
        await settle()

    def at_bottom():
        return table.property('bottomRow') == model.rowCount(QModelIndex()) - 1

    expected = 100
    await send(100)
    assert at_bottom()

    # Select a row, then more frames arrive: still followed
    js_table = engine.newQObject(table)
    row_height = js_table.property('rowHeight').callWithInstance(js_table, [table.property('topRow')]).toNumber()
    point = table.mapToScene(QPointF(table.width() / 2, 2.5 * row_height)).toPoint()
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(point.x(), point.y()))
    await settle()
    assert frame_table.property('selectionModel').hasSelection()
    expected = 200
    await send(100)
    assert auto_scroll.property('checked')
    assert at_bottom()

    # Jumping to a row (as find and the bookmarks do) switches auto-scroll off and keeps the row in view
    js_frame_table = engine.newQObject(frame_table)
    js_frame_table.property('showRow').callWithInstance(js_frame_table, [5])
    await settle()
    assert not auto_scroll.property('checked')
    expected = 300
    await send(100)
    assert table.property('topRow') <= 5 <= table.property('bottomRow')

    # Switching it back on jumps to the newest frame
    auto_scroll.setProperty('checked', True)
    await settle()
    assert at_bottom()
    connection.close()
    assert warnings == []


def test_someip_e2e_dialog_renders_without_warnings(window, qtbot):
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    root = engine.rootObjects()[0]
    view = sip.cast(root, QQuickWindow)
    someip = manager.someip
    someip._settings = None  # Do not keep the protections in the shared test settings
    assert someip.add_e2e({'service_id': '1234', 'method_id': '8001', 'data_id': '1', 'profile': '6'}) == ''
    for tool in ('someip', 'ethernet'):
        root.setProperty('tool', tool)
        qtbot.wait(50)
        dialogs = [o for o in view.findChildren(QObject) if o.metaObject().className().startswith('SomeIpE2eDialog')]
        assert dialogs
        for dialog in dialogs:
            dialog.open()
            qtbot.wait(80)
            dialog.close()
            qtbot.wait(50)
    someip.remove_e2e(0)
    root.setProperty('tool', '')
    qtbot.wait(50)
    assert warnings == []
