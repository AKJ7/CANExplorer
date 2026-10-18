// Example panel for engine_simulation.py. Panels are QML files declaring `property var panel`:
// panel.signal("Message.Signal") and panel.variable(name) give live values (.value, .number, .text),
// panel.set_variable(name, value), panel.send(message, {signal: value}), panel.send_raw(id, hex) and
// panel.log(text) act on the simulation.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Pane {
    id: root
    property var panel

    readonly property var engineSpeed: panel.signal("EngineData.EngineSpeed")
    readonly property var coolant: panel.signal("EngineData.CoolantTemp")
    readonly property var wheelSpeed: panel.signal("VehicleSpeed.WheelSpeed")
    readonly property var gear: panel.variable("gear")
    readonly property var ignition: panel.variable("ignition")
    readonly property var targetSpeed: panel.variable("target_speed")

    GridLayout {
        columns: 2
        columnSpacing: 24
        rowSpacing: 12

        Switch {
            text: qsTr("Ignition")
            // Follows the variable, which nodes or the Variables list can change as well
            checked: root.ignition.value === true
            onToggled: root.panel.set_variable("ignition", checked)
        }
        Item { width: 1; height: 1 }

        Label { text: qsTr("Target speed: %1 rpm").arg(targetSlider.value.toFixed(0)) }
        Slider {
            id: targetSlider
            from: 800
            to: 4000
            value: root.targetSpeed.value === undefined || root.targetSpeed.value === null ? 800 : root.targetSpeed.number
            Layout.preferredWidth: 260
            onMoved: root.panel.set_variable("target_speed", value)
        }

        Label { text: qsTr("Engine speed") }
        ColumnLayout {
            Label { text: root.engineSpeed.text; font.pixelSize: 22 }
            ProgressBar { from: 0; to: 4000; value: root.engineSpeed.number; Layout.preferredWidth: 260 }
        }

        Label { text: qsTr("Coolant temperature") }
        Label { text: root.coolant.text; font.pixelSize: 18; color: root.coolant.number > 85 ? "tomato" : palette.text }

        Label { text: qsTr("Gear") }
        Label { text: root.gear.text; font.pixelSize: 18 }

        Label { text: qsTr("Vehicle speed") }
        Label { text: root.wheelSpeed.text; font.pixelSize: 18 }

        Button {
            text: qsTr("Request vehicle speed (OBD)")
            onClicked: root.panel.send_raw("7DF", "02 01 0D 00 00 00 00 00")
        }
        Button {
            text: qsTr("Log a note")
            onClicked: root.panel.log("Note from the panel at " + root.engineSpeed.text)
        }
    }
}
