import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Dialog {
    title: qsTr("About %1").arg(metadata.name)
    modal: true
    anchors.centerIn: parent
    standardButtons: Dialog.Ok

    ColumnLayout {
        spacing: 12

        RowLayout {
            spacing: 16
            Logo {
                Layout.preferredWidth: 80
                Layout.preferredHeight: 80
            }
            Label {
                text: qsTr("%1 (v%2)").arg(metadata.name).arg(metadata.version)
                font.pixelSize: 20
                font.bold: true
            }
        }

        Label {
            text: metadata.app_info
            textFormat: Text.PlainText
        }

        Button {
            text: qsTr("Copy to Clipboard")
            onClicked: metadata.copy_app_info_to_clipboard()
        }
    }
}
