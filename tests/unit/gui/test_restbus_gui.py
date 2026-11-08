import uuid

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from can_explorer.core.decoding import DbcDecoder, FrameDecoder
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.gui.restbus import RestbusController

DBC = """
VERSION ""
BU_: ECU1 TESTER
BO_ 256 EngineData: 8 ECU1
 SG_ Rpm : 0|16@1+ (1,0) [0|8000] "rpm" TESTER
BO_ 512 TesterMsg: 8 TESTER
 SG_ Req : 0|8@1+ (1,0) [0|255] "" ECU1
BA_DEF_ BO_ "GenMsgCycleTime" INT 0 10000;
BA_DEF_DEF_ "GenMsgCycleTime" 0;
BA_ "GenMsgCycleTime" BO_ 256 10;
"""


class FakeConnection(QObject):
    state_changed = pyqtSignal()

    def __init__(self, session, connected=True, can_transmit=True):
        super().__init__()
        self.session = session
        self.connected = connected
        self.can_transmit = can_transmit


@pytest.fixture
def dbc_path(tmp_path):
    path = tmp_path / 'restbus.dbc'
    path.write_text(DBC)
    return path


def _connection(dbc_path=None, **kwargs):
    channel = f'restbus-{uuid.uuid4().hex}'
    decoder = FrameDecoder(database=DbcDecoder(dbc_path)) if dbc_path else FrameDecoder()
    return FakeConnection(BusSession(LiveSource(BusConfig(interface='virtual', channel=channel)), decoder), **kwargs)


def test_detects_database(qapp, dbc_path):
    controller = RestbusController(_connection(dbc_path))
    assert controller.has_database
    assert [row['name'] for row in controller.messages] == ['EngineData', 'TesterMsg']
    assert controller.message_count == 2 and controller.enabled_count == 1
    assert controller.nodes == ['ECU1', 'TESTER']


def test_no_database(qapp):
    controller = RestbusController(_connection())
    assert not controller.has_database
    assert controller.messages == [] and controller.nodes == []
    assert 'database' in controller.start()


def test_database_changed_is_detected(qapp, dbc_path):
    connection = _connection()  # Starts without a database
    controller = RestbusController(connection)
    assert not controller.has_database
    connection.session.decoder.database = DbcDecoder(dbc_path)  # Database loaded later
    controller.database_changed()
    assert controller.has_database and controller.message_count == 2


def test_select_all_and_cyclic(qapp, dbc_path):
    controller = RestbusController(_connection(dbc_path))
    controller.set_all(True)
    assert controller.enabled_count == 2
    controller.set_all(False)
    assert controller.enabled_count == 0
    controller.enable_cyclic_only()
    assert controller.enabled_count == 1  # Only EngineData has a cycle time


def test_exclude_node(qapp, dbc_path):
    controller = RestbusController(_connection(dbc_path))
    controller.set_all(True)
    assert controller.exclude_node('ECU1') == 1
    assert controller.enabled_count == 1


def test_default_cycle(qapp, dbc_path):
    controller = RestbusController(_connection(dbc_path))
    controller.set_default_cycle(250)
    assert controller.default_cycle_ms == 250


def test_signals_and_set_value(qapp, dbc_path):
    connection = _connection(dbc_path)
    controller = RestbusController(connection)
    signals = controller.message_signals('EngineData')
    assert [s['name'] for s in signals] == ['Rpm']
    controller.set_value('EngineData', 'Rpm', '2000')
    database = connection.session.decoder.database.database
    data = controller._sim.get((256, False)).data
    assert database.get_message_by_name('EngineData').decode(data)['Rpm'] == 2000


def test_start_requires_connection(qapp, dbc_path):
    controller = RestbusController(_connection(dbc_path, connected=False))
    assert 'Connect' in controller.start()
    controller = RestbusController(_connection(dbc_path, can_transmit=False))
    assert 'transmit' in controller.start()


async def test_start_and_stop(qapp, dbc_path):
    connection = _connection(dbc_path)
    connection.session.start()
    controller = RestbusController(connection)
    try:
        assert controller.start() == ''
        assert controller.running
        controller.stop()
        assert not controller.running
    finally:
        controller.shutdown()
        connection.session.stop()
