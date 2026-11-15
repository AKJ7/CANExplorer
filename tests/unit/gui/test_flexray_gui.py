import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QObject, QUrl
from PyQt6.QtQml import QQmlComponent, QQmlEngine

from can_explorer.core import flexray, flexray_vector
from can_explorer.gui.flexray import FlexRayController
from can_explorer.util.gui import get_res_path

DATA = Path(__file__).resolve().parents[2] / 'data'


@pytest.fixture
def controller(qapp):
    controller = FlexRayController()
    yield controller
    controller.shutdown()


def test_simulated_cluster_recorded_and_reopened(controller, qtbot, tmp_path):
    assert 'Load the cluster description' in controller.start_simulation(5)
    assert 'neither FIBEX nor AUTOSAR' in controller.load_database(QUrl.fromLocalFile(str(DATA / 'example.cdd')))
    assert controller.load_database(QUrl.fromLocalFile(str(DATA / 'flexray.arxml'))) == ''
    assert controller.cluster == 'Chassis' and len(controller.definitions) == 3
    assert controller.definitions[0] == {
        'name': 'FrEngine',
        'slot': 12,
        'cycles': 'every cycle',
        'channels': 'A+B',
        'length': 8,
        'signals': 'EngineSpeed, GearPosition, OilPressure',
    }
    record = tmp_path / 'drive'
    assert controller.start_simulation(2) == '' and controller.running and controller.source == 'simulation'
    assert controller.start_recording(QUrl.fromLocalFile(str(record))) == ''
    assert controller.recording and controller.recording_file == 'drive.pcap'
    qtbot.waitUntil(lambda: controller.statistics['frames'] >= 40, timeout=3000)
    controller.stop()
    controller.stop_recording()
    rows = controller.frames
    assert rows[0]['info'].startswith('FrEngine: EngineSpeed=') and rows[0]['channel'] in 'AB'
    slots = {(row['slot'], row['channel']) for row in controller.slots}
    assert {(12, 'A'), (12, 'B')} <= slots
    assert controller.set_filter('40-50') == '' and {row['slot'] for row in controller.frames} <= {40, 50}
    assert 'Not a slot' in controller.set_filter('x')
    controller.set_filter('')

    recorded = flexray.read_capture(tmp_path / 'drive.pcap')
    assert len(recorded) >= controller.statistics['frames'] - 10
    controller.clear()
    assert controller.open_capture(QUrl.fromLocalFile(str(tmp_path / 'drive.pcap'))) == ''
    assert controller.source == 'capture' and controller.statistics['frames'] == len(recorded)
    assert controller.save_frames(QUrl.fromLocalFile(str(tmp_path / 'copy'))) == ''
    assert len(flexray.read_capture(tmp_path / 'copy.pcap')) == len(recorded)
    assert 'Not a pcap' in controller.open_capture(QUrl.fromLocalFile(str(DATA / 'example.dbc')))


def test_vector_spy_through_the_controller(qapp, qtbot):
    from tests.unit.core.test_flexray import FakeXl, spy_event

    library = FakeXl()
    controller = FlexRayController(vector_factory=lambda: flexray_vector.VectorFlexRaySpy(library))
    try:
        assert 'Find channels first' in controller.start_vector(0)
        assert controller.refresh_vector_channels() == ''
        assert controller.vector_channels == [{'name': 'VN7610 FlexRay', 'mask': 0x10, 'serial': 0}]
        assert controller.start_vector(0) == '' and controller.source_name == 'VN7610 FlexRay'
        library.events = [spy_event(5, 1, bytes(4)), spy_event(6, 1, bytes(2), error=0x04)]
        qtbot.waitUntil(lambda: controller.statistics['frames'] == 2, timeout=2000)
        assert controller.statistics['errors'] == 1 and controller.frames[1]['info'] == 'frame CRC error'
    finally:
        controller.shutdown()


