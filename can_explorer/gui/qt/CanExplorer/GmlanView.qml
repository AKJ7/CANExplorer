import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// GMLAN (GMW3110) diagnostics: the GM/Opel diagnostic protocol, over ISO-TP (USDT) on this CAN bus. Useful on a
// GMLAN single-wire (SWCAN) connection, but works on any CAN connection that can send.
ColumnLayout {
    id: root
    required property var connection
    readonly property var gmlan: connection.gmlan
    readonly property bool ready: connection.connected && !gmlan.busy

    function report(error) {
        errorLabel.text = error || ""
    }

    function applyAddressing() {
        report(root.gmlan.set_addressing(requestIdField.text, responseIdField.text, extendedBox.checked))
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        Label { text: qsTr("GMLAN over ISO-TP"); font.bold: true }
        Label { text: qsTr("Request ID") }
        TextField {
            id: requestIdField
            text: "241"
            Layout.preferredWidth: 90
            onEditingFinished: root.applyAddressing()
        }
        Label { text: qsTr("Response ID") }
        TextField {
            id: responseIdField
            text: "641"
            Layout.preferredWidth: 90
            onEditingFinished: root.applyAddressing()
        }
        CheckBox {
            id: extendedBox
            text: qsTr("Ext")
            onToggled: root.applyAddressing()
        }
        Item { Layout.fillWidth: true }
        Label {
            text: root.connection.connected ? root.gmlan.addressing_text : qsTr("Connect first")
            color: Theme.muted
        }
    }

    // Modes and keep-alive
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        spacing: 6
        ComboBox {
            id: operationBox
            Layout.preferredWidth: 220
            textRole: "name"
            model: root.gmlan.operations
        }
        Button {
            text: qsTr("Initiate")
            enabled: root.ready
            ToolTip.visible: hovered
            ToolTip.text: qsTr("InitiateDiagnosticOperation (0x10)")
            onClicked: root.report(root.gmlan.initiate_operation(root.gmlan.operations[operationBox.currentIndex].level))
        }
        Button {
            text: qsTr("Disable normal comms")
            enabled: root.ready
            onClicked: root.report(root.gmlan.disable_normal_communication())
        }
        Button {
            text: qsTr("Return to normal")
            enabled: root.ready
            onClicked: root.report(root.gmlan.return_to_normal())
        }
        Button {
            text: qsTr("Programmed state")
            enabled: root.ready
            onClicked: root.report(root.gmlan.report_programmed_state())
        }
        Button {
            text: root.gmlan.tester_present ? qsTr("Stop tester present") : qsTr("Tester present")
            checkable: true
            checked: root.gmlan.tester_present
            enabled: root.connection.connected
            onClicked: root.report(root.gmlan.toggle_tester_present())
        }
    }

    // Data identifiers (GMLAN: one byte)
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        spacing: 6
        Label { text: qsTr("Data identifier") }
        TextField {
            id: didField
            text: "90"
            placeholderText: qsTr("hex, 1 byte")
            Layout.preferredWidth: 80
        }
        Button {
            text: qsTr("Read")
            enabled: root.ready
            onClicked: root.report(root.gmlan.read_did(didField.text))
        }
        TextField {
            id: writeField
            placeholderText: qsTr("data to write, hex")
            Layout.fillWidth: true
        }
        Button {
            text: qsTr("Write")
            enabled: root.ready
            onClicked: root.report(root.gmlan.write_did(didField.text, writeField.text))
        }
    }

    // Security access
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        spacing: 6
        Label { text: qsTr("Security level") }
        TextField {
            id: levelField
            text: "1"
            Layout.preferredWidth: 60
        }
        Button {
            text: qsTr("Key file…")
            onClicked: keyDialog.open()
        }
        Label {
            text: root.gmlan.key_file ? root.gmlan.key_file : qsTr("no key file")
            color: Theme.muted
            elide: Text.ElideMiddle
            Layout.fillWidth: true
        }
        Button {
            text: qsTr("Unlock")
            enabled: root.ready
            onClicked: root.report(root.gmlan.security_access(levelField.text))
        }
    }

    // DTCs and raw request
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        spacing: 6
        Button {
            text: qsTr("Read DTCs")
            enabled: root.ready
            onClicked: root.report(root.gmlan.read_dtcs())
        }
        Button {
            text: qsTr("Clear DTCs")
            enabled: root.ready
            onClicked: root.report(root.gmlan.clear_dtcs())
        }
        Label { text: qsTr("Raw request") }
        TextField {
            id: rawField
            placeholderText: qsTr("GMLAN request, hex, e.g. 1A 90")
            Layout.fillWidth: true
            onAccepted: root.report(root.gmlan.raw_request(text))
        }
        Button {
            text: qsTr("Send")
            enabled: root.ready
            onClicked: root.report(root.gmlan.raw_request(rawField.text))
        }
    }

    Label {
        id: errorLabel
        visible: text !== ""
        color: Theme.danger
        wrapMode: Text.WrapAnywhere
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            SplitView.preferredWidth: 320
            Label {
                text: qsTr("Diagnostic trouble codes (%1)").arg(root.gmlan.dtcs.length)
                padding: 4
            }
            ListView {
                id: dtcList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.gmlan.dtcs
                delegate: ItemDelegate {
                    required property var modelData
                    width: dtcList.width
                    text: `${modelData.code}  ${modelData.status}`
                    font.family: "monospace"
                }
            }
        }

        ColumnLayout {
            SplitView.fillWidth: true
            RowLayout {
                Label { text: qsTr("History"); padding: 4 }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Clear")
                    flat: true
                    onClicked: root.gmlan.clear_history()
                }
            }
            ListView {
                id: history
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.gmlan.history
                onCountChanged: positionViewAtEnd()
                delegate: Frame {
                    required property var modelData
                    width: history.width - 12
                    x: 4
                    padding: 6
                    ColumnLayout {
                        width: parent.width
                        Label {
                            text: `→ ${modelData.request}   ${modelData.request_description}`
                            font.family: "monospace"
                            wrapMode: Text.WrapAnywhere
                            Layout.fillWidth: true
                        }
                        Label {
                            text: (modelData.response ? `← ${modelData.response}   ` : "") + modelData.description
                            font.family: "monospace"
                            color: modelData.positive ? Theme.success : Theme.danger
                            wrapMode: Text.WrapAnywhere
                            Layout.fillWidth: true
                        }
                    }
                }
            }
        }
    }

    FileDialog {
        id: keyDialog
        title: qsTr("Seed/key algorithm")
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: root.gmlan.set_key_file(selectedFile)
    }
}
