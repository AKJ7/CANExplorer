# Node simulation and scripting

CANExplorer can simulate bus nodes with Python scripts and control them from QML panels, similar to
CANoe's CAPL nodes and panels. Everything runs in the **Simulation** tab of a connection:

1. Connect (any interface, e.g. the `virtual` one to work without hardware).
2. Optionally load a signal database (DBC, DBF, ARXML, KCD, SYM or LDF) to send and receive by name.
3. **Load script…** one or more Python files and **Start simulation**.
4. **Add panel…** QML files to control and watch the simulation.

`examples/simulation` contains a complete example (`vehicle.dbc`, `engine_simulation.py`, `engine_panel.qml`).

Scripts and panels are code: only load files you trust.

## Python scripts

A script defines one or more `Node` subclasses. Each class becomes one simulated node:

```python
from can_explorer.scripting import Node, on_message, on_signal, on_start, on_stop, on_variable


class Engine(Node):
    name = 'Engine'  # Optional, defaults to the class name

    @on_start
    def start(self):
        self.status = self.message('EngineData')  # Buffer with the signals of a database message
        self.every(0.1, self.send_status)         # Timer, stopped with the simulation

    def send_status(self):
        self.status.EngineSpeed = self.variables.get('target_speed', 800)
        self.send(self.status)

    @on_message('EngineControl', 0x7DF)          # Database names and/or identifiers
    def received(self, msg):
        self.log(msg.name, msg.signals)

    @on_signal('VehicleSpeed', 'WheelSpeed')      # When the decoded value changes
    def speed(self, value, msg):
        self.variables['speed'] = value

    @on_variable('ignition')                      # When a shared variable changes
    def ignition(self, on):
        self.log('ignition', on)

    @on_stop
    def stop(self):
        self.log('stopped')
```

### Handlers

| Decorator | Called with | When |
|---|---|---|
| `@on_start` | – | The simulation starts (or the script is loaded while it runs) |
| `@on_stop` | – | The simulation stops |
| `@on_message(*selectors)` | `ReceivedMessage` | A frame with one of the identifiers (`int`) or database names (`str`) is on the bus; no selectors: every frame |
| `@on_signal(message, signal)` | `value, ReceivedMessage` | A signal is first received or its value changes |
| `@on_variable(name)` | `value` | A shared variable changes |

Nodes receive the frames of other nodes and of the bus, but not their own.
Exceptions raised in handlers and timers are shown in the console; the simulation keeps running.

### Node API

| | |
|---|---|
| `self.send(buffer)` | Send a `MessageBuffer` |
| `self.send('EngineData', EngineSpeed=1000)` | Encode and send a database message; other signals use their initial value |
| `self.send(0x123, b'\x01\x02')`, `self.send(0x123, [1, 2])` | Send raw data. `extended=` defaults to identifiers above 0x7FF; `fd=True`, `remote=True` |
| `self.message(name)` | `MessageBuffer` of a database message: `buffer.Signal`, `buffer['Signal']`, `buffer.update(Signal=1)` |
| `self.every(period, callback, *args)` | Periodic timer (seconds), returns a `Timer` with `cancel()` |
| `self.after(delay, callback, *args)` | One-shot timer |
| `self.variables` | Shared variables: `get(name, default)`, `[name]`, `set(name, value)` |
| `self.log(*values)` | Write to the console |
| `self.time` | Seconds since the simulation started |

`ReceivedMessage` has `id`, `data`, `dlc`, `is_extended`, `is_fd`, `is_remote`, `timestamp`, `name` (database
name or `None`), `signals` (physical values as numbers) and `labels` (with value table texts), and `raw` (the
python-can message).

## QML panels

A panel is a QML file whose root object declares `property var panel`:

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15

Column {
    property var panel
    readonly property var speed: panel.signal("EngineData.EngineSpeed")

    Label { text: speed.text }                                  // "1000 rpm"
    ProgressBar { from: 0; to: 4000; value: speed.number }
    Switch {
        checked: panel.variable("ignition").value === true
        onToggled: panel.set_variable("ignition", checked)
    }
    Button { text: "Request"; onClicked: panel.send("EngineControl", { TargetSpeed: 2000 }) }
}
```

| | |
|---|---|
| `panel.signal("Message.Signal")` | Live value of a database signal: `.value`, `.number`, `.text` (with unit), `.unit` |
| `panel.variable(name)` | Live value of a shared variable (same properties) |
| `panel.set_variable(name, value)` | Set a shared variable (triggers `@on_variable` handlers) |
| `panel.send(message, {signal: value})` | Send a database message; returns an error text or `""` |
| `panel.send_raw("123", "01 02")` | Send raw hex data; returns an error text or `""` |
| `panel.log(text)` | Write to the console |

Values are refreshed every 50 ms. Panels can use all QtQuick modules. **Reload panels** reloads them from disk
after editing; errors in a panel are shown in the console.

## Packaged builds

In the standalone bundles, scripts can import CANExplorer, python-can, cantools and the standard library modules
the application itself uses. Other modules are not available there; run CANExplorer from source
(`uv run main`) for scripts that need additional packages.
