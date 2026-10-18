import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Bus health: error state, error frames, error counters and the load over up to an hour.
ScrollView {
    id: root
    required property var connection
    readonly property var health: connection.bus_health
    contentWidth: availableWidth
    clip: true

    function stateColor(state) {
        return state === "active" ? Theme.success : state === "warning" ? Theme.warning
             : state === "passive" ? Theme.chart[4] : state === "bus-off" ? Theme.danger : Theme.muted
    }

    function stateText(state) {
        return state === "active" ? qsTr("Error active") : state === "warning" ? qsTr("Error warning")
             : state === "passive" ? qsTr("Error passive") : state === "bus-off" ? qsTr("Bus off") : qsTr("Unknown")
    }

    ColumnLayout {
        width: root.availableWidth
        spacing: Theme.spacing

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Label { text: qsTr("Last"); color: Theme.muted }
            Repeater {
                model: root.health.windows
                Button {
                    required property var modelData
                    text: modelData.name
                    checkable: true
                    checked: root.health.window === modelData.value
                    onClicked: root.health.set_window(modelData.value)
                }
            }
            Item { Layout.fillWidth: true }
            Button {
                text: qsTr("Reset")
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Forget the history and the counts")
                onClicked: root.health.clear()
            }
        }

        RowLayout {
            spacing: Theme.spacing
            Layout.fillWidth: true

            StatCard {
                icon: "♥"
                value: root.stateText(root.health.state)
                label: qsTr("Error state")
                trend: root.health.state === "" ? qsTr("not reported yet") : ""
                accentColor: root.stateColor(root.health.state)
            }
            StatCard {
                icon: "!"
                value: root.health.error_frames.toLocaleString(Qt.locale(), "f", 0)
                label: qsTr("Error frames")
                trend: root.health.peak_errors > 0 ? qsTr("peak %1/s in the last %2").arg(root.health.peak_errors).arg(root.health.window_text) : ""
                trendPositive: false
                accentColor: Theme.danger
                history: root.health.errors
            }
            StatCard {
                icon: "#"
                value: root.health.has_counters ? root.health.counters_text : "–"
                label: qsTr("Error counters")
                trend: root.health.has_counters ? "" : qsTr("not reported by the interface")
                trendPositive: false
                accentColor: Theme.chart[4]
            }
            StatCard {
                icon: "⏻"
                value: `${root.health.bus_off_count} / ${root.health.passive_count}`
                label: qsTr("Bus off / error passive")
                trend: root.health.bus_off_count > 0 ? qsTr("bus off occurred") : ""
                trendPositive: false
                accentColor: Theme.chart[0]
            }
        }

        Card {
            title: qsTr("Error state over the last %1").arg(root.health.window_text)
            Layout.fillWidth: true
            Layout.preferredHeight: 104

            ColumnLayout {
                anchors.fill: parent
                spacing: 6
                Rectangle {
                    id: timelineBar
                    Layout.fillWidth: true
                    Layout.preferredHeight: 22
                    radius: 4
                    color: Theme.cardAlt
                    clip: true
                    Repeater {
                        model: root.health.timeline
                        Rectangle {
                            required property var modelData
                            x: modelData.start * timelineBar.width
                            width: Math.max(2, (modelData.end - modelData.start) * timelineBar.width)
                            height: timelineBar.height
                            color: root.stateColor(modelData.state)
                            ToolTip.visible: segmentArea.containsMouse
                            ToolTip.text: root.stateText(modelData.state)
                            MouseArea {
                                id: segmentArea
                                anchors.fill: parent
                                hoverEnabled: true
                            }
                        }
                    }
                }
                RowLayout {
                    spacing: 14
                    Repeater {
                        model: ["active", "warning", "passive", "bus-off", "unknown"]
                        RowLayout {
                            required property string modelData
                            spacing: 4
                            Rectangle { width: 10; height: 10; radius: 2; color: root.stateColor(modelData) }
                            Label { text: root.stateText(modelData); color: Theme.muted; font.pixelSize: 12 }
                        }
                    }
                }
            }
        }

        RowLayout {
            spacing: Theme.spacing
            Layout.fillWidth: true
            Layout.preferredHeight: 260

            Card {
                title: root.health.peak_load >= 0 ? qsTr("Bus load [%] (peak %1%)").arg(root.health.peak_load.toFixed(1))
                                                 : qsTr("Frames per second (bitrate unknown)")
                Layout.fillWidth: true
                Layout.fillHeight: true
                AreaChart {
                    anchors.fill: parent
                    values: root.health.peak_load >= 0 ? root.health.load : root.health.frame_rate
                    capacity: root.health.points
                    startLabel: "-" + root.health.window_text
                    color: Theme.chart[0]
                }
            }
            Card {
                title: qsTr("Error frames per %1").arg(root.health.bucket > 1 ? qsTr("%1 s (peak)").arg(root.health.bucket) : qsTr("second"))
                Layout.fillWidth: true
                Layout.fillHeight: true
                AreaChart {
                    anchors.fill: parent
                    values: root.health.errors
                    capacity: root.health.points
                    startLabel: "-" + root.health.window_text
                    color: Theme.danger
                }
            }
        }

        RowLayout {
            visible: root.health.has_counters
            spacing: Theme.spacing
            Layout.fillWidth: true
            Layout.preferredHeight: 220

            Card {
                title: qsTr("Transmit error counter (TEC)")
                Layout.fillWidth: true
                Layout.fillHeight: true
                AreaChart {
                    anchors.fill: parent
                    values: root.health.tec
                    capacity: root.health.points
                    startLabel: "-" + root.health.window_text
                    color: Theme.chart[4]
                }
            }
            Card {
                title: qsTr("Receive error counter (REC)")
                Layout.fillWidth: true
                Layout.fillHeight: true
                AreaChart {
                    anchors.fill: parent
                    values: root.health.rec
                    capacity: root.health.points
                    startLabel: "-" + root.health.window_text
                    color: Theme.chart[5]
                }
            }
        }

        Card {
            title: qsTr("Error events")
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(320, 60 + eventList.count * 26)

            ListView {
                id: eventList
                anchors.fill: parent
                clip: true
                model: root.health.events
                ScrollBar.vertical: ScrollBar {}
                delegate: RowLayout {
                    required property var modelData
                    width: eventList.width - 12
                    height: 26
                    spacing: 10
                    Label { text: modelData.time; font.family: "monospace"; color: Theme.muted }
                    Rectangle {
                        width: 8
                        height: 8
                        radius: 4
                        color: root.stateColor(modelData.state)
                        visible: modelData.state.length > 0
                    }
                    Label { text: modelData.text; elide: Text.ElideRight; Layout.fillWidth: true }
                }
            }
            Label {
                anchors.centerIn: parent
                visible: eventList.count === 0
                text: qsTr("No error frames received")
                color: Theme.muted
            }
        }
    }
}
