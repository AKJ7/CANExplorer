import uuid

import pytest

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
    manager.close_all()
    for root in engine.rootObjects():
        root.deleteLater()
    engine.deleteLater()


def test_main_window_loads_without_warnings(window, qtbot):
    engine, _, warnings = window
    assert len(engine.rootObjects()) == 1
    qtbot.wait(100)
    assert warnings == []


def test_connection_views_render_without_warnings(window, qtbot):
    engine, manager, warnings = window
    manager.add_connection('Bus', 'VirtualBus', f'qml-{uuid.uuid4().hex}', '500000', 'J1939', False, False)
    qtbot.wait(100)
    manager.remove_connection(0)
    qtbot.wait(100)
    assert warnings == []
