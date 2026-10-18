import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15


Dialog {
    id: dialog
    title: qsTr("Convert Signal Database")
    modal: true
    anchors.centerIn: parent
    standardButtons: Dialog.Close
    property url source
    property url destination

    function localPath(url) {
        return url.toString().replace(/^file:\/\//, "")
    }

    onAboutToShow: result.text = ""

    ColumnLayout {
        spacing: 8

        Label {
            text: qsTr("Converts between Vector DBC, BUSMASTER DBF, KCD and SYM (ARXML as input).")
            wrapMode: Text.Wrap
            Layout.preferredWidth: 520
        }

        GridLayout {
            columns: 3

            Label { text: qsTr("Source") }
            TextField {
                text: dialog.localPath(dialog.source)
                readOnly: true
                placeholderText: qsTr("Database to convert")
                Layout.preferredWidth: 380
            }
            Button {
                text: qsTr("Browse…")
                onClicked: sourceDialog.open()
            }

            Label { text: qsTr("Destination") }
            TextField {
                text: dialog.localPath(dialog.destination)
                readOnly: true
                placeholderText: qsTr("File to write")
                Layout.preferredWidth: 380
            }
            Button {
                text: qsTr("Browse…")
                onClicked: destinationDialog.open()
            }
        }

        Button {
            text: qsTr("Convert")
            enabled: dialog.source.toString() !== "" && dialog.destination.toString() !== ""
            Layout.alignment: Qt.AlignRight
            onClicked: result.text = connectionManager.convert_database(dialog.source, dialog.destination)
        }

        ScrollView {
            visible: result.text.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: 120

            TextArea {
                id: result
                readOnly: true
                wrapMode: Text.Wrap
            }
        }
    }

    FileDialog {
        id: sourceDialog
        title: qsTr("Database to convert")
        fileMode: FileDialog.OpenFile
        nameFilters: connectionManager.database_file_filters
        onAccepted: {
            dialog.source = selectedFile
            if (dialog.destination.toString() === "") {
                // Suggest the other format next to the source
                const path = selectedFile.toString()
                const target = path.toLowerCase().endsWith(".dbf") ? ".dbc" : ".dbf"
                dialog.destination = path.replace(/\.[^./]+$/, target)
            }
        }
    }

    FileDialog {
        id: destinationDialog
        title: qsTr("Write converted database")
        fileMode: FileDialog.SaveFile
        nameFilters: connectionManager.database_output_filters
        onAccepted: dialog.destination = selectedFile
    }
}
