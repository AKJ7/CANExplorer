import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// The AUTOSAR E2E protections of SOME/IP methods and events (profiles 4, 6, 7), checked in the SOME/IP tool and in
// Ethernet captures: CRC, length, Data ID and the counter sequence.
Dialog {
    id: dialog
    readonly property var someip: connectionManager.someip

    title: qsTr("SOME/IP E2E protections")
    modal: true
    anchors.centerIn: Overlay.overlay
    standardButtons: Dialog.Close
    width: Math.min(680, (Overlay.overlay ? Overlay.overlay.width : 680) - 40)

    function report(error, message) {
        statusLabel.color = error ? Theme.danger : Theme.muted
        statusLabel.text = error || message
        return !error
    }

    onAboutToShow: statusLabel.text = ""

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacing

        Label {
            text: qsTr("Messages of these methods and events are checked as they arrive: the CRC, length and Data ID of their E2E header, and the counter sequence. The E2E header follows the SOME/IP header (offset 64 bits of the protected data, which starts at the Request ID).")
            color: Theme.muted
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        Label {
            visible: dialog.someip.e2e_protections.length === 0
            text: qsTr("No protections yet.")
            color: Theme.muted
        }
        Repeater {
            model: dialog.someip.e2e_protections
            delegate: RowLayout {
                required property var modelData
                required property int index
                spacing: 8
                Layout.fillWidth: true
                Label { text: modelData.id; font.family: "monospace"; Layout.preferredWidth: 90 }
                Label { text: modelData.name; elide: Text.ElideRight; Layout.preferredWidth: 140 }
                Label {
                    text: qsTr("Profile %1 · Data ID %2 · offset %3 · max Δ %4").arg(modelData.profile)
                        .arg(modelData.data_id).arg(modelData.offset).arg(modelData.max_delta_counter)
                    color: Theme.muted
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Button { text: qsTr("Remove"); flat: true; onClicked: dialog.someip.remove_e2e(index) }
            }
        }

        GridLayout {
            columns: 4
            columnSpacing: 8
            rowSpacing: 6
            Layout.fillWidth: true

            Label { text: qsTr("Service") }
            TextField { id: serviceField; placeholderText: qsTr("hex, e.g. 1234"); font.family: "monospace"; Layout.fillWidth: true }
            Label { text: qsTr("Method / event") }
            TextField { id: methodField; placeholderText: qsTr("hex, e.g. 8001"); font.family: "monospace"; Layout.fillWidth: true }

            Label { text: qsTr("Profile") }
            ComboBox { id: profileBox; model: dialog.someip.e2e_profiles; Layout.fillWidth: true }
            Label { text: qsTr("Data ID") }
            TextField { id: dataIdField; placeholderText: qsTr("hex"); font.family: "monospace"; Layout.fillWidth: true }

            Label { text: qsTr("Offset (bits)") }
            TextField {
                id: offsetField
                text: "64"
                validator: IntValidator { bottom: 0 }
                Layout.fillWidth: true
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Bit offset of the E2E header in the protected data; 64 places it right after the SOME/IP header")
            }
            Label { text: qsTr("Max counter Δ") }
            TextField {
                id: deltaField
                text: "1"
                validator: IntValidator { bottom: 1 }
                Layout.fillWidth: true
                ToolTip.visible: hovered
                ToolTip.text: qsTr("The largest counter step accepted between two messages; larger steps are a wrong sequence, smaller ones above 1 mean lost messages")
            }

            Label { text: qsTr("Name") }
            TextField { id: nameField; placeholderText: qsTr("optional"); Layout.fillWidth: true; Layout.columnSpan: 2 }
            Button {
                text: qsTr("Add")
                highlighted: true
                onClicked: {
                    if (dialog.report(dialog.someip.add_e2e({
                        service_id: serviceField.text, method_id: methodField.text, data_id: dataIdField.text,
                        profile: profileBox.currentText, offset: offsetField.text, max_delta_counter: deltaField.text,
                        name: nameField.text,
                    }), qsTr("Added"))) {
                        methodField.text = ""
                        dataIdField.text = ""
                        nameField.text = ""
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Label {
                id: statusLabel
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Button { text: qsTr("Import…"); onClicked: importDialog.open() }
            Button {
                text: qsTr("Export…")
                enabled: dialog.someip.e2e_protections.length > 0
                onClicked: exportDialog.open()
            }
        }
    }

    FileChooser {
        id: importDialog
        title: qsTr("Import E2E protections")
        nameFilters: [qsTr("JSON files (*.json)"), qsTr("All files (*)")]
        onAccepted: dialog.report(dialog.someip.import_e2e(selectedFile), qsTr("Imported"))
    }
    FileChooser {
        id: exportDialog
        title: qsTr("Export E2E protections")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "json"
        nameFilters: [qsTr("JSON files (*.json)")]
        onAccepted: dialog.report(dialog.someip.export_e2e(selectedFile), qsTr("Exported"))
    }
}
