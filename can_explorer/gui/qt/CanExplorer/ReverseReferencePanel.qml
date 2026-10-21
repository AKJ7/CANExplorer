import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Reference signals: fields following a known value (a database signal, a CSV column or OBD-II values).
ColumnLayout {
    id: root
    required property var tools
    required property var connection
    readonly property var reference: tools.reference
    property var checkedPids: [0x0C, 0x0D]

    signal fieldUsed()
    signal plotted()
    signal message(string text)

    spacing: 8

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Label { text: qsTr("Reference") }
        ComboBox {
            id: sourceBox
            model: root.reference.sources
            textRole: "name"
            valueRole: "key"
            displayText: count === 0 ? qsTr("Load a database or CSV file, or poll OBD-II") : currentText
            Layout.fillWidth: true
            Layout.minimumWidth: 200
        }
        Button {
            text: qsTr("CSV file…")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Time in seconds in the first column (from the first frame, or absolute), values in the others, e.g. from a GPS logger")
            onClicked: csvDialog.open()
        }
        Label { text: qsTr("Max. lag (s)") }
        TextField {
            id: lagField
            text: "1.0"
            validator: DoubleValidator { bottom: 0; top: 30 }
            Layout.preferredWidth: 60
        }
        Button {
            text: qsTr("Find matching fields")
            highlighted: true
            enabled: !root.reference.running && sourceBox.count > 0
            onClicked: root.reference.find(sourceBox.currentValue, Number(lagField.text) || 0)
        }
    }

    // OBD-II polling while recording
    Card {
        visible: root.connection.can_transmit
        title: qsTr("OBD-II values")
        padding: 10
        Layout.fillWidth: true
        Layout.preferredHeight: obdColumn.implicitHeight + 60

        ColumnLayout {
            id: obdColumn
            anchors.fill: parent
            spacing: 6

            Flow {
                spacing: 4
                Layout.fillWidth: true
                Repeater {
                    model: root.reference.obd_pids
                    CheckBox {
                        required property var modelData
                        // Reading obd_status makes the count follow the answers
                        text: (root.reference.obd_status, root.reference.obd_answers(modelData.number) > 0
                               ? qsTr("%1 (%2)").arg(modelData.name).arg(root.reference.obd_answers(modelData.number))
                               : modelData.name)
                        checked: root.checkedPids.includes(modelData.number)
                        enabled: !root.reference.obd_running
                        onToggled: root.checkedPids = checked
                            ? root.checkedPids.concat([modelData.number])
                            : root.checkedPids.filter(number => number !== modelData.number)
                    }
                }
            }
            RowLayout {
                spacing: 8
                Label { text: qsTr("Every (ms)") }
                TextField {
                    id: intervalField
                    text: "50"
                    validator: IntValidator { bottom: 10; top: 10000 }
                    enabled: !root.reference.obd_running
                    Layout.preferredWidth: 60
                }
                CheckBox {
                    id: extendedBox
                    text: qsTr("29 bit identifiers")
                    enabled: !root.reference.obd_running
                }
                Button {
                    text: root.reference.obd_running ? qsTr("Stop polling") : qsTr("Start polling")
                    highlighted: root.reference.obd_running
                    enabled: root.connection.connected || root.reference.obd_running
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Requests the values from the OBD-II port (functional address 7DF) one after the other while you drive; they then appear as references")
                    onClicked: {
                        if (root.reference.obd_running) {
                            root.reference.stop_obd()
                            return
                        }
                        const error = root.reference.start_obd(root.checkedPids, Number(intervalField.text) || 50, extendedBox.checked)
                        if (error)
                            root.message(error)
                    }
                }
                Label {
                    text: root.reference.obd_status
                    color: Theme.muted
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
            }
        }
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        BusyIndicator {
            running: root.reference.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.reference.status || qsTr("Fields are ranked by their correlation with the reference; the factor and offset convert them into its unit")
            color: Theme.muted
            font.pixelSize: 12
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
    }

    ReverseResults {
        rows: root.reference.matches
        columns: [
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Field"), role: "key", width: 140, mono: true },
            { title: qsTr("Correlation"), role: "correlation", width: 90, mono: true },
            { title: qsTr("Factor"), role: "scale", width: 90, mono: true },
            { title: qsTr("Offset"), role: "offset", width: 90, mono: true },
            { title: qsTr("Lag"), role: "lag", width: 70, mono: true },
            { title: qsTr("Condition"), role: "condition", width: 0, mono: true, optional: true },
        ]
        cellColor: (row, role) => role === "correlation"
            ? (Math.abs(row.correlation) > 0.95 ? Theme.success : Math.abs(row.correlation) > 0.8 ? Theme.warning : Theme.muted)
            : Theme.text
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => {
            root.reference.use(index)
            root.fieldUsed()
        }
        onPlotRow: index => {
            const error = root.reference.plot(index)
            if (error)
                root.message(error)
            else
                root.plotted()
        }
    }

    FileDialog {
        id: csvDialog
        title: qsTr("Open reference values")
        nameFilters: [qsTr("CSV files (*.csv *.txt)"), qsTr("All files (*)")]
        onAccepted: {
            const error = root.reference.open_csv(selectedFile)
            if (error)
                root.message(error)
        }
    }
}
