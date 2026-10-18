import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Dialog {
    id: dialog
    title: qsTr("Add New Connection")
    modal: true
    anchors.centerIn: parent
    standardButtons: Dialog.Ok | Dialog.Cancel

    onAboutToShow: {
        nameField.text = qsTr("Connection %1").arg(connectionManager.connections.length + 1)
        channelBox.model = connectionManager.available_channels()
    }

    onAccepted: {
        connectionManager.add_connection(
            nameField.text,
            interfaceBox.currentText,
            channelBox.editText,
            bitrateBox.currentText,
            protocolBox.currentText,
            fdCheckBox.checked,
            selfReceptionCheckBox.checked,
        )
    }

    ColumnLayout {
        spacing: 12

        GroupBox {
            title: qsTr("Connection Name")
            Layout.fillWidth: true

            TextField {
                id: nameField
                anchors.fill: parent
            }
        }

        GroupBox {
            title: qsTr("Device Configuration")
            Layout.fillWidth: true

            GridLayout {
                columns: 2
                anchors.fill: parent

                Label { text: qsTr("Interface") }
                ComboBox {
                    id: interfaceBox
                    Layout.fillWidth: true
                    Layout.preferredWidth: 240
                    model: connectionManager.interfaces
                }

                Label { text: qsTr("Channel") }
                ComboBox {
                    id: channelBox
                    Layout.fillWidth: true
                    Layout.preferredWidth: 240
                    editable: true
                }

                Label { text: qsTr("Protocol") }
                ComboBox {
                    id: protocolBox
                    Layout.fillWidth: true
                    Layout.preferredWidth: 240
                    model: connectionManager.protocols
                }

                Label { text: qsTr("Bitrate") }
                ComboBox {
                    id: bitrateBox
                    Layout.fillWidth: true
                    Layout.preferredWidth: 240
                    model: connectionManager.bitrates
                }

                CheckBox {
                    id: fdCheckBox
                    text: qsTr("Flexible Data (fd-can)")
                    Layout.columnSpan: 2
                }

                CheckBox {
                    id: selfReceptionCheckBox
                    text: qsTr("Self-Reception")
                    Layout.columnSpan: 2
                }
            }
        }
    }
}
