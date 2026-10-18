import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Dialog {
    id: dialog
    title: qsTr("Add New Connection")
    modal: true
    anchors.centerIn: parent
    standardButtons: Dialog.Ok | Dialog.Cancel

    readonly property var detected: connectionManager.detected_channels[interfaceBox.currentText] || []
    readonly property bool lin: busBox.currentText === "LIN"

    onAboutToShow: {
        nameField.text = qsTr("Connection %1").arg(connectionManager.connections.length + 1)
        if (Object.keys(connectionManager.detected_channels).length === 0)
            connectionManager.detect_channels()
    }

    onAccepted: {
        connectionManager.add_connection({
            name: nameField.text,
            bus: busBox.currentText,
            lin_baudrate: linBaudrateBox.currentText,
            interface: interfaceBox.currentText,
            channel: channelBox.editText,
            bitrate: bitrateBox.currentText,
            data_bitrate: fdCheckBox.checked ? dataBitrateBox.currentText : "",
            protocol: dialog.lin ? "LIN" : protocolBox.currentText,
            fd: fdCheckBox.checked,
            receive_own_messages: selfReceptionCheckBox.checked,
        })
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
                columns: 3
                anchors.fill: parent

                Label { text: qsTr("Bus") }
                ComboBox {
                    id: busBox
                    model: ["CAN", "LIN"]
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    ToolTip.visible: hovered && dialog.lin
                    ToolTip.text: qsTr("LIN through a SocketCAN LIN driver such as sllin (e.g. channel sllin0), or the virtual interface")
                }

                Label { text: qsTr("Interface") }
                ComboBox {
                    id: interfaceBox
                    Layout.fillWidth: true
                    Layout.preferredWidth: 240
                    Layout.columnSpan: 2
                    model: connectionManager.interfaces
                    Component.onCompleted: {
                        // Prefer an interface with detected channels
                        const index = find("SocketcanBus")
                        if (index >= 0)
                            currentIndex = index
                    }
                }

                Label { text: qsTr("Channel") }
                ComboBox {
                    id: channelBox
                    Layout.fillWidth: true
                    Layout.preferredWidth: 240
                    editable: true
                    model: dialog.detected
                }
                ToolButton {
                    text: connectionManager.detecting ? qsTr("Detecting…") : qsTr("Detect")
                    enabled: !connectionManager.detecting
                    onClicked: connectionManager.detect_channels()
                }
                Label {
                    visible: interfaceBox.currentText === "RemoteBus"
                    text: qsTr("A bus shared by can-server on another machine: HOST:PORT/BUS, e.g. raspberrypi:8765/vehicle, "
                               + "with ?token=… when the server needs one")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.columnSpan: 3
                    Layout.fillWidth: true
                    Layout.preferredWidth: 300
                }

                Label { text: qsTr("Protocol"); visible: !dialog.lin }
                ComboBox {
                    id: protocolBox
                    visible: !dialog.lin
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    model: connectionManager.protocols
                }

                Label { text: qsTr("Baud rate"); visible: dialog.lin }
                ComboBox {
                    id: linBaudrateBox
                    visible: dialog.lin
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    model: connectionManager.lin_baudrates
                    Component.onCompleted: currentIndex = Math.max(0, find("19200"))
                }

                Label { text: qsTr("Bitrate"); visible: !dialog.lin }
                ComboBox {
                    id: bitrateBox
                    visible: !dialog.lin
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    model: connectionManager.bitrates
                    Component.onCompleted: currentIndex = Math.max(0, find("500000"))
                }

                CheckBox {
                    id: fdCheckBox
                    visible: !dialog.lin
                    text: qsTr("Flexible Data (CAN FD)")
                    Layout.columnSpan: 3
                }

                Label {
                    text: qsTr("Data bitrate")
                    enabled: fdCheckBox.checked
                    visible: !dialog.lin
                }
                ComboBox {
                    id: dataBitrateBox
                    visible: !dialog.lin
                    enabled: fdCheckBox.checked
                    Layout.fillWidth: true
                    Layout.columnSpan: 2
                    model: connectionManager.data_bitrates
                    Component.onCompleted: currentIndex = Math.max(0, find("2000000"))
                }

                CheckBox {
                    id: selfReceptionCheckBox
                    text: qsTr("Self-Reception")
                    Layout.columnSpan: 3
                }
            }
        }
    }
}
