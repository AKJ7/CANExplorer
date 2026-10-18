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
    readonly property var speeds: [0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 0.0]  // As core/playback.py SPEEDS

    onAboutToShow: standardButton(Dialog.Ok).enabled = Qt.binding(() => dialog.selectedFile.toString().length > 0)
    onAccepted: connectionManager.open_log_file(selectedFile, protocolBox.currentText, speeds[speedBox.currentIndex])

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

        Label { text: qsTr("Speed") }
        ComboBox {
            id: speedBox
            model: dialog.speeds.map(speed => speed === 1 ? qsTr("1× (recorded timing)")
                                     : speed === 0 ? qsTr("As fast as possible") : `${speed}×`)
            currentIndex: 2
            Layout.columnSpan: 2
            Layout.fillWidth: true
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Can be changed, and the replay paused, while it runs")
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
