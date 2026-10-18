import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Key figures and charts of a connection.
ScrollView {
    id: root
    required property var connection
    contentWidth: availableWidth
    clip: true

    // "↑ +12.5%" comparing the newest value with the one ten seconds earlier
    function trend(history) {
        if (!history || history.length < 11)
            return ""
        const previous = history[history.length - 11], latest = history[history.length - 1]
        if (previous <= 0)
            return ""
        const change = (latest - previous) / previous * 100
        return `${change >= 0 ? "↑ +" : "↓ "}${change.toFixed(1)}%`
    }

    function rate(value) {
        return value >= 100 ? value.toFixed(0) : value.toFixed(1)
    }

    readonly property var activity: connection.rx_history.map((rx, i) => rx + (connection.tx_history[i] || 0))

    ColumnLayout {
        width: root.availableWidth
        spacing: Theme.spacing

        RowLayout {
            spacing: Theme.spacing
            Layout.fillWidth: true

            StatCard {
                icon: "↓"
                value: root.rate(root.connection.rx_rate)
                label: qsTr("Received frames/s")
                trend: root.trend(root.connection.rx_history)
                trendPositive: !trend.startsWith("↓")
                accentColor: Theme.chart[0]
                history: root.connection.rx_history
            }
            StatCard {
                icon: "↑"
                value: root.rate(root.connection.tx_rate)
                label: qsTr("Transmitted frames/s")
                trend: root.trend(root.connection.tx_history)
                trendPositive: !trend.startsWith("↓")
                accentColor: Theme.chart[1]
                history: root.connection.tx_history
            }
            StatCard {
                icon: "%"
                value: root.connection.bus_load >= 0 ? root.connection.bus_load.toFixed(1) + "%" : "–"
                label: qsTr("Bus load")
                trend: root.connection.bus_load >= 0 ? root.trend(root.connection.load_history) : qsTr("bitrate unknown")
                trendPositive: !trend.startsWith("↑")
                accentColor: Theme.chart[2]
                history: root.connection.load_history
            }
            StatCard {
                icon: "!"
                value: root.connection.error_frames.toLocaleString(Qt.locale(), "f", 0)
                label: qsTr("Error frames")
                trend: root.connection.bus_state ? qsTr("bus %1").arg(root.connection.bus_state) : ""
                trendPositive: root.connection.bus_state === "" || root.connection.bus_state === "active"
                accentColor: Theme.danger
            }
        }

        RowLayout {
            spacing: Theme.spacing
            Layout.fillWidth: true
            Layout.preferredHeight: 340

            Card {
                title: qsTr("Bus activity (frames/s)")
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 3
                AreaChart {
                    anchors.fill: parent
                    values: root.activity
                    color: Theme.chart[0]
                }
            }
            Card {
                title: qsTr("Traffic breakdown")
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 2
                DonutChart {
                    anchors.fill: parent
                    segments: root.connection.traffic_breakdown
                    centerText: root.connection.frame_count.toLocaleString(Qt.locale(), "f", 0)
                    centerLabel: qsTr("frames")
                }
            }
        }

        Card {
            title: qsTr("Top identifiers")
            Layout.fillWidth: true
            Layout.preferredHeight: 92 + Math.max(1, topList.count) * 46

            ColumnLayout {
                anchors.fill: parent
                spacing: 6

                Label {
                    visible: topList.count === 0
                    text: root.connection.connected ? qsTr("No frames yet") : qsTr("Connect to see the traffic")
                    color: Theme.muted
                }

                ListView {
                    id: topList
                    interactive: false
                    spacing: 6
                    model: root.connection.top_identifiers
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    delegate: Rectangle {
                        required property var modelData
                        required property int index
                        width: ListView.view.width
                        height: 40
                        radius: Theme.smallRadius
                        color: Theme.cardAlt

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 16
                            spacing: 14
                            Label {
                                text: `#${index + 1}`
                                color: Theme.accent
                                font.bold: true
                                Layout.preferredWidth: 32
                            }
                            Label {
                                text: modelData.id
                                color: Theme.text
                                font.family: "monospace"
                                Layout.preferredWidth: 80
                            }
                            Label {
                                text: modelData.name || "–"
                                color: modelData.name ? Theme.text : Theme.muted
                                elide: Text.ElideRight
                                Layout.preferredWidth: 180
                            }
                            Pill {
                                text: modelData.direction
                                tint: modelData.direction === "Rx" ? Theme.chart[0] : Theme.chart[1]
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                height: 8
                                radius: 4
                                color: Theme.alpha(Theme.border, 0.5)
                                Rectangle {
                                    width: parent.width * modelData.share
                                    height: parent.height
                                    radius: 4
                                    gradient: Gradient {
                                        orientation: Gradient.Horizontal
                                        GradientStop { position: 0.0; color: Theme.chart[0] }
                                        GradientStop { position: 1.0; color: Theme.chart[1] }
                                    }
                                }
                            }
                            Label {
                                text: modelData.cycle
                                color: Theme.muted
                                horizontalAlignment: Text.AlignRight
                                Layout.preferredWidth: 80
                            }
                            Label {
                                text: modelData.count.toLocaleString(Qt.locale(), "f", 0)
                                color: Theme.text
                                horizontalAlignment: Text.AlignRight
                                Layout.preferredWidth: 80
                            }
                        }
                    }
                }
            }
        }
    }
}
