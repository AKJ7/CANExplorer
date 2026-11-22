import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// VW TP 2.0 / KWP2000 diagnostics of Volkswagen-group modules (VW, Audi, Seat, Škoda, roughly 2000–2011): a channel to
// one module, the module scan, measuring blocks, coding records and raw requests. Names, scaling and coding layouts
// come from a label file the user loads; none is built in.
ColumnLayout {
    id: root
    required property var connection
    readonly property var tp20: connection.tp20
    readonly property bool ready: connection.connected && !tp20.busy

    function report(error) {
        errorLabel.text = error || ""
    }

    spacing: 4

    // Channel and labels
    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4
        spacing: 6
        Label { text: qsTr("VW TP 2.0 / KWP2000"); font.bold: true }
        Label { text: qsTr("Module") }
        TextField {
            id: moduleField
            text: "01"
            enabled: !root.tp20.channel_open
            Layout.preferredWidth: 50
            ToolTip.visible: hovered
            ToolTip.text: qsTr("The module's logical address, hex (01 is the engine)")
        }
        Label { text: qsTr("Session") }
        TextField {
            id: sessionField
            text: "89"
            enabled: !root.tp20.channel_open
            Layout.preferredWidth: 50
            ToolTip.visible: hovered
            ToolTip.text: qsTr("StartDiagnosticSession sent after opening the channel, hex; empty: none")
        }
        Button {
            text: root.tp20.channel_open ? qsTr("Close channel") : qsTr("Open channel")
            enabled: root.connection.connected && !root.tp20.busy && !root.tp20.scanning
            onClicked: {
                if (root.tp20.channel_open)
                    root.tp20.close_channel()
                else
                    root.report(root.tp20.open_channel(moduleField.text, sessionField.text))
            }
        }
        Label {
            text: root.tp20.channel_open
                  ? (root.tp20.module_name ? `${root.tp20.module_name} — ` : "") + root.tp20.channel_text
                  : (root.connection.connected ? qsTr("No channel") : qsTr("Connect first"))
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Button {
            text: root.tp20.labels_name ? qsTr("Labels: %1").arg(root.tp20.labels_name) : qsTr("Load labels…")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("A JSON label file with module names, measuring-block formulas and coding records")
            onClicked: labelsDialog.open()
        }
        Button {
            text: "✕"
            flat: true
            visible: root.tp20.labels_name !== ""
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Unload the label file")
            onClicked: root.tp20.unload_labels()
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
    Label {
        visible: root.tp20.error !== ""
        text: root.tp20.error
        color: Theme.warning
        wrapMode: Text.WrapAnywhere
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    TabBar {
        id: tabs
        objectName: "tp20Tabs"
        Layout.fillWidth: true
        TabButton { text: qsTr("Modules") }
        TabButton { text: qsTr("Measuring blocks") }
        TabButton { text: qsTr("Coding") }
        TabButton { text: qsTr("Requests") }
    }

    StackLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        currentIndex: tabs.currentIndex

        // Module scan
        ColumnLayout {
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 4
                spacing: 6
                Label { text: qsTr("Addresses") }
                TextField {
                    id: addressesField
                    text: "01-FF"
                    Layout.preferredWidth: 140
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Hex ranges and addresses, e.g. 01-FF or 01, 17, 56")
                }
                Label { text: qsTr("Identification") }
                TextField {
                    id: identificationField
                    placeholderText: qsTr("option, hex (optional)")
                    Layout.preferredWidth: 150
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Ask each module found for ReadECUIdentification with this option, after the session above")
                }
                Button {
                    text: root.tp20.scanning ? qsTr("Stop") : qsTr("Scan")
                    enabled: root.connection.connected && !root.tp20.channel_open && !root.tp20.busy
                    onClicked: {
                        if (root.tp20.scanning)
                            root.tp20.stop_scan()
                        else
                            root.report(root.tp20.scan(addressesField.text, identificationField.text, sessionField.text))
                    }
                }
                Label {
                    text: root.tp20.scan_progress
                    color: Theme.muted
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }
            }
            Label {
                text: qsTr("Close the channel to scan. Each address gets a channel setup, closed again; click a module to use it.")
                color: Theme.muted
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                Layout.leftMargin: 4
            }
            ListView {
                id: scanList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.tp20.scan_results
                delegate: ItemDelegate {
                    required property var modelData
                    width: scanList.width
                    font.family: "monospace"
                    text: `0x${modelData.address}  ${modelData.name || "—"}  `
                          + (modelData.tester_transmits_on ? `${modelData.tester_transmits_on}/${modelData.module_transmits_on}  ` : "")
                          + (modelData.identification_text || "")
                          + (modelData.error ? `  ${modelData.error}` : "")
                    enabled: !root.tp20.channel_open
                    onClicked: moduleField.text = modelData.address
                }
            }
        }

        // Measuring blocks
        ColumnLayout {
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 4
                spacing: 6
                Label { text: qsTr("Group") }
                TextField {
                    id: groupField
                    text: "1"
                    Layout.preferredWidth: 60
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Measuring-block group, 0-255 (decimal)")
                    onAccepted: root.report(root.tp20.read_group(text))
                }
                Button {
                    text: qsTr("Read")
                    enabled: root.ready && root.tp20.channel_open && !root.tp20.live
                    onClicked: root.report(root.tp20.read_group(groupField.text))
                }
                Switch {
                    text: qsTr("Live")
                    checked: root.tp20.live
                    enabled: root.tp20.channel_open
                    onToggled: root.report(root.tp20.set_live(checked, groupField.text))
                }
                Item { Layout.fillWidth: true }
                Label {
                    text: root.tp20.block.group !== undefined ? qsTr("Group %1").arg(root.tp20.block.group) : ""
                    color: Theme.muted
                }
            }
            ListView {
                id: valueList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.tp20.block.values || []
                delegate: ItemDelegate {
                    required property var modelData
                    required property int index
                    width: valueList.width
                    contentItem: RowLayout {
                        Label {
                            text: modelData.name || qsTr("Value %1").arg(index + 1)
                            Layout.preferredWidth: 240
                            elide: Text.ElideRight
                        }
                        Label {
                            text: modelData.error ? modelData.error : modelData.text
                            color: modelData.error ? Theme.danger : Theme.text
                            font.family: "monospace"
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                        }
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: modelData.raw
                }
            }
            Label {
                visible: !!root.tp20.block.unparsed
                text: qsTr("Unparsed: %1").arg(root.tp20.block.unparsed || "")
                color: Theme.warning
                font.family: "monospace"
                Layout.leftMargin: 4
            }
            Label {
                text: qsTr("Without a label file the values are raw: formula byte, a and b.")
                color: Theme.muted
                Layout.leftMargin: 4
                visible: root.tp20.labels_name === ""
            }
        }

        // Coding
        ColumnLayout {
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 4
                spacing: 6
                Label { text: qsTr("Record") }
                ComboBox {
                    id: recordBox
                    model: root.tp20.coding_records
                    Layout.preferredWidth: 200
                    enabled: count > 0
                }
                Label { text: qsTr("or local identifier") }
                TextField {
                    id: locationField
                    placeholderText: qsTr("hex, e.g. 9A")
                    Layout.preferredWidth: 90
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Read a record the label file does not describe, as raw bytes")
                }
                Button {
                    text: qsTr("Read")
                    enabled: root.ready && root.tp20.channel_open
                    onClicked: root.report(root.tp20.read_coding(recordBox.currentText || "", locationField.text))
                }
                Label {
                    text: root.tp20.coding_record
                    color: Theme.muted
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
            }
            ListView {
                id: fieldList
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.tp20.coding_fields
                delegate: Frame {
                    required property var modelData
                    width: fieldList.width - 12
                    x: 4
                    padding: 4
                    RowLayout {
                        width: parent.width
                        Label {
                            text: modelData.name + (modelData.changed ? " ●" : "")
                            color: modelData.changed ? Theme.accent : Theme.text
                            Layout.preferredWidth: 220
                            elide: Text.ElideRight
                            ToolTip.visible: modelData.description !== "" && hoverHandler.hovered
                            ToolTip.text: modelData.description
                            HoverHandler { id: hoverHandler }
                        }
                        Label {
                            text: modelData.location
                            color: Theme.muted
                            Layout.preferredWidth: 130
                        }
                        ComboBox {
                            visible: modelData.options.length > 0
                            Layout.preferredWidth: 260
                            model: modelData.options
                            textRole: "text"
                            currentIndex: modelData.options.findIndex(option => option.value === modelData.value)
                            displayText: currentIndex < 0 ? `${modelData.value}` : currentText
                            onActivated: index => root.report(root.tp20.set_field(modelData.name, modelData.options[index].value))
                        }
                        SpinBox {
                            visible: modelData.options.length === 0
                            from: 0
                            to: modelData.maximum
                            value: modelData.value
                            editable: true
                            onValueModified: root.report(root.tp20.set_field(modelData.name, value))
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 4
                spacing: 6
                Label { text: qsTr("Bytes") }
                TextField {
                    id: bytesField
                    text: root.tp20.coding_hex
                    font.family: "monospace"
                    enabled: root.tp20.coding_hex !== ""
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Edit the record's bytes directly; Enter applies")
                    onAccepted: root.report(root.tp20.set_coding_bytes(text))
                }
                Button {
                    text: qsTr("Revert")
                    enabled: root.tp20.coding_modified
                    onClicked: root.tp20.revert_coding()
                }
                Button {
                    text: qsTr("Write…")
                    enabled: root.ready && root.tp20.channel_open && root.tp20.coding_modified
                    onClicked: confirmWrite.open()
                }
            }
            Label {
                visible: root.tp20.coding_original_hex !== ""
                text: qsTr("In the module: %1").arg(root.tp20.coding_original_hex)
                font.family: "monospace"
                color: Theme.muted
                Layout.leftMargin: 4
            }
            TextField {
                visible: root.tp20.coding_backup_hex !== ""
                readOnly: true
                selectByMouse: true
                text: qsTr("Backup before the last write: %1").arg(root.tp20.coding_backup_hex)
                font.family: "monospace"
                Layout.fillWidth: true
                Layout.leftMargin: 4
            }
            Label {
                visible: root.tp20.coding_status !== ""
                text: root.tp20.coding_status
                color: root.tp20.coding_status.indexOf("did not keep") >= 0 ? Theme.danger : Theme.success
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                Layout.leftMargin: 4
            }
        }

        // Security access, raw requests and history
        ColumnLayout {
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 4
                spacing: 6
                Label { text: qsTr("Security level") }
                TextField {
                    id: levelField
                    text: "01"
                    Layout.preferredWidth: 50
                }
                TextField {
                    id: keyField
                    placeholderText: qsTr("Key file (compute_key) or keystore:<entry>")
                    Layout.fillWidth: true
                }
                Button {
                    text: "…"
                    onClicked: keyDialog.open()
                }
                Button {
                    text: qsTr("Unlock")
                    enabled: root.ready && root.tp20.channel_open
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("SecurityAccess (0x27): request a seed, send compute_key(level, seed)")
                    onClicked: root.report(root.tp20.security_access(levelField.text, keyField.text))
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 4
                spacing: 6
                Label { text: qsTr("Raw request") }
                TextField {
                    id: rawField
                    placeholderText: qsTr("KWP2000 request, hex, e.g. 21 01")
                    Layout.fillWidth: true
                    onAccepted: root.report(root.tp20.raw_request(text))
                }
                Button {
                    text: qsTr("Send")
                    enabled: root.ready && root.tp20.channel_open
                    onClicked: root.report(root.tp20.raw_request(rawField.text))
                }
                Button {
                    text: qsTr("Clear")
                    flat: true
                    onClicked: root.tp20.clear_history()
                }
            }
            ListView {
                id: history
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.tp20.history
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

    Dialog {
        id: confirmWrite
        title: qsTr("Write the coding?")
        modal: true
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Ok | Dialog.Cancel
        width: Math.min(620, (Overlay.overlay ? Overlay.overlay.width : 620) - 40)
        ColumnLayout {
            width: parent.width
            Label {
                text: qsTr("This changes the configuration of module 0x%1. The current record is read first and kept as the backup.").arg(root.tp20.module)
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            Repeater {
                model: root.tp20.coding_changes
                Label {
                    required property string modelData
                    text: modelData
                    font.family: "monospace"
                    wrapMode: Text.WrapAnywhere
                    Layout.fillWidth: true
                }
            }
        }
        onAccepted: root.report(root.tp20.write_coding())
    }

    FileChooser {
        id: labelsDialog
        title: qsTr("Label file")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Label files (*.json)"), qsTr("All files (*)")]
        onAccepted: root.report(root.tp20.load_labels(selectedFile))
    }
    FileChooser {
        id: keyDialog
        title: qsTr("Seed & key algorithm")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Python files (*.py)"), qsTr("All files (*)")]
        onAccepted: keyField.text = selectedFile.toString()
    }
}
