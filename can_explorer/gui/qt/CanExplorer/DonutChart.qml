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

    // The arcs to draw, clockwise from the top: [{index, start, end}] in radians. A small gap separates the segments;
    // it shrinks for small segments, which would otherwise end before they start (the canvas then draws the arc the
    // long way round, over the whole donut). Empty segments are left out.
    function arcs() {
        const result = []
        if (total <= 0)
            return result
        let angle = -Math.PI / 2
        segments.forEach((segment, index) => {
            const sweep = Math.max(0, segment.value) / total * 2 * Math.PI
            if (sweep > 0) {
                const gap = Math.min(0.02, sweep / 4)
                result.push({ index: index, start: angle + gap, end: angle + sweep - gap })
            }
            angle += sweep
        })
        return result
    }

    // A legend percentage; shares that round to 0.0 but are not empty show as "<0.1%"
    function percentText(value) {
        const percent = total > 0 ? value / total * 100 : 0
        return value > 0 && percent < 0.05 ? "<0.1%" : percent.toFixed(1) + "%"
    }

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
            ctx.lineWidth = thickness
            root.arcs().forEach(arc => {
                ctx.strokeStyle = Theme.chart[arc.index % Theme.chart.length]
                ctx.beginPath()
                ctx.arc(cx, cy, radius - thickness / 2, arc.start, arc.end, false)
                ctx.stroke()
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
                        text: root.percentText(modelData.value)
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
