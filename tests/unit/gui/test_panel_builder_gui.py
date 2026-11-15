"""The panel builder: the controller, the panel it generates running on a simulation's live panel object, and the
dialog rendering without QML warnings."""

import os
import uuid
from pathlib import Path

import pytest
from PyQt6.QtCore import QObject, QUrl
from PyQt6.QtQml import QQmlComponent, QQmlEngine

from can_explorer.util.gui import get_res_path

EXAMPLES = Path(__file__).resolve().parents[3] / 'examples' / 'simulation'


@pytest.fixture
def connection(qapp):
    from can_explorer.gui.connection_manager import ConnectionManager

    manager = ConnectionManager()
    manager.add_connection({'name': 'Bench', 'interface': 'VirtualBus', 'channel': f'builder-{uuid.uuid4().hex}'})
    connection = manager.connections[-1]
    connection.load_database(QUrl.fromLocalFile(str(EXAMPLES / 'vehicle.dbc')))
    yield connection
    manager.close_all()


@pytest.fixture
def engine(qapp):
    os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'CanExplorerStyle')
    engine = QQmlEngine()
    engine.addImportPath(str(get_res_path('.')))
    warnings = []
    engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
    engine.collected_warnings = warnings
    return engine


def build(builder):
    builder.set_panel('Bench', 640, 400)
    gauge = builder.add('gauge', 10, 10)
    builder.update(gauge, {'source': 'signal:EngineData.EngineSpeed', 'max': 4000, 'warn': 3500, 'unknown': 1})
    switch = builder.add('switch', 260, 10)
    builder.update(switch, {'label': 'Ignition', 'variable': 'ignition'})
    button = builder.add('button', 260, 80)
    builder.update(button, {'label': 'Gear 3', 'action': 'variable', 'variable': 'gear', 'value': '3'})
    raw = builder.add('button', 260, 140)
    builder.update(raw, {'label': 'OBD speed', 'action': 'raw', 'id': '7DF', 'data': '02 01 0D'})
    lamp = builder.add('lamp', 260, 200)
    builder.update(lamp, {'source': 'variable:ignition'})
    for kind in ('value', 'bar', 'trend', 'slider', 'label'):
        index = builder.add(kind, 400, 300)
        if kind != 'label':
            builder.update(index, {'source': 'signal:EngineData.CoolantTemp', 'variable': 'target_speed'})


def test_controller_editing_and_files(connection, tmp_path):
    builder = connection.panel_builder
    assert {kind['kind'] for kind in builder.kinds} >= {'gauge', 'lamp', 'trend', 'slider', 'button'}
    assert 'signal:EngineData.EngineSpeed' in builder.sources
    index = builder.add('gauge', 13, 30)  # Snapped to the 8 px grid
    assert builder.selected == index and (builder.selected_widget['x'], builder.selected_widget['y']) == (16, 32)
    assert builder.problems == ['Gauge 1: choose the signal or variable it shows'] and builder.qml == ''
    builder.update(index, {'source': 'signal:EngineData.EngineSpeed', 'colour': 'ignored'})
    assert builder.problems == [] and builder.qml.startswith('// CANExplorer panel builder layout:')
    builder.move(index, 10_000, 10_000)  # Kept inside the panel
    assert (builder.widgets[0]['x'], builder.widgets[0]['y']) == (640 - 200, 400 - 200)
    builder.resize(index, 5, 5)
    assert builder.widgets[0]['width'] == 16
    copy = builder.duplicate(index)
    assert copy == 1 and builder.widgets[1]['props'] == builder.widgets[0]['props']
    builder.raise_widget(1, -1)
    assert builder.selected == 0
    assert builder.select_at(builder.widgets[1]['x'] + 1, builder.widgets[1]['y'] + 1) == 1
    builder.remove(0)
    assert len(builder.widgets) == 1 and builder.dirty

    assert builder.save(QUrl()) == 'Choose a file name'
    assert builder.use_in_simulation() == 'Save the panel first'
    assert builder.save(QUrl.fromLocalFile(str(tmp_path / 'bench'))) == ''
    assert builder.file_name == 'bench.qml' and not builder.dirty
    assert builder.use_in_simulation() == ''
    assert connection.simulation.panels[-1]['name'] == 'bench'
    builder.new_panel()
    assert builder.widgets == [] and builder.file_name == ''
    assert builder.open(QUrl.fromLocalFile(str(tmp_path / 'bench.qml'))) == ''
    assert len(builder.widgets) == 1 and builder.file_name == 'bench.qml'
    assert 'not made with the panel builder' in builder.open(QUrl.fromLocalFile(str(EXAMPLES / 'engine_panel.qml')))


def test_generated_panel_runs_on_the_live_panel(connection, engine, qapp, qtbot):
    builder = connection.panel_builder
    build(builder)
    assert builder.problems == []
    component = QQmlComponent(engine)
    component.setData(builder.qml.encode(), QUrl.fromLocalFile('/panel.qml'))
    panel = component.createWithInitialProperties({'panel': connection.simulation.panel})
    assert panel is not None, component.errorString()
    qtbot.wait(150)
    variables = connection.simulation.runtime.variables

    def find(kind, text):
        return next(
            child
            for child in panel.findChildren(QObject)
            if kind in child.metaObject().className() and child.property('text') == text
        )

    switch = find('Switch', 'Ignition')
    variables.set('ignition', True)
    qtbot.waitUntil(lambda: switch.property('checked') is True)
    find('Button', 'Gear 3').clicked.emit()
    assert variables.get('gear') == 3
    find('Button', 'OBD speed').clicked.emit()  # Sends 7DF on the connection (not open: logged, no error)
    qtbot.wait(150)
    assert engine.collected_warnings == []
    panel.deleteLater()


def test_dialog_renders_without_warnings(connection, engine, qtbot):
    engine.rootContext().setContextProperty('connectionManager', connection.parent())
    build(connection.panel_builder)
    component = QQmlComponent(engine)
    component.setData(
        b'import QtQuick 2.15\nimport QtQuick.Controls 2.15\nimport CanExplorer 1.0\n'
        b'ApplicationWindow { width: 1300; height: 900; property alias dialog: builderDialog\n'
        b'PanelBuilderDialog { id: builderDialog; connection: connectionObject } }',
        QUrl.fromLocalFile('/window.qml'),
    )
    engine.rootContext().setContextProperty('connectionObject', connection)
    window = component.create()
    assert window is not None, component.errorString()
    dialog = window.property('dialog')
    dialog.open()
    qtbot.wait(150)
    connection.panel_builder.select(0)  # The inspector of a gauge, then of a button
    qtbot.wait(50)
    connection.panel_builder.select(2)
    qtbot.wait(50)
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickItem

    def items(item):
        yield item
        for child in item.childItems():
            yield from items(child)

    content = sip.cast(dialog.property('contentItem'), QQuickItem)
    tabs = next(
        item for item in items(content) if 'TabBar' in item.metaObject().className() and item.property('count') == 2
    )
    tabs.setProperty('currentIndex', 1)  # The preview runs the generated panel
    qtbot.wait(200)
    dialog.close()
    qtbot.wait(50)
    assert engine.collected_warnings == []
    window.deleteLater()
