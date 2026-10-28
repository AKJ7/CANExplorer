import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// XCP/CCP measurement and calibration: connect to the ECU's XCP or CCP slave on this CAN bus, load an A2L file,
// watch the measurements live and read or write the calibration values.
ColumnLayout {
    id: root
    required property var connection
    readonly property var xcp: connection.xcp
    readonly property string protocol: protocolBox.currentText

    function report(error) {
        errorLabel.text = error
    }

    function connectSlave() {
        report(root.xcp.connect_slave({
            protocol: protocolBox.currentText,
            request_id: requestField.text,
            response_id: responseField.text,
            extended: extendedBox.checked,
            station_address: stationField.text,
            byte_order: byteOrderBox.currentText === "Motorola (big endian)" ? "big" : "little",
        }))
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("Protocol"); font.bold: true }
        ComboBox {
            id: protocolBox
            model: root.xcp.protocols
            enabled: !root.xcp.connected
            Layout.preferredWidth: 90
        }
        Label { text: qsTr("Command ID") }
        TextField {
            id: requestField
            text: "7E0"
            enabled: !root.xcp.connected
            Layout.preferredWidth: 80
            ToolTip.visible: hovered
            ToolTip.text: qsTr("CAN identifier the master sends commands on (XCP CMD / CCP CRO)")
        }
        Label { text: qsTr("Response ID") }
        TextField {
            id: responseField
            text: "7E1"
            enabled: !root.xcp.connected
            Layout.preferredWidth: 80
            ToolTip.visible: hovered
            ToolTip.text: qsTr("CAN identifier the slave answers on (XCP RES / CCP DTO)")
        }
        CheckBox {
            id: extendedBox
            text: qsTr("Ext")
            enabled: !root.xcp.connected
        }
        Label { text: qsTr("Station"); visible: root.protocol === "CCP" }
        TextField {
            id: stationField
            text: "0"
            visible: root.protocol === "CCP"
            enabled: !root.xcp.connected
            Layout.preferredWidth: 60
            ToolTip.visible: hovered
            ToolTip.text: qsTr("CCP station address (hex) selecting the ECU on a shared bus")
        }
        ComboBox {
            id: byteOrderBox
            visible: root.protocol === "CCP"
            enabled: !root.xcp.connected
            model: [qsTr("Intel (little endian)"), qsTr("Motorola (big endian)")]
            currentIndex: 1
            Layout.preferredWidth: 180
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Byte order of the ECU's addresses and values (XCP reports its own)")
        }
        Button {
            text: root.xcp.connected ? qsTr("Disconnect") : qsTr("Connect")
            enabled: (root.xcp.connected || root.connection.connected) && !root.xcp.busy
            onClicked: {
                report("")
                if (root.xcp.connected)
                    root.xcp.disconnect_slave()
                else
                    root.connectSlave()
            }
        }
        Button {
            text: root.xcp.a2l_file
                ? root.xcp.a2l_file.split(/[\\/]/).pop()
                : qsTr("A2L file…")
            ToolTip.visible: hovered
            ToolTip.text: root.xcp.a2l_file ? qsTr("A2L loaded; click to unload") : qsTr("Load an A2L file to see measurements and calibration values by name")
            onClicked: root.xcp.a2l_file ? root.xcp.unload_a2l() : a2lDialog.open()
        }
        BusyIndicator {
            running: root.xcp.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Item { Layout.fillWidth: true }
        Label {
            id: errorLabel
            color: Theme.danger
            visible: text.length > 0
        }
    }

    // Read/write a raw address when there is no A2L
    RowLayout {
        Layout.fillWidth: true
        Layout.leftMargin: 4
        Layout.rightMargin: 4

        Label { text: qsTr("Address") }
        TextField {
            id: addressField
            text: "1000"
            placeholderText: qsTr("hex")
            Layout.preferredWidth: 90
        }
        Label { text: qsTr("Size") }
        SpinBox {
            id: sizeBox
            from: 1
            to: 64
            value: 2
            Layout.preferredWidth: 110
        }
        Button {
            text: qsTr("Read")
            enabled: root.xcp.connected && !root.xcp.busy
            onClicked: root.report(root.xcp.read_address(addressField.text, sizeBox.value))
        }
        TextField {
            id: writeField
            placeholderText: qsTr("bytes to write, hex")
            Layout.preferredWidth: 160
        }
        Button {
            text: qsTr("Write")
            enabled: root.xcp.connected && !root.xcp.busy
            onClicked: root.report(root.xcp.write_address(addressField.text, writeField.text))
        }
        Item { Layout.fillWidth: true }
        Label { text: qsTr("Poll every") }
        SpinBox {
            from: 50
            to: 5000
            stepSize: 50
            value: 200
            editable: true
            Layout.preferredWidth: 120
            onValueModified: root.xcp.set_poll_interval(value)
        }
        Label { text: qsTr("ms") }
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Variables from the A2L
        Card {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 420
            padding: 1

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                RowLayout {
                    Layout.fillWidth: true
                    Layout.margins: 8
                    spacing: 10
                    Label { text: qsTr("Name"); color: Theme.muted; Layout.preferredWidth: 160 }
                    Label { text: qsTr("Value"); color: Theme.muted; Layout.preferredWidth: 140 }
                    Label { text: qsTr("Type"); color: Theme.muted; Layout.preferredWidth: 90 }
                    Label { text: qsTr("Address"); color: Theme.muted; Layout.preferredWidth: 80 }
                    Label { text: qsTr("Set"); color: Theme.muted; Layout.fillWidth: true }
                }

                ListView {
                    id: variableList
                    clip: true
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ScrollBar.vertical: ScrollBar {}
                    model: root.xcp.variables
                    delegate: Rectangle {
                        id: row
                        required property var modelData
                        width: variableList.width
                        height: 40
                        color: "transparent"

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 8
                            anchors.rightMargin: 8
                            spacing: 10

                            ColumnLayout {
                                Layout.preferredWidth: 160
                                spacing: 0
                                Label {
                                    text: row.modelData.name + (row.modelData.writable ? " ✎" : "")
                                    font.bold: true
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Label {
                                    text: row.modelData.description
                                    color: Theme.muted
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                            }
                            Label {
                                text: row.modelData.error ? row.modelData.error : (row.modelData.value || "–")
                                color: row.modelData.error ? Theme.danger : Theme.text
                                font.family: "monospace"
                                elide: Text.ElideRight
                                Layout.preferredWidth: 140
                            }
                            Label {
                                text: row.modelData.type
                                color: Theme.muted
                                font.pixelSize: 11
                                Layout.preferredWidth: 90
                            }
                            Label {
                                text: row.modelData.address
                                color: Theme.muted
                                font.family: "monospace"
                                font.pixelSize: 11
                                Layout.preferredWidth: 80
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                visible: row.modelData.writable
                                TextField {
                                    id: setField
                                    placeholderText: qsTr("value")
                                    Layout.preferredWidth: 90
                                    enabled: root.xcp.connected
                                    onAccepted: root.report(root.xcp.write_variable(row.modelData.name, text))
                                }
                                Button {
                                    text: qsTr("Set")
                                    flat: true
                                    enabled: root.xcp.connected && !root.xcp.busy
                                    onClicked: root.report(root.xcp.write_variable(row.modelData.name, setField.text))
                                }
                            }
                            Item { Layout.fillWidth: true; visible: !row.modelData.writable }
                        }
                    }
                }

                Label {
                    visible: root.xcp.variables.length === 0
                    text: qsTr("Load an A2L file to see the ECU's measurements and calibration values, or read and write raw addresses above.")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    padding: 10
                    Layout.fillWidth: true
                }
            }
        }

        // Log
        ColumnLayout {
            SplitView.preferredWidth: 320
            SplitView.minimumWidth: 220
            spacing: 2

            RowLayout {
                Layout.fillWidth: true
                Label { text: qsTr("Log"); color: Theme.muted; padding: 4 }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Clear")
                    flat: true
                    onClicked: root.xcp.clear_log()
                }
            }
            ListView {
                id: logList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.xcp.log
                onCountChanged: positionViewAtEnd()
                delegate: Label {
                    required property var modelData
                    width: logList.width - 10
                    text: modelData.text
                    font.pixelSize: 12
                    color: modelData.error ? Theme.danger : Theme.text
                    wrapMode: Text.Wrap
                }
            }
        }
    }

    FileDialog {
        id: a2lDialog
        title: qsTr("A2L file")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("A2L files (*.a2l)"), qsTr("All files (*)")]
        onAccepted: root.report(root.xcp.load_a2l(selectedFile))
    }
}
