import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// Programming an ECU over UDS: RequestDownload, TransferData and RequestTransferExit with progress.
ColumnLayout {
    id: root
    required property var connection
    readonly property var flashing: connection.flashing
    readonly property var diagnostics: connection.diagnostics
    property url lastFile

    function report(error) {
        errorLabel.text = error
    }

    function statusColor(status) {
        return status === "done" ? Theme.success
             : status === "failed" ? Theme.danger
             : status === "running" ? Theme.accent : Theme.muted
    }

    function statusIcon(status) {
        return status === "done" ? "✓" : status === "failed" ? "✕" : status === "running" ? "▶" : status === "skipped" ? "–" : "○"
    }

    spacing: 8

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("Firmware"); font.bold: true }
        Button {
            text: root.flashing.file ? root.flashing.file.split(/[\\/]/).pop() : qsTr("Open file…")
            enabled: !root.flashing.running
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Intel HEX (.hex), Motorola S-record (.s19, .s28, .s37, .srec, .mot) or binary (.bin) file")
            onClicked: fileDialog.open()
        }
        Label {
            text: qsTr("Base address")
            visible: root.flashing.binary
        }
        TextField {
            id: baseField
            text: "08000000"
            visible: root.flashing.binary
            Layout.preferredWidth: 100
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Address of the first byte of the binary file (hex)")
            onEditingFinished: if (root.lastFile.toString()) root.report(root.flashing.load_file(root.lastFile, text))
        }
        Label {
            text: root.flashing.image_text
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    Label {
        text: qsTr("UDS over %1, %2 (set in the Diagnostics view)").arg(root.diagnostics.transport_name).arg(root.diagnostics.addressing_text)
        color: Theme.muted
        Layout.leftMargin: 4
    }

    GridLayout {
        columns: 4
        columnSpacing: 12
        rowSpacing: 2
        enabled: !root.flashing.running
        Layout.leftMargin: 4

        CheckBox {
            id: sessionBox
            text: qsTr("Programming session")
            checked: true
            ToolTip.visible: hovered
            ToolTip.text: qsTr("DiagnosticSessionControl 0x02 first")
        }
        RowLayout {
            Label { text: qsTr("Security access level") }
            SpinBox {
                id: levelBox
                from: 0
                to: 0x7F
                value: 0
                stepSize: 1
                ToolTip.visible: hovered
                ToolTip.text: qsTr("0: none. Odd levels request a seed; the key comes from the key file of the Diagnostics view")
            }
        }
        RowLayout {
            Label { text: qsTr("Max. block length") }
            SpinBox {
                id: blockBox
                from: 0
                to: 4095
                value: 0
                editable: true
                ToolTip.visible: hovered
                ToolTip.text: qsTr("0: as the ECU accepts (at most 4095 bytes per TransferData)")
            }
        }
        Item { Layout.fillWidth: true }

        RowLayout {
            CheckBox {
                id: eraseBox
                text: qsTr("Erase memory, routine")
                checked: true
            }
            TextField {
                id: eraseField
                text: "FF00"
                enabled: eraseBox.checked
                Layout.preferredWidth: 70
                ToolTip.visible: hovered
                ToolTip.text: qsTr("RoutineControl started with the address and size of each segment before downloading it")
            }
        }
        RowLayout {
            CheckBox {
                id: checkBox
                text: qsTr("Check routine")
            }
            TextField {
                id: checkField
                text: "FF01"
                enabled: checkBox.checked
                Layout.preferredWidth: 70
                ToolTip.visible: hovered
                ToolTip.text: qsTr("RoutineControl started after the download, e.g. FF01 checkProgrammingDependencies")
            }
        }
        CheckBox {
            id: resetBox
            text: qsTr("ECU reset at the end")
            checked: true
        }
        Item {}
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Button {
            text: qsTr("Flash")
            highlighted: true
            enabled: !root.flashing.running && root.flashing.file.length > 0 && root.connection.connected
            onClicked: root.report(root.flashing.start({
                session: sessionBox.checked,
                security_level: levelBox.value,
                erase: eraseBox.checked,
                erase_routine: eraseField.text,
                check: checkBox.checked,
                check_routine: checkField.text,
                reset: resetBox.checked,
                max_block: blockBox.value,
            }))
        }
        Button {
            text: qsTr("Cancel")
            visible: root.flashing.running
            onClicked: root.flashing.cancel()
        }
        ProgressBar {
            value: root.flashing.progress
            Layout.fillWidth: true
        }
        Label {
            text: root.flashing.progress_text
            font.family: "monospace"
        }
    }

    Label {
        id: errorLabel
        color: Theme.danger
        visible: text.length > 0
        Layout.leftMargin: 4
    }
    Label {
        visible: root.flashing.state === "done" || root.flashing.state === "failed" || root.flashing.state === "cancelled"
        text: root.flashing.state === "done" ? qsTr("Programming finished")
              : root.flashing.state === "cancelled" ? qsTr("Cancelled") : qsTr("Programming failed: %1").arg(root.flashing.error)
        color: root.flashing.state === "done" ? Theme.success : Theme.danger
        font.bold: true
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    ListView {
        id: stepList
        clip: true
        spacing: 2
        Layout.fillWidth: true
        Layout.fillHeight: true
        ScrollBar.vertical: ScrollBar {}
        model: root.flashing.steps
        delegate: RowLayout {
            required property var modelData
            width: stepList.width - 12
            x: 4
            spacing: 10
            Label {
                text: root.statusIcon(modelData.status)
                color: root.statusColor(modelData.status)
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
                Layout.preferredWidth: 18
            }
            Label {
                text: modelData.name
                color: modelData.status === "skipped" || modelData.status === "pending" ? Theme.muted : Theme.text
                Layout.preferredWidth: 320
            }
            Label {
                text: modelData.detail
                color: modelData.status === "failed" ? Theme.danger : Theme.muted
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }
    }

    Label {
        visible: !root.flashing.file
        text: qsTr("Open a firmware file to program the ECU addressed in the Diagnostics view.")
        color: Theme.muted
        Layout.leftMargin: 4
        Layout.fillHeight: true
    }

    FileDialog {
        id: fileDialog
        title: qsTr("Firmware file")
        fileMode: FileDialog.OpenFile
        nameFilters: [
            qsTr("Firmware files (*.hex *.ihex *.ihx *.s19 *.s28 *.s37 *.srec *.mot *.bin)"),
            qsTr("All files (*)"),
        ]
        onAccepted: {
            root.lastFile = selectedFile
            root.report(root.flashing.load_file(selectedFile, baseField.text))
        }
    }
}
