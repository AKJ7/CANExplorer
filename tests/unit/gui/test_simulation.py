import asyncio
import uuid
from pathlib import Path

import can
import pytest
from PyQt6.QtCore import QUrl

from can_explorer.gui.connection_manager import ConnectionManager

EXAMPLES = Path(__file__).resolve().parents[3] / 'examples' / 'simulation'


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.02)


@pytest.fixture
def connection(qapp):
    manager = ConnectionManager()
    channel = f'simulation-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Sim', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[0]
    yield connection, channel
    manager.close_all()


async def test_example_simulation(connection):
    connection, channel = connection
    simulation = connection.simulation
    assert simulation.load_script(QUrl.fromLocalFile(str(EXAMPLES / 'engine_simulation.py'))) == ''
    assert simulation.scripts[0]['nodes'] == 'Engine, Gearbox'
    assert simulation.start() == 'Connect before starting the simulation'
    connection.open()
    assert 'Load a signal database' not in simulation.start()  # Starts; sending fails until a database is loaded
    await asyncio.sleep(0.15)
    assert any(line['level'] == 'error' and 'Load a signal database' in line['text'] for line in simulation.console)
    simulation.stop()
    simulation.clear_console()
    connection.load_database(QUrl.fromLocalFile(str(EXAMPLES / 'vehicle.dbc')))
    assert simulation.start() == '' and simulation.running

    panel = simulation.panel
    engine_speed = panel.signal('EngineData.EngineSpeed')
    gear = panel.variable('gear')
    assert engine_speed.unit == 'rpm' and engine_speed.text == '–'
    assert simulation.set_variable('ignition', 'true') == ''
    assert simulation.set_variable('target_speed', '3000') == ''
    await wait_until(lambda: simulation.runtime.variables.get('gear') == 'Second')
    await asyncio.sleep(0.1)
    panel._flush()
    assert engine_speed.number > 2500 and engine_speed.text.endswith(' rpm')
    assert gear.value == 'Second' and gear.text == 'Second'
    assert {'name': 'ignition', 'value': 'True'} in simulation.variables
    assert any('shifted to Second' in line['text'] for line in simulation.console)

    # OBD request from another bus participant, answered by the Gearbox node
    peer = can.Bus(interface='virtual', channel=channel)
    peer.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=bytes([2, 1, 0x0D, 0, 0, 0, 0, 0])))
    await wait_until(lambda: any(f.message.arbitration_id == 0x7E8 for f in connection.session.trace))
    peer.shutdown()

    # Sending from panels
    assert panel.send('VehicleSpeed', {'WheelSpeed': 12}) == ''
    assert 'Unknown message' in panel.send('Nope', {})
    assert panel.send_raw('123', '01 02') == ''
    assert 'Invalid hex' in panel.send_raw('xyz', '')
    panel.log('hello')
    assert simulation.console[-1]['source'] == 'panel' and simulation.console[-1]['text'] == 'hello'

    connection.close()
    assert not simulation.running


async def test_scripts_and_panels_management(connection, tmp_path):
    connection, _ = connection
    simulation = connection.simulation
    broken = tmp_path / 'broken.py'
    broken.write_text('import nothing_like_this\n')
    error = simulation.load_script(QUrl.fromLocalFile(str(broken)))
    assert 'ModuleNotFoundError' in error and simulation.scripts[0]['error'] == error
    broken.write_text('from can_explorer.scripting import Node\nclass Fixed(Node):\n    pass\n')
    assert simulation.reload_script(0) == '' and simulation.scripts[0]['nodes'] == 'Fixed'
    simulation.remove_script(0)
    assert simulation.scripts == []
    assert simulation.add_panel(QUrl.fromLocalFile(str(EXAMPLES / 'engine_panel.qml'))) == ''
    assert simulation.add_panel(QUrl.fromLocalFile(str(EXAMPLES / 'engine_panel.qml'))) == ''  # Not twice
    assert [panel['name'] for panel in simulation.panels] == ['engine_panel']
    assert 'Not a QML file' in simulation.add_panel(QUrl.fromLocalFile(str(broken)))
    simulation.remove_panel(0)
    assert simulation.panels == []
    assert simulation.set_variable('', '1') == 'Enter a variable name'
    simulation.set_variable('mode', '0x10')
    simulation.set_variable('ratio', '1.5')
    simulation.set_variable('label', 'eco')
    assert simulation.runtime.variables.get('mode') == 16
    assert simulation.runtime.variables.get('ratio') == 1.5
    assert simulation.runtime.variables.get('label') == 'eco'
