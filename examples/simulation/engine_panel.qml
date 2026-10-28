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

    // A circular speed gauge drawn with Canvas, so the example needs no extra components
    component SpeedGauge: Item {
        id: gauge
        property real value: 0
        property real from: 0
        property real to: 4000
        property real redline: 3500
        property string unit: "rpm"
        implicitWidth: 200
        implicitHeight: 200

        onValueChanged: canvas.requestPaint()
        Behavior on value { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }

        Canvas {
            id: canvas
            anchors.fill: parent
            property color track: "#363b47"
            property color fill: "#5ec8f2"
            property color warn: "#ff5c57"
            property color ink: root.palette.text
            onInkChanged: requestPaint()

            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                var cx = width / 2, cy = height / 2
                var r = Math.min(width, height) / 2 - 16
                var a0 = Math.PI * 0.75, a1 = Math.PI * 2.25, span = a1 - a0
                function angleOf(v) {
                    var fraction = (v - gauge.from) / (gauge.to - gauge.from)
                    return a0 + span * Math.max(0, Math.min(1, fraction))
                }

                ctx.lineCap = "round"
                ctx.lineWidth = 12
                ctx.strokeStyle = track
                ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke()

                var valueAngle = angleOf(gauge.value)
                var redlineAngle = angleOf(gauge.redline)
                ctx.strokeStyle = fill
                ctx.beginPath(); ctx.arc(cx, cy, r, a0, Math.min(valueAngle, redlineAngle)); ctx.stroke()
                if (gauge.value > gauge.redline) {
                    ctx.strokeStyle = warn
                    ctx.beginPath(); ctx.arc(cx, cy, r, redlineAngle, valueAngle); ctx.stroke()
                }

                ctx.strokeStyle = ink
                ctx.fillStyle = ink
                ctx.lineWidth = 2
                ctx.font = "11px sans-serif"
                ctx.textAlign = "center"
                ctx.textBaseline = "middle"
                var majors = Math.max(1, Math.round((gauge.to - gauge.from) / 1000))
                for (var i = 0; i <= majors; i++) {
                    var v = gauge.from + (gauge.to - gauge.from) * i / majors
                    var a = angleOf(v), c = Math.cos(a), s = Math.sin(a)
                    ctx.beginPath()
                    ctx.moveTo(cx + c * (r - 18), cy + s * (r - 18))
                    ctx.lineTo(cx + c * (r - 8), cy + s * (r - 8))
                    ctx.stroke()
                    ctx.fillText((v / 1000).toFixed(0), cx + c * (r - 32), cy + s * (r - 32))
                }

                var needle = gauge.value > gauge.redline ? warn : fill
                ctx.strokeStyle = needle
                ctx.lineWidth = 3
                ctx.beginPath()
                ctx.moveTo(cx - Math.cos(valueAngle) * 14, cy - Math.sin(valueAngle) * 14)
                ctx.lineTo(cx + Math.cos(valueAngle) * (r - 22), cy + Math.sin(valueAngle) * (r - 22))
                ctx.stroke()
                ctx.fillStyle = ink
                ctx.beginPath(); ctx.arc(cx, cy, 5, 0, 2 * Math.PI); ctx.fill()
            }
        }

        ColumnLayout {
            anchors.centerIn: parent
            anchors.verticalCenterOffset: gauge.height * 0.16
            spacing: 0
            Label {
                text: gauge.value.toFixed(0)
                font.pixelSize: 26
                font.bold: true
                Layout.alignment: Qt.AlignHCenter
            }
            Label {
                text: gauge.unit
                opacity: 0.7
                Layout.alignment: Qt.AlignHCenter
            }
        }
    }

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

        Label { text: qsTr("Engine speed"); Layout.alignment: Qt.AlignTop; topPadding: 80 }
        SpeedGauge {
            value: root.engineSpeed.number || 0
            from: 0
            to: 4000
            redline: 3500
            Layout.preferredWidth: 200
            Layout.preferredHeight: 200
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
