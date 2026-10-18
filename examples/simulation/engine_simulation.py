"""Example simulation: an engine and a gearbox on the bus described by vehicle.dbc.

Load vehicle.dbc as signal database, this script in the Simulation tab, and engine_panel.qml as panel.
The panel sets the shared variables 'ignition' and 'target_speed'; the nodes react to them.
"""

from can_explorer.scripting import Node, on_message, on_signal, on_start, on_stop, on_variable

IDLE_SPEED = 800


class Engine(Node):
    """Sends EngineData every 100 ms. The speed follows the target speed while the ignition is on."""

    @on_start
    def start(self):
        self.status = self.message('EngineData')
        self.speed = 0.0
        self.temperature = 20.0
        self.every(0.1, self.update)

    def update(self):
        running = bool(self.variables.get('ignition', False))
        target = float(self.variables.get('target_speed', IDLE_SPEED)) if running else 0.0
        if running:
            target = max(target, IDLE_SPEED)
        self.speed += (target - self.speed) * 0.2  # Follow the target smoothly
        if running:
            self.temperature = min(90.0, self.temperature + 0.2)
        self.status.EngineSpeed = round(self.speed)
        self.status.CoolantTemp = round(self.temperature)
        self.status.GearPosition = self.variables.get('gear', 'Neutral')  # Set by the Gearbox node
        self.send(self.status)

    @on_variable('ignition')
    def ignition(self, on):
        self.log('ignition', 'on' if on else 'off')

    @on_stop
    def stop(self):
        self.log('engine stopped at', round(self.speed), 'rpm')


class Gearbox(Node):
    """Chooses the gear from the engine speed and reports the vehicle speed."""

    RATIOS = {'Neutral': 0.0, 'First': 0.008, 'Second': 0.015}

    @on_start
    def start(self):
        self.gear = 'Neutral'
        self.vehicle_speed = self.message('VehicleSpeed')

    @on_signal('EngineData', 'EngineSpeed')
    def engine_speed(self, rpm, msg):
        gear = 'Neutral' if rpm < 1000 else 'First' if rpm < 2500 else 'Second'
        if gear != self.gear:
            self.gear = gear
            self.log('shifted to', gear)
        self.vehicle_speed.WheelSpeed = round(rpm * self.RATIOS[gear], 2)
        self.send(self.vehicle_speed)
        self.variables['gear'] = gear

    @on_message(0x7DF)
    def obd_request(self, msg):
        """Answers the OBD-II vehicle speed request (service 01, PID 0D)."""
        if msg.data[:3] == bytes([0x02, 0x01, 0x0D]):
            self.send(0x7E8, [0x03, 0x41, 0x0D, int(self.vehicle_speed.WheelSpeed) & 0xFF, 0, 0, 0, 0])
