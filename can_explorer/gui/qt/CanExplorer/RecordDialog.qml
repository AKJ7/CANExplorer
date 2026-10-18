import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Start a recording: file, splitting into several files, and optionally a trigger.
Dialog {
    id: dialog
    required property var connection
    property url selectedFile
    property string error: ""

    title: qsTr("Record")
    modal: true
    anchors.centerIn: Overlay.overlay
    standardButtons: Dialog.Ok | Dialog.Cancel

    function start() {
        return dialog.connection.record(dialog.selectedFile, {
            split_mb: splitBox.checked && sizeField.text ? Number(sizeField.text) : 0,
            split_minutes: splitBox.checked && minutesField.text ? Number(minutesField.text) : 0,
            trigger: triggerBox.checked,
            start: startField.text,
            stop: stopField.text,
            pre: Number(preField.text || 0),
            post: Number(postField.text || 0),
            repeat: repeatBox.checked,
        })
    }

    onAboutToShow: {
        error = ""
        standardButton(Dialog.Ok).text = qsTr("Start")
        standardButton(Dialog.Ok).enabled = Qt.binding(() => dialog.selectedFile.toString().length > 0
            && (!triggerBox.checked || startField.text.trim().length > 0))
    }
    // Keep the dialog open when starting fails
    onAccepted: {
        const message = start()
        if (message) {
            error = message
            open()
        }
    }

    component NumberField: TextField {
        validator: DoubleValidator { bottom: 0; decimals: 3; notation: DoubleValidator.StandardNotation }
        inputMethodHints: Qt.ImhFormattedNumbersOnly
        horizontalAlignment: Text.AlignRight
        Layout.preferredWidth: 90
    }

    ColumnLayout {
        spacing: 12
        implicitWidth: 640

        RowLayout {
            spacing: 10
            Layout.fillWidth: true
            Label { text: qsTr("File") }
            TextField {
                text: dialog.selectedFile.toString().replace("file://", "")
                readOnly: true
                placeholderText: qsTr("Choose a log file")
                Layout.fillWidth: true
            }
            Button {
                text: qsTr("Browse…")
                onClicked: fileDialog.open()
            }
        }

        GroupBox {
            title: qsTr("Splitting")
            Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                spacing: 8
                CheckBox {
                    id: splitBox
                    text: qsTr("Split into numbered files (trace_001, trace_002, …)")
                }
                RowLayout {
                    enabled: splitBox.checked
                    spacing: 8
                    Label { text: qsTr("New file every") }
                    NumberField { id: sizeField; text: "100"; placeholderText: qsTr("off") }
                    Label { text: qsTr("MB and/or every") }
                    NumberField { id: minutesField; placeholderText: qsTr("off") }
                    Label { text: qsTr("minutes") }
                }
            }
        }

        GroupBox {
            title: qsTr("Trigger")
            Layout.fillWidth: true
            GridLayout {
                anchors.fill: parent
                columns: 4
                columnSpacing: 10
                rowSpacing: 8

                CheckBox {
                    id: triggerBox
                    text: qsTr("Record only around a trigger condition")
                    Layout.columnSpan: 4
                }

                Label { text: qsTr("Start when"); enabled: triggerBox.checked }
                TextField {
                    id: startField
                    enabled: triggerBox.checked
                    placeholderText: qsTr("e.g. EngineData.EngineSpeed > 6000, 7E8 data=03 7F, ext")
                    Layout.columnSpan: 3
                    Layout.fillWidth: true
                }
                Label { text: qsTr("Stop when"); enabled: triggerBox.checked }
                TextField {
                    id: stopField
                    enabled: triggerBox.checked
                    placeholderText: qsTr("optional; without it, recording stops after the time below")
                    Layout.columnSpan: 3
                    Layout.fillWidth: true
                }
                Label { text: qsTr("Keep before (s)"); enabled: triggerBox.checked }
                NumberField { id: preField; enabled: triggerBox.checked; text: "2" }
                Label { text: qsTr("Record after (s)"); enabled: triggerBox.checked }
                NumberField { id: postField; enabled: triggerBox.checked; text: "5" }
                CheckBox {
                    id: repeatBox
                    enabled: triggerBox.checked
                    text: qsTr("Arm again after each recording (numbered files)")
                    Layout.columnSpan: 4
                }
                Label {
                    text: qsTr("Conditions: signal comparisons (Message.Signal > value, == text), identifiers and ranges "
                               + "as in the filter, and data patterns (data=01 ?? FF).")
                    color: Theme.muted
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                    Layout.columnSpan: 4
                    Layout.fillWidth: true
                    Layout.preferredWidth: 560
                }
            }
        }

        Label {
            visible: dialog.error.length > 0
            text: dialog.error
            color: Theme.danger
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
    }

    FileDialog {
        id: fileDialog
        title: qsTr("Record to file")
        fileMode: FileDialog.SaveFile
        nameFilters: connectionManager.log_file_filters.slice(1)
        defaultSuffix: selectedNameFilter.extensions.length > 0 ? selectedNameFilter.extensions[0] : "blf"
        onAccepted: dialog.selectedFile = selectedFile
    }
}
