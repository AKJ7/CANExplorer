import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Shown when no connection is open.
ScrollView {
    id: root
    contentWidth: availableWidth
    clip: true

    component ActionCard: Card {
        id: card
        property string icon: ""
        property string heading: ""
        property string description: ""
        property color accentColor: Theme.accent
        signal activated()

        Layout.fillWidth: true
        Layout.preferredHeight: 170
        color: area.containsMouse ? Qt.lighter(Theme.card, Theme.dark ? 1.12 : 0.98) : Theme.card

        ColumnLayout {
            anchors.fill: parent
            spacing: 8
            Rectangle {
                width: 44
                height: 44
                radius: 22
                color: card.accentColor
                Label {
                    anchors.centerIn: parent
                    text: card.icon
                    color: "white"
                    font.pixelSize: 20
                    font.bold: true
                }
            }
            Label { text: card.heading; color: Theme.text; font.pixelSize: Theme.titleSize; font.bold: true }
            Label {
                text: card.description
                color: Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Item { Layout.fillHeight: true }
        }
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: card.activated()
        }
    }

    ColumnLayout {
        width: root.availableWidth
        spacing: Theme.spacing

        RowLayout {
            spacing: 20
            Layout.topMargin: 12
            Logo {
                Layout.preferredWidth: 88
                Layout.preferredHeight: 88
            }
            ColumnLayout {
                spacing: 6
                Label {
                    text: qsTr("Welcome to %1").arg(metadata.name)
                    color: Theme.text
                    font.pixelSize: 28
                    font.bold: true
                }
                Label {
                    text: qsTr("Monitor, analyse, diagnose and simulate CAN, CAN FD and LIN buses.")
                    color: Theme.muted
                    font.pixelSize: 15
                }
            }
        }

        RowLayout {
            spacing: Theme.spacing
            Layout.fillWidth: true
            Layout.topMargin: 12
            ActionCard {
                icon: "+"
                heading: qsTr("New connection")
                description: qsTr("Connect to a CAN, CAN FD or LIN interface, or to a virtual bus.")
                accentColor: Theme.chart[0]
                onActivated: Actions.newConnection.trigger()
            }
            ActionCard {
                icon: "⟲"
                heading: qsTr("Replay a log file")
                description: qsTr("Open an ASC, BLF, CSV, LOG, TRC or SQLite recording.")
                accentColor: Theme.chart[1]
                onActivated: Actions.openLogFile.trigger()
            }
            ActionCard {
                icon: "⇆"
                heading: qsTr("Convert a database")
                description: qsTr("Convert between DBC, DBF, KCD and SYM, or from ARXML.")
                accentColor: Theme.chart[2]
                onActivated: Actions.convertDatabase.trigger()
            }
            ActionCard {
                icon: "▣"
                heading: qsTr("Open a workspace")
                description: qsTr("Connections, databases, filters, plots and scripts saved together.")
                accentColor: Theme.chart[5]
                onActivated: Actions.openWorkspace.trigger()
            }
            ActionCard {
                icon: "✎"
                heading: qsTr("Edit a database")
                description: qsTr("Edit the messages, signals and nodes of DBC, DBF, KCD and SYM files.")
                accentColor: Theme.chart[4]
                onActivated: Actions.editDatabase.trigger()
            }
        }

        Card {
            title: qsTr("Recent workspaces")
            visible: connectionManager.app_state.recent_workspaces.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: 72 + connectionManager.app_state.recent_workspaces.length * 46

            ColumnLayout {
                anchors.fill: parent
                spacing: 6
                Repeater {
                    model: connectionManager.app_state.recent_workspaces
                    ItemDelegate {
                        required property var modelData
                        required property int index
                        Layout.fillWidth: true
                        implicitHeight: 40
                        onClicked: Actions.openRecentWorkspace(index)
                        background: Rectangle {
                            radius: Theme.smallRadius
                            color: parent.hovered ? Theme.alpha(Theme.accent, 0.14) : Theme.cardAlt
                        }
                        contentItem: RowLayout {
                            spacing: 12
                            Label { text: modelData.name; color: Theme.text; font.bold: true; Layout.leftMargin: 4 }
                            Label {
                                text: modelData.path
                                color: Theme.muted
                                elide: Text.ElideMiddle
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }
        }

        Card {
            title: qsTr("Recent connections")
            visible: connectionManager.app_state.recent_connections.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: 72 + connectionManager.app_state.recent_connections.length * 46

            ColumnLayout {
                anchors.fill: parent
                spacing: 6
                Repeater {
                    model: connectionManager.app_state.recent_connections
                    ItemDelegate {
                        required property var modelData
                        required property int index
                        Layout.fillWidth: true
                        implicitHeight: 40
                        onClicked: connectionManager.open_recent_connection(index)
                        background: Rectangle {
                            radius: Theme.smallRadius
                            color: parent.hovered ? Theme.alpha(Theme.accent, 0.14) : Theme.cardAlt
                        }
                        contentItem: RowLayout {
                            spacing: 12
                            Label { text: modelData.name; color: Theme.text; font.bold: true; Layout.leftMargin: 4 }
                            Label { text: `${modelData.interface}:${modelData.channel}`; color: Theme.muted }
                            Item { Layout.fillWidth: true }
                            Pill { text: modelData.bus || "CAN"; tint: Theme.accent }
                        }
                    }
                }
            }
        }
    }
}
