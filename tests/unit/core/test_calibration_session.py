import uuid
from pathlib import Path

from can_explorer.core.calibration_session import CalibrationSession
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.xcp_slave import DEFAULT_STATIC_LISTS, XcpSlaveSimulator, example_memory

A2L = Path(__file__).resolve().parents[2] / 'data' / 'example.a2l'


async def test_measure_uses_static_daq_lists():
    channel = f'calibration-session-{uuid.uuid4().hex}'
    master = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    master.start()
    ecu.start()
    simulator = XcpSlaveSimulator(ecu, memory=example_memory(), static_daq=DEFAULT_STATIC_LISTS)
    calibration = CalibrationSession()
    try:
        calibration.load(str(A2L))
        assert (await calibration.connect(master))['connected']
        measured = await calibration.measure(['EngineSpeed', 'CoolantTemp'], duration=0.3)
        assert measured['method'] == "DAQ (event 0, the ECU's static DAQ lists)"
        assert measured['values']['EngineSpeed']['last'] == 1500.0 and measured['values']['CoolantTemp']['samples'] > 3
    finally:
        await calibration.disconnect()
        simulator.stop()
        master.stop()
        ecu.stop()
