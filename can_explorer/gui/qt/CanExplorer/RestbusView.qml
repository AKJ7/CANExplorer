import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Restbus simulation of one connection: transmit its database messages periodically so the ECU under test sees the
// rest of the bus. Choose which messages to send, exclude the node you are testing, and edit the sent signal values.
ColumnLayout {
    id: root
    required property var connection
    readonly property var rb: connection.restbus

    spacing: 12

    // Controls and configuration
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 12
        spacing: 10
        enabled: root.rb.has_database

        Label { text: qsTr("Default cycle"); color: Theme.muted }
        SpinBox {
            id: cycleBox
            from: 1
            to: 60000
            stepSize: 10
            value: root.rb.default_cycle_ms
            enabled: !root.rb.running
            editable: true
            Layout.preferredWidth: 120
            onValueModified: root.rb.set_default_cycle(value)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Period (ms) for messages without a cycle time in the database")
        }

        ToolSeparator {}

        Button { text: qsTr("All"); enabled: !root.rb.running; onClicked: root.rb.set_all(true) }
        Button { text: qsTr("None"); enabled: !root.rb.running; onClicked: root.rb.set_all(false) }
        Button {
            text: qsTr("Cyclic only")
            enabled: !root.rb.running
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Enable only the messages that have a cycle time")
            onClicked: root.rb.enable_cyclic_only()
        }

        ToolSeparator {}

        Label { text: qsTr("Exclude"); color: Theme.muted }
        ComboBox {
            id: nodeBox
            Layout.preferredWidth: 130
            enabled: !root.rb.running && root.rb.nodes.length > 0
            model: root.rb.nodes
        }
        Button {
            text: qsTr("Exclude node")
            enabled: !root.rb.running && root.rb.nodes.length > 0
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Stop sending the messages of the ECU you are testing")
            onClicked: if (nodeBox.currentText) statusLabel.show(qsTr("%1 message(s) disabled").arg(root.rb.exclude_node(nodeBox.currentText)))
        }

        Item { Layout.fillWidth: true }

        Pill {
            visible: root.rb.has_database
            text: qsTr("%1 / %2 on").arg(root.rb.enabled_count).arg(root.rb.message_count)
            tint: Theme.accent
        }
        Button {
            text: root.rb.running ? qsTr("Stop") : qsTr("Start")
            highlighted: !root.rb.running
            enabled: root.rb.has_database && (root.rb.running || root.rb.can_start)
            onClicked: {
                if (root.rb.running)
                    root.rb.stop()
                else {
                    const error = root.rb.start()
                    if (error)
                        statusLabel.show(error)
                }
            }
        }
    }

    Label {
        id: statusLabel
        visible: text.length > 0
        color: Theme.muted
        Layout.leftMargin: 12
        Timer { id: statusTimer; interval: 4000; onTriggered: statusLabel.text = "" }
        function show(message) { text = message; statusTimer.restart() }
    }

    // Content: the message table, or a centred message when there is no database
    Item {
        Layout.fillWidth: true
        Layout.fillHeight: true

        Label {
            anchors.centerIn: parent
            width: parent.width * 0.7
            visible: !root.rb.has_database
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            color: Theme.muted
            text: qsTr("Load a signal database (DBC/ARXML/…) on this connection to simulate its messages.")
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 12
            anchors.topMargin: 0
            visible: root.rb.has_database
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                Layout.rightMargin: 12
                spacing: 12
                Label { text: qsTr("Send"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 44 }
                Label { text: qsTr("ID"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 90 }
                Label { text: qsTr("Message"); color: Theme.muted; font.bold: true; Layout.fillWidth: true }
                Label { text: qsTr("Sender"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 130 }
                Label { text: qsTr("Cycle"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 70 }
                Label { text: qsTr("Signals"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 70 }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.alpha(Theme.border, 0.6) }

            ListView {
                id: list
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: root.rb.messages
                ScrollBar.vertical: ScrollBar {}

                delegate: Column {
                    id: messageRow
                    required property var modelData
                    property bool expanded: false
                    width: ListView.view.width

                    RowLayout {
                        width: parent.width
                        height: 34
                        spacing: 12
                        CheckBox {
                            Layout.preferredWidth: 44
                            checked: messageRow.modelData.enabled
                            enabled: !root.rb.running
                            onToggled: root.rb.set_enabled(messageRow.modelData.name, checked)
                        }
                        Label {
                            text: messageRow.modelData.id + (messageRow.modelData.extended ? "x" : "")
                            color: Theme.text
                            font.family: "monospace"
                            Layout.preferredWidth: 90
                        }
                        Label { text: messageRow.modelData.name; color: Theme.text; elide: Text.ElideRight; Layout.fillWidth: true }
                        Label { text: messageRow.modelData.senders; color: Theme.muted; elide: Text.ElideRight; Layout.preferredWidth: 130 }
                        Label {
                            text: messageRow.modelData.cycle_ms > 0 ? (messageRow.modelData.cycle_ms + " ms") : qsTr("event")
                            color: Theme.muted
                            Layout.preferredWidth: 70
                        }
                        Label {
                            text: messageRow.modelData.signal_count > 0 ? (messageRow.expanded ? "▾ " : "▸ ") + messageRow.modelData.signal_count : "–"
                            color: messageRow.modelData.signal_count > 0 ? Theme.accent : Theme.muted
                            Layout.preferredWidth: 70
                            MouseArea {
                                anchors.fill: parent
                                enabled: messageRow.modelData.signal_count > 0
                                cursorShape: Qt.PointingHandCursor
                                onClicked: messageRow.expanded = !messageRow.expanded
                            }
                        }
                    }

                    // Signal value editors, shown when the row is expanded
                    Flow {
                        width: parent.width - 56
                        x: 56
                        visible: messageRow.expanded
                        spacing: 16
                        bottomPadding: 8
                        Repeater {
                            model: messageRow.expanded ? root.rb.message_signals(messageRow.modelData.name) : []
                            delegate: RowLayout {
                                required property var modelData
                                spacing: 6
                                Label { text: modelData.name + (modelData.unit ? " [" + modelData.unit + "]" : "") + ":"; color: Theme.muted }
                                TextField {
                                    text: modelData.value
                                    Layout.preferredWidth: 80
                                    selectByMouse: true
                                    onEditingFinished: root.rb.set_value(messageRow.modelData.name, modelData.name, text)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
