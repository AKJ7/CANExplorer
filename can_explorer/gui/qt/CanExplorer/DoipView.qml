import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// Diagnostics over IP (ISO 13400-2): find DoIP entities, connect to one with routing activation, then run UDS and
// flash ECUs over it with the Diagnostics and Flashing views of the connections.
ColumnLayout {
    id: root
    readonly property var doip: connectionManager.doip
    readonly property string pageTitle: doip.connection_text
    // The simulated entity is chosen in the list; typing an address chooses that address instead
    property bool useSimulated: false
    readonly property bool simulatedChosen: useSimulated || doip.simulated

    function report(error) {
        errorLabel.text = error
    }

    function connectEntity() {
        report(doip.connect_entity({
            host: simulatedChosen ? "simulated" : hostField.text,
            port: Number(portField.text),
            tester_address: testerField.text,
            activation_type: activationBox.currentValue,
            protocol_version: versionBox.currentValue,
            tls: tlsBox.checked,
        }))
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("Entity"); font.bold: true }
        TextField {
            id: hostField
            enabled: !root.doip.is_open
            text: root.simulatedChosen ? qsTr("Simulated entity") : ""
            placeholderText: qsTr("IP address")
            Layout.preferredWidth: 170
            onTextEdited: root.useSimulated = false
            onAccepted: if (!root.doip.is_open && !root.doip.busy) root.connectEntity()
        }
        Label { text: qsTr("Port") }
        TextField {
            id: portField
            enabled: !root.doip.is_open && !root.simulatedChosen
            text: String(root.doip.default_port)
            validator: IntValidator { bottom: 1; top: 65535 }
            Layout.preferredWidth: 70
        }
        CheckBox {
            id: tlsBox
            text: qsTr("TLS")
            enabled: !root.doip.is_open && !root.simulatedChosen
            ToolTip.visible: hovered
            ToolTip.text: qsTr("TLS (ISO 13400-2:2019, port %1). The entity's certificate is not verified").arg(root.doip.tls_port)
            onToggled: {
                if (checked && portField.text === String(root.doip.default_port))
                    portField.text = String(root.doip.tls_port)
                else if (!checked && portField.text === String(root.doip.tls_port))
                    portField.text = String(root.doip.default_port)
            }
        }
        Label { text: qsTr("Tester address") }
        TextField {
            id: testerField
            enabled: !root.doip.is_open
            text: root.doip.default_tester_address
            Layout.preferredWidth: 60
            ToolTip.visible: hovered
            ToolTip.text: qsTr("The tester's logical address (hex); external test equipment uses 0E00 to 0FFF")
        }
        ComboBox {
            id: activationBox
            enabled: !root.doip.is_open
            model: root.doip.activation_types
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 170
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Routing activation type")
        }
        ComboBox {
            id: versionBox
            enabled: !root.doip.is_open
            model: root.doip.protocol_versions
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 160
            ToolTip.visible: hovered
            ToolTip.text: qsTr("DoIP protocol version of the messages sent")
        }
        Button {
            text: root.doip.is_open ? qsTr("Disconnect") : qsTr("Connect")
            enabled: !root.doip.busy
            onClicked: {
                root.report("")
                if (root.doip.is_open)
                    root.doip.disconnect_entity()
                else
                    root.connectEntity()
            }
        }
        Button {
            text: qsTr("Status")
            enabled: root.doip.is_open
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Ask the entity for its status and diagnostic power mode")
            onClicked: root.report(root.doip.query_status())
        }
        BusyIndicator {
            running: root.doip.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Item { Layout.fillWidth: true }
    }

    Label {
        id: errorLabel
        color: Theme.danger
        visible: text.length > 0
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Entities found and the log
        ColumnLayout {
            SplitView.preferredWidth: 340
            SplitView.minimumWidth: 260
            spacing: 2

            RowLayout {
                Layout.fillWidth: true
                TextField {
                    id: discoverField
                    placeholderText: qsTr("Broadcast (255.255.255.255)")
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Where to send the vehicle identification request: empty for every network, a directed broadcast such as 192.168.0.255, or one entity's address")
                    onAccepted: root.report(root.doip.discover(text))
                }
                Button {
                    text: qsTr("Discover")
                    enabled: !root.doip.discovering
                    onClicked: root.report(root.doip.discover(discoverField.text))
                }
            }
            ListView {
                id: entityList
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.doip.entities
                delegate: ItemDelegate {
                    id: entityItem
                    required property var modelData
                    width: entityList.width - 10
                    highlighted: modelData.simulated ? root.simulatedChosen
                                                     : !root.simulatedChosen && hostField.text === modelData.host
                    enabled: !root.doip.is_open
                    onClicked: {
                        root.useSimulated = modelData.simulated
                        if (!modelData.simulated) {
                            hostField.text = modelData.host
                            portField.text = String(modelData.port)
                        }
                    }
                    contentItem: ColumnLayout {
                        spacing: 0
                        Label {
                            text: entityItem.modelData.simulated ? entityItem.modelData.vin
                                : `${entityItem.modelData.host}  ·  0x${entityItem.modelData.logical_address}`
                            font.bold: true
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Label {
                            visible: !entityItem.modelData.simulated
                            text: qsTr("VIN %1 · EID %2").arg(entityItem.modelData.vin || "—").arg(entityItem.modelData.eid)
                            color: Theme.muted
                            font.pixelSize: 11
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Label {
                            visible: entityItem.modelData.further_action.length > 0
                            text: qsTr("Further action: %1").arg(entityItem.modelData.further_action)
                            color: Theme.warning
                            font.pixelSize: 11
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Label { text: qsTr("Log"); color: Theme.muted; padding: 4 }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Clear")
                    flat: true
                    onClicked: root.doip.clear_log()
                }
            }
            ListView {
                id: logList
                clip: true
                Layout.fillWidth: true
                Layout.preferredHeight: 160
                ScrollBar.vertical: ScrollBar {}
                model: root.doip.log
                onCountChanged: positionViewAtEnd()
                delegate: Label {
                    required property var modelData
                    width: logList.width - 10
                    text: modelData.text
                    font.pixelSize: 12
                    color: modelData.error ? Theme.danger : Theme.text
                    wrapMode: Text.Wrap
                }
            }
        }

        // UDS over the connection
        ColumnLayout {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 400
            spacing: 2

            TabBar {
                id: tabs
                TabButton { text: qsTr("Diagnostics"); width: implicitWidth }
                TabButton { text: qsTr("Flashing"); width: implicitWidth }
            }
            StackLayout {
                currentIndex: tabs.currentIndex
                Layout.fillWidth: true
                Layout.fillHeight: true

                DiagnosticsView {
                    diagnostics: root.doip.diagnostics
                    connected: root.doip.is_open
                }
                FlashView {
                    flashing: root.doip.flashing
                    diagnostics: root.doip.diagnostics
                    connected: root.doip.is_open
                }
            }
        }
    }
}
