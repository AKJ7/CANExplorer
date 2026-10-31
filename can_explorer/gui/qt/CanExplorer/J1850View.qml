import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// SAE J1850 VPW / GM Class 2 (SAE J1979) diagnostics, over this connection's bus (the elm327_vpw interface).
// Used on the 10.4 kbit/s Class 2 bus of 1996–2007 GM vehicles (e.g. the 99–06 P01/P59 PCMs).
ColumnLayout {
    id: root
    required property var connection
    readonly property var j1850: connection.j1850
    readonly property bool ready: connection.connected && !j1850.busy

    function report(error) {
        errorLabel.text = error || ""
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        Label { text: qsTr("J1850 VPW / GM Class 2"); font.bold: true }
        Item { Layout.fillWidth: true }
        Label {
            text: root.connection.connected ? qsTr("SAE J1979 over J1850 VPW") : qsTr("Connect first")
            color: Theme.muted
        }
    }

    // Current data (mode 0x01 PIDs)
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        spacing: 6
        Label { text: qsTr("PID") }
        ComboBox {
            id: pidBox
            Layout.preferredWidth: 260
            textRole: "name"
            model: root.j1850.pids
        }
        Button {
            text: qsTr("Read")
            enabled: root.ready
            ToolTip.visible: hovered
            ToolTip.text: qsTr("ShowCurrentData (mode 0x01)")
            onClicked: root.report(root.j1850.read_pid(root.j1850.pids[pidBox.currentIndex].number))
        }
        Button {
            text: qsTr("Read VIN")
            enabled: root.ready
            ToolTip.visible: hovered
            ToolTip.text: qsTr("RequestVehicleInformation (mode 0x09)")
            onClicked: root.report(root.j1850.read_vin())
        }
        Label {
            text: root.j1850.vin ? root.j1850.vin : ""
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    // Trouble codes and raw request
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        spacing: 6
        Button {
            text: qsTr("Read DTCs")
            enabled: root.ready
            onClicked: root.report(root.j1850.read_dtcs())
        }
        Button {
            text: qsTr("Clear DTCs")
            enabled: root.ready
            onClicked: root.report(root.j1850.clear_dtcs())
        }
        Label { text: qsTr("Raw request") }
        TextField {
            id: rawField
            placeholderText: qsTr("J1979 request, hex, e.g. 01 0C")
            Layout.fillWidth: true
            onAccepted: root.report(root.j1850.raw_request(text))
        }
        Button {
            text: qsTr("Send")
            enabled: root.ready
            onClicked: root.report(root.j1850.raw_request(rawField.text))
        }
    }

    Label {
        id: errorLabel
        visible: text !== ""
        color: Theme.danger
        wrapMode: Text.WrapAnywhere
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            SplitView.preferredWidth: 260
            Label {
                text: qsTr("Diagnostic trouble codes (%1)").arg(root.j1850.dtcs.length)
                padding: 4
            }
            ListView {
                id: dtcList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.j1850.dtcs
                delegate: ItemDelegate {
                    required property var modelData
                    width: dtcList.width
                    text: modelData.code
                    font.family: "monospace"
                }
            }
        }

        ColumnLayout {
            SplitView.fillWidth: true
            RowLayout {
                Label { text: qsTr("History"); padding: 4 }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Clear")
                    flat: true
                    onClicked: root.j1850.clear_history()
                }
            }
            ListView {
                id: history
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.j1850.history
                onCountChanged: positionViewAtEnd()
                delegate: Frame {
                    required property var modelData
                    width: history.width - 12
                    x: 4
                    padding: 6
                    ColumnLayout {
                        width: parent.width
                        Label {
                            text: `→ ${modelData.request}   ${modelData.request_description}`
                            font.family: "monospace"
                            wrapMode: Text.WrapAnywhere
                            Layout.fillWidth: true
                        }
                        Label {
                            text: (modelData.response ? `← ${modelData.response}   ` : "") + modelData.description
                            font.family: "monospace"
                            color: modelData.positive ? Theme.success : Theme.danger
                            wrapMode: Text.WrapAnywhere
                            Layout.fillWidth: true
                        }
                    }
                }
            }
        }
    }
}
