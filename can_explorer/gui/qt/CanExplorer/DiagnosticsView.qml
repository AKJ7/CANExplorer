import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


ColumnLayout {
    id: root
    required property var diagnostics
    required property bool connected
    readonly property bool ready: connected && !diagnostics.busy
    readonly property string addressing: diagnostics.addressing

    function report(error) {
        errorLabel.text = error
    }

    function applyAddressing() {
        if (addressing === "doip")
            report(diagnostics.set_target(targetField.text))
        else
            report(diagnostics.set_addressing(requestIdField.text, responseIdField.text, extendedBox.checked, nadField.text))
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("UDS over %1").arg(root.diagnostics.transport_name); font.bold: true }
        Label { text: qsTr("Request ID"); visible: root.addressing === "can" }
        TextField {
            id: requestIdField
            text: "7E0"
            visible: root.addressing === "can"
            Layout.preferredWidth: 90
            onEditingFinished: root.applyAddressing()
        }
        Label { text: qsTr("Response ID"); visible: root.addressing === "can" }
        TextField {
            id: responseIdField
            text: "7E8"
            visible: root.addressing === "can"
            Layout.preferredWidth: 90
            onEditingFinished: root.applyAddressing()
        }
        CheckBox {
            id: extendedBox
            text: qsTr("Ext")
            visible: root.addressing === "can"
            onToggled: root.applyAddressing()
        }
        Label { text: qsTr("NAD"); visible: root.addressing === "lin" }
        TextField {
            id: nadField
            text: "01"
            visible: root.addressing === "lin"
            Layout.preferredWidth: 60
            onEditingFinished: root.applyAddressing()
        }
        Label { text: qsTr("Target address"); visible: root.addressing === "doip" }
        TextField {
            id: targetField
            text: root.addressing === "doip" ? root.diagnostics.target : ""
            visible: root.addressing === "doip"
            Layout.preferredWidth: 70
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Logical address of the ECU (hex), or of the DoIP entity itself")
            onEditingFinished: root.applyAddressing()
        }
        Button {
            text: root.diagnostics.description_file
                ? root.diagnostics.description_file.split(/[\\/]/).pop()
                : qsTr("Description…")
            ToolTip.visible: hovered
            ToolTip.text: root.diagnostics.description_file
                ? qsTr("ECU description loaded; click to unload")
                : qsTr("Load the ECU's diagnostic description (CANdelaStudio .cdd) to name and decode its DIDs")
            onClicked: root.diagnostics.description_file
                ? root.diagnostics.unload_description()
                : descriptionDialog.open()
        }
        Switch {
            text: qsTr("Tester present")
            checked: root.diagnostics.tester_present
            enabled: root.connected
            onToggled: root.diagnostics.set_tester_present(checked)
        }
        BusyIndicator {
            running: root.diagnostics.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Item { Layout.fillWidth: true }
        Label {
            id: errorLabel
            color: Theme.danger
            visible: text.length > 0
        }
    }

    GridLayout {
        columns: 5
        columnSpacing: 6
        rowSpacing: 2
        Layout.leftMargin: 4
        Layout.rightMargin: 4

        Label { text: qsTr("Session") }
        ComboBox {
            id: sessionBox
            model: root.diagnostics.sessions
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 180
        }
        Button {
            text: qsTr("Change")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.session_control(sessionBox.currentValue))
        }
        ComboBox {
            id: resetBox
            model: root.diagnostics.reset_types
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 180
        }
        Button {
            text: qsTr("ECU reset")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.ecu_reset(resetBox.currentValue))
        }

        Label { text: qsTr("Data identifier") }
        ComboBox {
            id: didBox
            editable: true
            model: root.diagnostics.known_dids.map(entry => `${entry.did}  ${entry.name}`)
            currentIndex: model.findIndex(text => text.startsWith("F190"))
            Layout.preferredWidth: 280
        }
        Button {
            text: qsTr("Read")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.read_did(didBox.editText.trim().split(/\s+/)[0]))
        }
        TextField {
            id: didDataField
            placeholderText: qsTr("data to write, hex")
            Layout.preferredWidth: 180
        }
        Button {
            text: qsTr("Write")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.write_did(didBox.editText.trim().split(/\s+/)[0], didDataField.text))
        }

        Label { text: qsTr("Security level") }
        RowLayout {
            SpinBox {
                id: levelBox
                from: 1
                to: 0x7F
                stepSize: 2
                value: 1
            }
            Button {
                text: root.diagnostics.key_file ? root.diagnostics.key_file.split(/[\\/]/).pop() : qsTr("Key file…")
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Python file defining compute_key(level: int, seed: bytes) -> bytes")
                onClicked: keyFileDialog.open()
            }
        }
        Button {
            text: qsTr("Unlock")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.security_access(levelBox.value))
        }
        Item { Layout.columnSpan: 2 }

        Label { text: qsTr("Routine") }
        RowLayout {
            ComboBox {
                id: routineTypeBox
                model: root.diagnostics.routine_types
                textRole: "name"
                valueRole: "value"
            }
            TextField {
                id: routineIdField
                placeholderText: qsTr("ID, hex")
                Layout.preferredWidth: 80
            }
        }
        Button {
            text: qsTr("Run")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.routine_control(routineTypeBox.currentValue, routineIdField.text, routineDataField.text))
        }
        TextField {
            id: routineDataField
            placeholderText: qsTr("parameters, hex")
            Layout.preferredWidth: 180
        }
        Item {}

        Label { text: qsTr("DTC status mask") }
        TextField {
            id: maskField
            text: "FF"
            Layout.preferredWidth: 60
        }
        Button {
            text: qsTr("Read DTCs")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.read_dtcs(maskField.text))
        }
        Button {
            text: qsTr("Clear DTCs")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.clear_dtcs())
        }
        Item {}

        Label { text: qsTr("Raw request") }
        TextField {
            id: requestField
            text: "22 F1 90"
            placeholderText: qsTr("UDS request, hex")
            Layout.preferredWidth: 280
            onAccepted: if (root.ready) root.report(root.diagnostics.raw_request(text))
        }
        Button {
            text: qsTr("Send")
            enabled: root.ready
            onClicked: root.report(root.diagnostics.raw_request(requestField.text))
        }
        Item { Layout.columnSpan: 2 }
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            SplitView.preferredWidth: 360

            Label {
                text: qsTr("Diagnostic trouble codes (%1)").arg(root.diagnostics.dtcs.length)
                padding: 4
            }
            ListView {
                id: dtcList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.diagnostics.dtcs
                delegate: ItemDelegate {
                    required property var modelData
                    width: dtcList.width
                    text: `${modelData.code}  ${modelData.status}`
                    font.family: "monospace"
                    ToolTip.visible: hovered
                    ToolTip.text: modelData.flags
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
                    onClicked: root.diagnostics.clear_history()
                }
            }
            ListView {
                id: history
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.diagnostics.history
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
        id: descriptionDialog
        title: qsTr("ECU diagnostic description")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("CANdelaStudio descriptions (*.cdd)"), qsTr("All files (*)")]
        onAccepted: root.report(root.diagnostics.load_description(selectedFile))
    }

    FileDialog {
        id: keyFileDialog
        title: qsTr("Seed/key algorithm")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: root.diagnostics.set_key_file(selectedFile)
    }
}
