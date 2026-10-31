import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// OBD-II (SAE J1979): scan a vehicle for its supported values, watch them live, and read or clear trouble codes.
ColumnLayout {
    id: root
    required property var connection
    readonly property var obd: connection.obd
    readonly property string pageTitle: obd.vin ? obd.vin : (obd.scanned ? qsTr("connected") : qsTr("not scanned"))
    spacing: Theme.spacing

    RowLayout {
        spacing: 8
        Layout.fillWidth: true

        Label { text: qsTr("Request"); color: Theme.muted }
        TextField {
            id: reqField
            text: obd.request_id
            enabled: !obd.live
            Layout.preferredWidth: 80
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Request identifier (hex): 7DF is the OBD-II functional broadcast; 7E0 addresses one ECU")
        }
        Label { text: qsTr("Response"); color: Theme.muted }
        TextField {
            id: respField
            text: obd.response_id
            enabled: !obd.live
            Layout.preferredWidth: 80
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Response identifier (hex): 7E8 is the first ECU")
        }
        Button {
            text: qsTr("Scan")
            highlighted: !obd.scanned
            enabled: !obd.busy && !obd.live
            onClicked: { obd.set_ids(reqField.text, respField.text); obd.scan() }
        }
        Button {
            text: obd.live ? qsTr("Stop") : qsTr("Live")
            highlighted: obd.live
            enabled: obd.scanned && !obd.busy
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Poll the supported values continuously")
            onClicked: obd.toggle_live()
        }
        BusyIndicator {
            running: obd.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Item { Layout.fillWidth: true }
        Pill {
            visible: obd.vin.length > 0
            text: obd.vin
            tint: Theme.chart[4]
        }
        Pill {
            visible: obd.scanned
            text: obd.mil_on ? qsTr("MIL on · %1 DTC").arg(obd.dtc_count) : qsTr("MIL off")
            tint: obd.mil_on ? Theme.danger : Theme.success
        }
    }

    Label {
        visible: obd.status.length > 0
        text: obd.status
        color: Theme.muted
        Layout.fillWidth: true
        elide: Text.ElideRight
    }

    Label {
        visible: !obd.scanned && !obd.busy
        text: qsTr("Connect to a vehicle's OBD-II port (a standard 500 kbit/s CAN bus), then press Scan to read the VIN, the supported live values and the trouble codes. If nothing answers, try the request/response identifiers 7E0 and 7E8 for a single ECU.")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Live values
    Card {
        title: qsTr("Live data")
        visible: obd.scanned
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(320, 70 + Math.ceil(obd.values.length / 4) * 74)

        Flow {
            anchors.fill: parent
            spacing: 10
            Repeater {
                model: obd.values
                delegate: Rectangle {
                    id: tile
                    required property var modelData
                    width: 150
                    height: 64
                    radius: Theme.smallRadius
                    color: Theme.cardAlt
                    border.color: Theme.alpha(Theme.border, 0.6)
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 0
                        Label {
                            text: tile.modelData.name
                            color: Theme.muted
                            font.pixelSize: 11
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        RowLayout {
                            spacing: 4
                            Label {
                                text: tile.modelData.value || "—"
                                color: Theme.text
                                font.pixelSize: 22
                                font.family: "monospace"
                            }
                            Label {
                                text: tile.modelData.unit
                                color: Theme.muted
                                font.pixelSize: 12
                                Layout.alignment: Qt.AlignBottom
                                bottomPadding: 3
                            }
                        }
                    }
                }
            }
        }
    }

    // Trouble codes
    Card {
        title: qsTr("Trouble codes")
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 8

            RowLayout {
                spacing: 8
                Layout.fillWidth: true
                Button {
                    text: qsTr("Read codes")
                    enabled: obd.scanned && !obd.busy
                    onClicked: obd.read_dtcs()
                }
                Button {
                    text: qsTr("Clear codes")
                    enabled: obd.scanned && !obd.busy
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Clear the stored trouble codes and turn the MIL off (service 04)")
                    onClicked: obd.clear_dtcs()
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Theme.spacing

                Repeater {
                    model: [
                        { title: qsTr("Stored"), rows: obd.dtcs, empty: qsTr("No stored codes") },
                        { title: qsTr("Pending"), rows: obd.pending_dtcs, empty: qsTr("No pending codes") }
                    ]
                    delegate: ColumnLayout {
                        id: column
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 4
                        Label {
                            text: column.modelData.title + " (" + column.modelData.rows.length + ")"
                            color: Theme.muted
                            font.bold: true
                            font.pixelSize: 12
                        }
                        Label {
                            visible: column.modelData.rows.length === 0
                            text: column.modelData.empty
                            color: Theme.muted
                            font.pixelSize: 12
                        }
                        Repeater {
                            model: column.modelData.rows
                            delegate: Label {
                                required property var modelData
                                text: modelData.code
                                color: Theme.text
                                font.family: "monospace"
                            }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
        }
    }
}
