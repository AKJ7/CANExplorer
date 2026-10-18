import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


Card {
    id: root
    required property var connection
    title: qsTr("Transmit")
    implicitHeight: 2 * padding + 82

    function send() {
        errorLabel.text = root.connection.send(
            idField.text, dataField.text, extendedBox.checked, fdBox.checked,
            periodicBox.checked ? periodBox.value : 0, hardwareBox.checked, headerBox.checked)
    }

    RowLayout {
        anchors.fill: parent

        Label { text: qsTr("ID") }
        TextField {
            id: idField
            text: root.connection.is_lin ? "10" : "123"
            placeholderText: qsTr("hex")
            Layout.preferredWidth: 100
            onAccepted: root.send()
        }
        CheckBox { id: extendedBox; text: qsTr("Ext"); visible: !root.connection.is_lin }
        CheckBox {
            id: headerBox
            text: root.connection.is_lin ? qsTr("Header only") : qsTr("RTR")
            ToolTip.visible: hovered
            ToolTip.text: root.connection.is_lin
                ? qsTr("Send only the LIN header; the slave publishing the frame answers")
                : qsTr("Send a remote frame")
        }
        CheckBox {
            id: fdBox
            text: qsTr("FD")
            visible: root.connection.fd
        }

        Label { text: qsTr("Data") }
        TextField {
            id: dataField
            text: "01 02 03 04"
            enabled: !headerBox.checked
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
        CheckBox {
            id: hardwareBox
            text: qsTr("HW timed")
            enabled: periodicBox.checked
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Let the interface schedule the frames. More precise, but they only appear in the trace when the bus echoes own messages.")
        }

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
            color: Theme.danger
            visible: text.length > 0
        }
    }
}
