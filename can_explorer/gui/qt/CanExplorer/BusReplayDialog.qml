import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Transmit the frames of a log file on the bus of a connection.
Dialog {
    id: dialog
    required property var connection
    property url selectedFile
    property string error: ""

    title: qsTr("Replay Log File on the Bus")
    modal: true
    anchors.centerIn: Overlay.overlay
    standardButtons: Dialog.Ok | Dialog.Cancel

    function start() {
        return dialog.connection.bus_replay.start(dialog.selectedFile, dialog.connection.bus_replay.speeds[speedBox.currentIndex],
                                                  loopBox.checked, filterField.text)
    }

    onAboutToShow: {
        error = ""
        standardButton(Dialog.Ok).text = qsTr("Start")
        standardButton(Dialog.Ok).enabled = Qt.binding(() => dialog.selectedFile.toString().length > 0)
    }
    // Keep the dialog open when starting fails
    onAccepted: {
        const message = start()
        if (message) {
            error = message
            open()
        }
    }

    GridLayout {
        columns: 3
        columnSpacing: 10
        rowSpacing: 10

        Label { text: qsTr("File") }
        TextField {
            text: dialog.selectedFile.toString().replace("file://", "")
            readOnly: true
            placeholderText: qsTr("Choose a log file")
            Layout.preferredWidth: 320
        }
        Button {
            text: qsTr("Browse…")
            onClicked: fileDialog.open()
        }

        Label { text: qsTr("Speed") }
        ComboBox {
            id: speedBox
            model: dialog.connection.bus_replay.speed_labels.map(label => label === "Max" ? qsTr("As fast as possible") : label)
            currentIndex: dialog.connection.bus_replay.speeds.indexOf(1.0)
            Layout.columnSpan: 2
            Layout.fillWidth: true
        }

        Label { text: qsTr("Frames") }
        TextField {
            id: filterField
            placeholderText: qsTr("All; or a filter, e.g. rx !7DF 100-1FF")
            Layout.columnSpan: 2
            Layout.fillWidth: true
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Display filter syntax: hex IDs and ranges, ! excludes, ext/std/fd/rtr, rx/tx (direction in the recording)")
        }

        CheckBox {
            id: loopBox
            text: qsTr("Repeat until stopped")
            Layout.columnSpan: 3
        }

        Label {
            text: qsTr("Error frames are not sent. The frames appear as Tx in the trace.")
            color: Theme.muted
            font.pixelSize: 12
            Layout.columnSpan: 3
        }
        Label {
            visible: dialog.error.length > 0
            text: dialog.error
            color: Theme.danger
            wrapMode: Text.Wrap
            Layout.columnSpan: 3
            Layout.maximumWidth: 480
        }
    }

    FileDialog {
        id: fileDialog
        title: qsTr("Log file to replay on the bus")
        fileMode: FileDialog.OpenFile
        nameFilters: connectionManager.log_file_filters
        onAccepted: dialog.selectedFile = selectedFile
    }
}
