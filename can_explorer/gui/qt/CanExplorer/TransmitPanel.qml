import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


GroupBox {
    id: root
    required property var connection
    title: qsTr("Transmit")

    function send() {
        errorLabel.text = root.connection.send(
            idField.text, dataField.text, extendedBox.checked, fdBox.checked,
            periodicBox.checked ? periodBox.value : 0)
    }

    RowLayout {
        anchors.fill: parent

        Label { text: qsTr("ID") }
        TextField {
            id: idField
            text: "123"
            placeholderText: qsTr("hex")
            Layout.preferredWidth: 100
            onAccepted: root.send()
        }
        CheckBox { id: extendedBox; text: qsTr("Ext") }
        CheckBox { id: fdBox; text: qsTr("FD") }

        Label { text: qsTr("Data") }
        TextField {
            id: dataField
            text: "01 02 03 04"
            placeholderText: qsTr("hex bytes, e.g. 01 02 FF")
            Layout.fillWidth: true
            Layout.minimumWidth: 160
            onAccepted: root.send()
        }

        CheckBox { id: periodicBox; text: qsTr("Every") }
        SpinBox {
            id: periodBox
            enabled: periodicBox.checked
            from: 1
            to: 60000
            value: 100
            editable: true
        }
        Label { text: qsTr("ms") }

        Button {
            text: periodicBox.checked ? qsTr("Start") : qsTr("Send")
            enabled: root.connection.connected
            onClicked: root.send()
        }
        Button {
            text: qsTr("Stop all (%1)").arg(root.connection.periodic_count)
            enabled: root.connection.periodic_count > 0
            onClicked: root.connection.stop_periodic()
        }
        Label {
            id: errorLabel
            color: "tomato"
            visible: text.length > 0
        }
    }
}