def test_cluster_parameters_pdus_and_clusters(controller):
    url = QUrl.fromLocalFile(str(DATA / 'flexray_pdus.arxml'))
    assert controller.load_database(url) == ''
    assert controller.clusters == ['Chassis', 'Body'] and controller.controllers == ['EngineEcu']
    assert {'label': 'Static slots', 'value': '50'} in controller.parameters
    combined = [row for row in controller.pdus if row['frame'] == 'FrCombined']
    assert [(row['pdu'], row['offset'], row['update_bit']) for row in combined] == [
        ('EnginePdu', 0, '15.0'),
        ('WheelPdu', 8, '15.1'),
    ]
    assert controller.choose_cluster('Body') == '' and controller.cluster == 'Body'
    assert [d['name'] for d in controller.definitions] == ['FrStatus'] and controller.parameters == []
    assert 'no FlexRay cluster' in controller.choose_cluster('Nowhere')


def test_simulation_analysis_sending_and_plotting(controller, qtbot):
    assert controller.load_database(QUrl.fromLocalFile(str(DATA / 'flexray_pdus.arxml'))) == ''
    assert 'simulation' in controller.transmit_raw(70, 0, 1, 'A', '0102')  # Nothing to send with yet
    assert controller.start_simulation(0) == '' and controller.can_transmit
    key = controller.plot_signal('FrEngine', 'EngineSpeed')
    assert key == 'FrEngine.EngineSpeed' and controller.tracked_signals == [key]
    assert controller.plot_view.charts == [[key]]
    engine = next(i for i, d in enumerate(controller.definitions) if d['name'] == 'FrEngine')
    assert controller.transmit_definition(engine, {'EngineSpeed': '1000'}, '') == ''
    assert controller.transmit_raw(70, 1, 2, 'A', '01 02') == ''
    assert 'even number' in controller.transmit_raw(71, 0, 1, 'A', '01')
    assert 'Not hex' in controller.transmit_raw(71, 0, 1, 'A', 'xyz')
    assert [(row['slot'], row['channels'], row['data']) for row in controller.transmissions] == [
        (12, 'A+B', 'A0 0F 00 00 0C 80 00 00'),  # OilPressure 0 kPa: raw 200 (offset -100)
        (70, 'A', '01 02'),
    ]
    qtbot.waitUntil(lambda: controller.statistics['frames'] >= 60, timeout=3000)
    controller.stop()
    sent = [row for row in controller.frames if row['slot'] == 12]
    assert sent and all(row['flags'].endswith('Tx') and 'EngineSpeed=1000' in row['info'] for row in sent)
    assert any(row['slot'] == 70 for row in controller.frames)
    series = controller.plot_view.tracker.series[key]
    assert series.values and series.values[-1] == 1000
    controller._refresh_analysis(force=True)
    analysis = controller.analysis
    assert analysis['described'] and analysis['timed'] and {c['channel'] for c in analysis['channels']} == {'A', 'B'}
    assert any(issue['slot'] == 70 and issue['kind'] == 'not described' for issue in analysis['issues'])
    assert 'compared' in analysis['comparison']
    assert controller.transmissions == []  # Stopping the source stops sending
    controller.untrack_key(key)
    assert controller.tracked_signals == [] and controller.plot_view.charts == []


def test_blf_and_asc_logs_open(controller, tmp_path):
    from tests.unit.core.test_flexray_logs import ASC, _container, _object, _receive_ex, _status, blf_file

    blf = tmp_path / 'drive.blf'
    blf.write_bytes(
        blf_file([_container(_object(66, _receive_ex(12, 0, bytes(8)), 1000) + _object(48, _status(1), 2000), True)])
    )
    assert controller.open_capture(QUrl.fromLocalFile(str(blf))) == ''
    assert controller.source_name == 'drive.blf (BLF)' and controller.statistics['frames'] == 1
    assert [(row['kind'], row['text']) for row in controller.events] == [('symbol', 'CAS')]
    asc = tmp_path / 'drive.asc'
    asc.write_text(
        ASC.format(base='dec', slot=26, flags=2, state=32, crc=5, data=' '.join(['0'] * 8), two='1 2', four='1 2 3 4',
                   flag=0),
        encoding='utf-8',
    )  # fmt: skip
    assert controller.open_capture(QUrl.fromLocalFile(str(asc))) == ''
    assert controller.source_name == 'drive.asc (ASC)' and controller.statistics['frames'] == 5
    assert controller.analysis['channels'] and not controller.analysis['described']


