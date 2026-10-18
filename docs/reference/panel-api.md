# QML panel API

A panel is a QML file whose root object declares `property var panel`. CANExplorer sets it to the panel bridge of
the connection when the panel is created:

```qml
import QtQuick 2.15

Item {
    property var panel
}
```

## The `panel` object

| Member | Returns | Description |
|---|---|---|
| `signal("Message.Signal")` | Watch | Live value of a signal of the loaded database |
| `variable(name)` | Watch | Live value of a shared variable |
| `set_variable(name, value)` | – | Set a shared variable; `@on_variable` handlers of the nodes are called when it changes |
| `send(message, {signal: value})` | error text or `""` | Encode and send a database message; signals not given keep their initial value |
| `send_raw("123", "01 02")` | error text or `""` | Send raw hex data; identifiers above 7FF are sent as extended |
| `log(text)` | – | Write to the simulation console |

Errors of `send` and `send_raw` are also written to the console.

## Watches

`signal()` and `variable()` return a watch object; bind to its properties:

| Property | Type | Description |
|---|---|---|
| `value` | var | Current value: a number, the text of a value table entry, or `undefined` before the first value |
| `number` | real | The value as a number (0 when unknown or not numeric), for gauges, sliders and progress bars |
| `text` | string | The value with its unit, `–` when unknown |
| `unit` | string | Unit of the signal |
| `key` | string | The signal key or variable name |

Watches are updated at most every 50 ms. The same watch object is returned for the same key, so it is fine to call
`panel.signal(...)` in bindings; storing it in a `readonly property var` keeps the panel readable.

## Example

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ColumnLayout {
    id: root
    property var panel
    readonly property var speed: panel.signal("EngineData.EngineSpeed")
    readonly property var ignition: panel.variable("ignition")

    Switch {
        text: "Ignition"
        checked: root.ignition.value === true          // Follows changes made elsewhere
        onToggled: root.panel.set_variable("ignition", checked)
    }
    Label { text: root.speed.text; font.pixelSize: 24 }
    ProgressBar { from: 0; to: 4000; value: root.speed.number }
    Button {
        text: "OBD speed request"
        onClicked: root.panel.send_raw("7DF", "02 01 0D 00 00 00 00 00")
    }
}
```

`examples/simulation/engine_panel.qml` is a complete panel.
