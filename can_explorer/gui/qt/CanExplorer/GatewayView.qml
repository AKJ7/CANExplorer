import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// Bus gateway: forward frames received on one connection onto another, with a filter, optional identifier
// rewriting and a per-identifier rate limit. Rules run while the gateway is on.
ColumnLayout {
    id: root
    readonly property var gateway: connectionManager.gateway
    readonly property var connections: gateway.connections
    readonly property var sources: connections.map(c => c.name)
    readonly property var destinations: connections.filter(c => c.can_transmit).map(c => c.name)
    readonly property string pageTitle: gateway.running
        ? qsTr("forwarding · %1 rule(s)").arg(gateway.rules.length) : ""

    function report(error) {
        errorLabel.text = error
    }

    function addRule() {
        const error = gateway.add_rule({
            name: nameField.text,
            source: sourceBox.currentText,
            destination: destBox.currentText,
            filter: filterField.text,
            new_id: newIdField.text,
            id_offset: offsetField.text,
            min_interval_ms: Number(rateField.text),
        })
        report(error)
        if (error === "") {
            nameField.text = ""
            filterField.text = ""
            newIdField.text = ""
            offsetField.text = ""
            rateField.text = "0"
        }
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("Bus gateway"); font.bold: true }
        Switch {
            text: root.gateway.enabled ? qsTr("On") : qsTr("Off")
            checked: root.gateway.enabled
            onToggled: root.gateway.set_enabled(checked)
        }
        Label {
            text: qsTr("Forwards frames received on the source bus onto the destination bus. Enable it once rules are set.")
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Button {
            text: qsTr("Reset counters")
            enabled: root.gateway.rules.length > 0
            onClicked: root.gateway.reset_counters()
        }
        Button {
            text: qsTr("Clear rules")
            enabled: root.gateway.rules.length > 0
            onClicked: root.gateway.clear_rules()
        }
    }

    // New rule form
    GroupBox {
        title: qsTr("Add a forwarding rule")
        Layout.fillWidth: true
        Layout.leftMargin: 4
        Layout.rightMargin: 4

        GridLayout {
            anchors.fill: parent
            columns: 8
            columnSpacing: 8
            rowSpacing: 4

            Label { text: qsTr("From") }
            ComboBox {
                id: sourceBox
                model: root.sources
                Layout.preferredWidth: 160
            }
            Label { text: "→" }
            ComboBox {
                id: destBox
                model: root.destinations
                Layout.preferredWidth: 160
            }
            Label { text: qsTr("Name") }
            TextField {
                id: nameField
                placeholderText: qsTr("optional")
                Layout.fillWidth: true
                Layout.columnSpan: 3
            }

            Label { text: qsTr("Filter") }
            TextField {
                id: filterField
                placeholderText: qsTr("e.g. 100-1FF, !7DF (empty: all)")
                Layout.preferredWidth: 160
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Which received frames to forward: identifiers and ranges in hex, ! to exclude, std/ext/fd")
            }
            Label { text: qsTr("New ID") }
            TextField {
                id: newIdField
                placeholderText: qsTr("hex")
                Layout.preferredWidth: 70
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Rewrite the identifier to this value (usually with a single-identifier filter)")
            }
            Label { text: qsTr("ID offset") }
            TextField {
                id: offsetField
                placeholderText: qsTr("±hex")
                Layout.preferredWidth: 70
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Add this (signed, hex) to the identifier instead of rewriting it")
            }
            Label { text: qsTr("Rate limit") }
            RowLayout {
                TextField {
                    id: rateField
                    text: "0"
                    Layout.preferredWidth: 60
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Minimum milliseconds between forwarded frames of the same identifier (0: no limit)")
                }
                Label { text: qsTr("ms") }
            }
            Button {
                text: qsTr("Add rule")
                enabled: root.sources.length > 0 && root.destinations.length > 0
                onClicked: root.addRule()
            }
        }
    }

    Label {
        id: errorLabel
        color: Theme.danger
        visible: text.length > 0
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    Label {
        visible: root.connections.length < 2
        text: qsTr("Open at least two connections to forward frames between them.")
        color: Theme.muted
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    // Rules
    Card {
        Layout.fillWidth: true
        Layout.fillHeight: true
        padding: 1

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 8
                spacing: 10
                Label { text: qsTr("On"); color: Theme.muted; Layout.preferredWidth: 40 }
                Label { text: qsTr("Rule"); color: Theme.muted; Layout.fillWidth: true }
                Label { text: qsTr("Filter"); color: Theme.muted; Layout.preferredWidth: 120 }
                Label { text: qsTr("Rewrite"); color: Theme.muted; Layout.preferredWidth: 100 }
                Label { text: qsTr("Rate"); color: Theme.muted; Layout.preferredWidth: 60 }
                Label { text: qsTr("Forwarded"); color: Theme.muted; Layout.preferredWidth: 90 }
                Label { text: qsTr("Dropped"); color: Theme.muted; Layout.preferredWidth: 80 }
                Label { text: ""; Layout.preferredWidth: 40 }
            }

            ListView {
                id: ruleList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.gateway.rules
                delegate: Rectangle {
                    id: ruleRow
                    required property var modelData
                    width: ruleList.width
                    height: 46
                    color: "transparent"

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 8
                        anchors.rightMargin: 8
                        spacing: 10

                        Switch {
                            checked: ruleRow.modelData.enabled
                            Layout.preferredWidth: 40
                            onToggled: root.gateway.set_rule_enabled(ruleRow.modelData.index, checked)
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0
                            RowLayout {
                                spacing: 6
                                Rectangle {
                                    width: 9
                                    height: 9
                                    radius: 5
                                    color: ruleRow.modelData.active ? Theme.success
                                         : ruleRow.modelData.enabled ? Theme.warning : Theme.muted
                                }
                                Label { text: ruleRow.modelData.name; font.bold: true; elide: Text.ElideRight }
                            }
                            Label {
                                text: `${ruleRow.modelData.source} → ${ruleRow.modelData.destination}`
                                     + (ruleRow.modelData.source_missing || ruleRow.modelData.destination_missing
                                        ? "  " + qsTr("(connection missing)") : "")
                                color: Theme.muted
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                        }
                        Label {
                            text: ruleRow.modelData.filter || qsTr("all")
                            font.family: "monospace"
                            color: Theme.muted
                            Layout.preferredWidth: 120
                            elide: Text.ElideRight
                        }
                        Label {
                            text: ruleRow.modelData.new_id ? ("ID " + ruleRow.modelData.new_id)
                                : ruleRow.modelData.id_offset ? ("+" + ruleRow.modelData.id_offset) : "—"
                            font.family: "monospace"
                            color: Theme.muted
                            Layout.preferredWidth: 100
                        }
                        Label {
                            text: ruleRow.modelData.min_interval_ms > 0
                                ? ruleRow.modelData.min_interval_ms + " ms" : "—"
                            color: Theme.muted
                            Layout.preferredWidth: 60
                        }
                        Label {
                            text: ruleRow.modelData.forwarded.toLocaleString(Qt.locale(), "f", 0)
                            font.family: "monospace"
                            color: Theme.success
                            Layout.preferredWidth: 90
                        }
                        Label {
                            text: ruleRow.modelData.dropped.toLocaleString(Qt.locale(), "f", 0)
                            font.family: "monospace"
                            color: ruleRow.modelData.dropped > 0 ? Theme.danger : Theme.muted
                            Layout.preferredWidth: 80
                        }
                        ToolButton {
                            text: "✕"
                            Layout.preferredWidth: 40
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Remove this rule")
                            onClicked: root.gateway.remove_rule(ruleRow.modelData.index)
                        }
                    }
                }
            }
        }
    }
}
