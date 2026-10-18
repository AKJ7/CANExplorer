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
    assert pages == ['dashboard', 'trace', 'overview', 'signals', 'diagnostics', 'simulation']
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
