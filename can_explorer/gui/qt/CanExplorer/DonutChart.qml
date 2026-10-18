import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Donut with a legend; `segments` is a list of {label, value}.
RowLayout {
    id: root
    property var segments: []
    property string centerText: total.toLocaleString(Qt.locale(), "f", 0)
    property string centerLabel: qsTr("Total")
    readonly property real total: segments.reduce((sum, segment) => sum + segment.value, 0)

    spacing: 24

    Canvas {
        id: canvas
        Layout.fillHeight: true
        Layout.preferredWidth: height
        Layout.minimumWidth: 120

        Connections {
            target: root
            function onSegmentsChanged() { canvas.requestPaint() }
        }
        Connections {
            target: Theme
            function onNameChanged() { canvas.requestPaint() }
        }
        onWidthChanged: requestPaint()

        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const radius = Math.min(width, height) / 2 - 2, thickness = radius * 0.36
            const cx = width / 2, cy = height / 2
            if (root.total <= 0) {
                ctx.strokeStyle = Theme.cardAlt
                ctx.lineWidth = thickness
                ctx.beginPath(); ctx.arc(cx, cy, radius - thickness / 2, 0, 2 * Math.PI); ctx.stroke()
                return
            }
            let angle = -Math.PI / 2
            root.segments.forEach((segment, index) => {
                const sweep = segment.value / root.total * 2 * Math.PI
                ctx.strokeStyle = Theme.chart[index % Theme.chart.length]
                ctx.lineWidth = thickness
                ctx.beginPath()
                ctx.arc(cx, cy, radius - thickness / 2, angle + 0.02, angle + sweep - 0.02)
                ctx.stroke()
                angle += sweep
            })
        }

        Column {
            anchors.centerIn: parent
            Label {
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.centerText
                color: Theme.text
                font.pixelSize: 22
                font.bold: true
            }
            Label {
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.centerLabel
                color: Theme.muted
                font.pixelSize: 12
            }
        }
    }

    ColumnLayout {
        spacing: 10
        Layout.alignment: Qt.AlignVCenter
        Repeater {
            model: root.segments
            RowLayout {
                required property var modelData
                required property int index
                spacing: 10
                Rectangle {
                    width: 14
                    height: 14
                    radius: 4
                    color: Theme.chart[index % Theme.chart.length]
                    Layout.alignment: Qt.AlignTop
                    Layout.topMargin: 3
                }
                ColumnLayout {
                    spacing: 0
                    Label { text: modelData.label; color: Theme.muted }
                    Label {
                        text: (root.total > 0 ? modelData.value / root.total * 100 : 0).toFixed(1) + "%"
                        color: Theme.text
                        font.bold: true
                    }
                }
            }
        }
        Label {
            visible: root.segments.length === 0
            text: qsTr("No frames yet")
            color: Theme.muted
        }
    }
}
