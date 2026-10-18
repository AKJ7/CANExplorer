import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15


Dialog {
    id: dialog
    title: qsTr("Replay Log File")
    modal: true
    anchors.centerIn: parent
    standardButtons: Dialog.Ok | Dialog.Cancel
    property url selectedFile

    onAboutToShow: standardButton(Dialog.Ok).enabled = Qt.binding(() => dialog.selectedFile.toString().length > 0)
    onAccepted: connectionManager.open_log_file(selectedFile, protocolBox.currentText, realtimeBox.checked)

    GridLayout {
        columns: 3

        Label { text: qsTr("File") }
        TextField {
            text: dialog.selectedFile.toString().replace("file://", "")
            readOnly: true
            placeholderText: qsTr("Choose a log file")
            Layout.preferredWidth: 280
        }
        Button {
            text: qsTr("Browse…")
            onClicked: fileDialog.open()
        }

        Label { text: qsTr("Protocol") }
        ComboBox {
            id: protocolBox
            model: connectionManager.protocols
            Layout.columnSpan: 2
            Layout.fillWidth: true
        }

        CheckBox {
            id: realtimeBox
            text: qsTr("Reproduce original timing")
            checked: true
            Layout.columnSpan: 3
        }
    }

    FileDialog {
        id: fileDialog
        title: qsTr("Open log file")
        fileMode: FileDialog.OpenFile
        nameFilters: connectionManager.log_file_filters
        onAccepted: dialog.selectedFile = selectedFile
    }
}