def test_interfaces_through_the_controller(qapp, qtbot, tmp_path):
    from tests.unit.core.test_flexray import FakeXl
    from tests.unit.core.test_flexray_interfaces import FakeXlNode, _fake_icsneopy

    from can_explorer.core.flexray_intrepid import IntrepidFlexRay

    library = FakeXlNode()
    module, device = _fake_icsneopy()

    def vector(mode='spy'):
        return flexray_vector.VectorFlexRayNode(library) if mode == 'node' else flexray_vector.VectorFlexRaySpy(library)

    controller = FlexRayController(vector_factory=vector, intrepid_factory=lambda: IntrepidFlexRay(module))
    try:
        assert controller.refresh_interfaces() == ''
        assert [(c['name'], c['kind']) for c in controller.interfaces] == [
            ('VN7610 FlexRay', 'vector'),
            ('neoVI FIRE 3 FlexRay FD0001 FlexRay 1', 'intrepid'),
            ('neoVI FIRE 3 FlexRay FD0001 FlexRay 2', 'intrepid'),
        ]
        # Intrepid, watching with the device's configuration
        assert controller.start_interface(1, 'watch', '', False, QUrl()) == ''
        assert (controller.source, controller.mode, controller.can_transmit) == ('intrepid', 'listen', False)
        device.messages = [module.FlexRayMessage(slotid=5, cycle=1, data=(1, 2), timestamp=10)]
        qtbot.waitUntil(lambda: controller.statistics['frames'] == 1, timeout=2000)
        # Taking part needs the description's parameters
        assert 'Taking part in the cluster needs' in controller.start_interface(2, 'node', '', False, QUrl())
        assert controller.load_database(QUrl.fromLocalFile(str(DATA / 'flexray_pdus.arxml'))) == ''
        error = controller.start_interface(2, 'node', 'EngineEcu', False, QUrl())
        assert 'does not give these FlexRay parameters' in error and 'LISTEN-TIMEOUT' in error
        # Vector, taking part with the channel's configuration, as the coldstart node of key slot 12
        assert controller.start_interface(0, 'node', '', True, QUrl()) == ''
        assert controller.mode == 'node' and controller.can_transmit
        assert ('startup', 12, flexray_vector.FLAG_STARTUP | flexray_vector.FLAG_SYNC, 3) in library.calls
        assert controller.transmit_raw(40, 1, 4, 'B', '0a0b') == ''
        assert library.transmitted[-1][1:5] == (40, 1, 4, 1)
        assert controller.stop_transmit(40) == '' and controller.transmissions == []
        config = tmp_path / 'xl.json'
        config.write_text('{"gMacroPerCycle": 3636}')
        assert controller.start_interface(0, 'node', '', False, QUrl.fromLocalFile(str(config))) == ''
        assert ('configure', 3636, 0) in library.calls
        controller.stop()
        assert 'Choose an interface' in controller.start_interface(9, 'watch', '', False, QUrl())
    finally:
        controller.shutdown()
    assert isinstance(FakeXl(), FakeXl)


def test_view_renders_without_warnings(qapp, qtbot):
    from can_explorer.gui.connection_manager import ConnectionManager
    from can_explorer.gui.signal_plot import register_qml_types

    register_qml_types()

    os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'CanExplorerStyle')
    manager = ConnectionManager()
    engine = QQmlEngine()
    engine.addImportPath(str(get_res_path('.')))
    warnings = []
    engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
    engine.rootContext().setContextProperty('connectionManager', manager)
    try:
        assert manager.flexray.load_database(QUrl.fromLocalFile(str(DATA / 'flexray_pdus.arxml'))) == ''
        component = QQmlComponent(engine)
        component.setData(
            b'import CanExplorer 1.0\nFlexRayView { width: 1200; height: 800 }', QUrl.fromLocalFile('/v.qml')
        )
        view = component.create()
        assert view is not None, component.errorString()
        assert manager.flexray.start_simulation(2) == ''
        manager.flexray.plot_signal('FrEngine', 'EngineSpeed')
        assert manager.flexray.transmit_raw(70, 0, 1, 'A', '0102') == ''
        qtbot.waitUntil(lambda: manager.flexray.statistics['frames'] > 20, timeout=3000)
        tabs = view.findChild(QObject, 'flexrayTabs')
        for index in range(7):  # Every tab, with frames, analysis, a plot and a transmission
            tabs.setProperty('currentIndex', index)
            qtbot.wait(60)
        manager.flexray.stop()
        assert warnings == []
        view.deleteLater()
    finally:
        manager.close_all()
