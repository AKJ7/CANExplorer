"""Example test script for the engine and gearbox of examples/simulation.

In the application: connect to a virtual bus, load examples/simulation/vehicle.dbc as database and
engine_simulation.py in the Simulation view and start it, then load this script in the Tests view and run it.

On the command line (the simulation runs alongside):

    uv run can-test examples/testing/vehicle_tests.py --database examples/simulation/vehicle.dbc \\
        --simulation examples/simulation/engine_simulation.py --report report.html
"""

from can_explorer.core.uds import UdsError
from can_explorer.testing import TestModule, testcase


class EngineTests(TestModule):
    """Engine control unit: cyclic status and speed control."""

    async def setup(self, t):
        t.variables['ignition'] = False
        t.variables['target_speed'] = 800
        await t.expect_signal('EngineData.EngineSpeed', 0, timeout=10, description='engine stopped')

    async def teardown(self, t):
        t.variables['ignition'] = False

    @testcase
    async def engine_data_is_sent_every_100_ms(self, t):
        """EngineData is sent cyclically, also with the ignition off."""
        await t.expect_cycle_time('EngineData', 100, tolerance=0.1, duration=1.0)

    @testcase
    async def engine_is_off_without_ignition(self, t):
        msg = await t.expect_message('EngineData')
        t.check_equal(msg.signals['EngineSpeed'], 0, 'engine speed')

    @testcase(timeout=10)
    async def engine_idles_after_ignition_on(self, t):
        t.variables['ignition'] = True
        speed = await t.expect_signal('EngineData.EngineSpeed', lambda rpm: rpm >= 790, timeout=3)
        t.check_range(speed, 790, 850, 'idle speed')
        temperature = (await t.expect_message('EngineData')).signals['CoolantTemp']
        t.check_range(temperature, -40, 120, 'coolant temperature')

    @testcase(timeout=10)
    async def speed_follows_the_target(self, t):
        t.variables['ignition'] = True
        t.variables['target_speed'] = 3000
        speed = await t.expect_signal('EngineData.EngineSpeed', lambda rpm: rpm > 2900, timeout=4)
        t.log('engine speed reached', speed, 'rpm')
        await t.expect_signal('EngineData.GearPosition', 'Second', timeout=1)
        t.variables['target_speed'] = 800


class GearboxTests(TestModule):
    """Gearbox: gear selection and vehicle speed."""

    @testcase(timeout=10)
    async def vehicle_speed_matches_the_gear(self, t):
        t.variables['ignition'] = True
        t.variables['target_speed'] = 2000
        await t.expect_signal('EngineData.GearPosition', 'First', timeout=4)
        engine = await t.expect_message('EngineData')
        wheel = await t.expect_signal('VehicleSpeed.WheelSpeed', timeout=1)
        t.check_equal(wheel, engine.signals['EngineSpeed'] * 0.008, 'wheel speed in first gear', tolerance=1.5)
        t.variables['ignition'] = False

    @testcase(title='OBD vehicle speed request')
    async def obd_vehicle_speed_request(self, t):
        """OBD-II service 01, PID 0D: vehicle speed."""
        t.send(0x7DF, [0x02, 0x01, 0x0D, 0, 0, 0, 0, 0])
        response = await t.expect_message(0x7E8, timeout=0.5)
        t.require(response is not None, 'response received')
        t.check_equal(response.data[1:3], bytes([0x41, 0x0D]), 'positive response to 01 0D')


class DiagnosticsTests(TestModule):
    """UDS diagnostics of the engine ECU (skipped when no ECU answers on 7E0/7E8)."""

    uds_request_id = 0x7E0
    uds_response_id = 0x7E8

    @testcase
    async def read_vin(self, t):
        try:
            vin = await t.uds.read_data_by_identifier(0xF190)
        except UdsError as e:
            t.skip(f'no diagnostic response: {e}')
        t.check_equal(len(vin), 17, 'VIN length')
