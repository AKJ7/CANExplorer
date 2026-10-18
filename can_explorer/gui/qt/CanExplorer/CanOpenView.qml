import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// CANopen master: NMT, the object dictionary of a node over SDO, its PDOs and emergency messages.
ColumnLayout {
    id: root
    required property var connection
    readonly property var canopen: connection.canopen
    readonly property bool canSend: connection.can_transmit && connection.connected
    readonly property bool ready: canSend && !canopen.busy

    function report(error) {
        errorLabel.text = error
    }

    function stateColor(state) {
        return state === "Operational" ? Theme.success
             : state === "Pre-Operational" ? Theme.accent
             : state === "Stopped" ? Theme.warning : Theme.muted
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("Node"); font.bold: true }
        SpinBox {
            id: nodeBox
            from: 1
            to: 127
            value: root.canopen.node_id
            editable: true
            onValueModified: root.canopen.set_node(value)
        }
        Button {
            text: root.canopen.dictionary_file ? root.canopen.dictionary_file.split(/[\\/]/).pop() : qsTr("Object dictionary…")
            ToolTip.visible: hovered
            ToolTip.text: root.canopen.dictionary_file
                ? qsTr("Object dictionary of the node loaded; click to unload")
                : qsTr("Load the node's EDS or DCF file to name, read and write its objects and decode its PDOs")
            onClicked: root.canopen.dictionary_file ? root.canopen.unload_dictionary() : dictionaryDialog.open()
        }
        Label {
            text: root.canopen.device_name
            color: Theme.muted
            elide: Text.ElideRight
            Layout.maximumWidth: 260
        }
        Item { Layout.fillWidth: true }
        ComboBox {
            id: nmtBox
            visible: root.connection.can_transmit
            model: root.canopen.nmt_commands
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 200
        }
        Button {
            text: qsTr("To node")
            visible: root.connection.can_transmit
            enabled: root.canSend
            onClicked: root.report(root.canopen.nmt(nmtBox.currentValue, false))
        }
        Button {
            text: qsTr("To all")
            visible: root.connection.can_transmit
            enabled: root.canSend
            onClicked: root.report(root.canopen.nmt(nmtBox.currentValue, true))
        }
        Button {
            text: qsTr("SYNC")
            visible: root.connection.can_transmit
            enabled: root.canSend
            onClicked: root.report(root.canopen.send_sync())
        }
        BusyIndicator {
            running: root.canopen.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
    }

    Label {
        id: errorLabel
        color: Theme.danger
        visible: text.length > 0
        Layout.leftMargin: 4
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Nodes and emergency messages
        ColumnLayout {
            SplitView.preferredWidth: 280
            SplitView.minimumWidth: 200
            spacing: 2

            RowLayout {
                Layout.fillWidth: true
                Label {
                    text: root.canopen.nodes.length ? qsTr("Nodes") : qsTr("No heartbeats yet")
                    color: Theme.muted
                    padding: 4
                }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Clear")
                    flat: true
                    onClicked: root.canopen.clear()
                }
            }
            ListView {
                id: nodeList
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.canopen.nodes
                delegate: ItemDelegate {
                    id: nodeItem
                    required property var modelData
                    width: nodeList.width - 10
                    highlighted: root.canopen.node_id === modelData.node_id
                    onClicked: root.canopen.set_node(modelData.node_id)
                    ToolTip.visible: hovered && modelData.last_emcy.length > 0
                    ToolTip.text: qsTr("Last emergency: %1").arg(modelData.last_emcy)

                    contentItem: RowLayout {
                        spacing: 8
                        Label {
                            text: qsTr("Node %1").arg(nodeItem.modelData.node_id)
                            font.bold: true
                            Layout.preferredWidth: 70
                        }
                        Pill {
                            text: nodeItem.modelData.state
                            tint: root.stateColor(nodeItem.modelData.state)
                        }
                        Item { Layout.fillWidth: true }
                        Pill {
                            visible: nodeItem.modelData.emcy_count > 0
                            text: qsTr("%1 EMCY").arg(nodeItem.modelData.emcy_count)
                            tint: Theme.danger
                        }
                    }
                }
            }
            Label {
                text: qsTr("Emergencies of node %1").arg(root.canopen.node_id)
                color: Theme.muted
                padding: 4
                visible: root.canopen.emergencies.length > 0
            }
            ListView {
                id: emcyList
                visible: count > 0
                clip: true
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(contentHeight, 160)
                ScrollBar.vertical: ScrollBar {}
                model: root.canopen.emergencies
                delegate: Label {
                    required property var modelData
                    width: emcyList.width - 10
                    leftPadding: 4
                    text: `${modelData.time} s  ${modelData.code} ${modelData.text}`
                    color: modelData.code === "0x0000" ? Theme.success : Theme.danger
                    elide: Text.ElideRight
                    ToolTip.visible: emcyArea.containsMouse
                    ToolTip.text: qsTr("Error register %1, data %2").arg(modelData.register).arg(modelData.data)
                    MouseArea {
                        id: emcyArea
                        anchors.fill: parent
                        hoverEnabled: true
                    }
                }
            }
        }

        // Object dictionary and PDOs
        ColumnLayout {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 360
            spacing: 4

            TabBar {
                id: tabs
                Layout.fillWidth: true
                TabButton { text: qsTr("Object dictionary"); width: implicitWidth }
                TabButton { text: qsTr("PDOs"); width: implicitWidth }
            }

            StackLayout {
                currentIndex: tabs.currentIndex
                Layout.fillWidth: true
                Layout.fillHeight: true

                ColumnLayout {
                    spacing: 4

                    TextField {
                        id: objectFilter
                        visible: root.canopen.objects.length > 0
                        placeholderText: qsTr("Filter objects by index or name")
                        Layout.fillWidth: true
                    }
                    Label {
                        visible: root.canopen.objects.length === 0
                        text: qsTr("Load the node's EDS or DCF file to browse its objects, or enter an index and sub-index below.")
                        color: Theme.muted
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        padding: 4
                    }
                    ListView {
                        id: objectList
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredHeight: 200
                        ScrollBar.vertical: ScrollBar {}
                        model: {
                            const filter = objectFilter.text.trim().toLowerCase()
                            return root.canopen.objects.filter(entry => !filter
                                || `${entry.index}:${entry.subindex} ${entry.name}`.toLowerCase().includes(filter))
                        }
                        delegate: ItemDelegate {
                            id: objectItem
                            required property var modelData
                            width: objectList.width - 10
                            highlighted: indexField.text === modelData.index && subField.text === modelData.subindex
                            onClicked: {
                                indexField.text = modelData.index
                                subField.text = modelData.subindex
                                valueField.text = modelData.default
                            }
                            contentItem: RowLayout {
                                spacing: 8
                                Label {
                                    text: `${objectItem.modelData.index}:${objectItem.modelData.subindex}`
                                    font.family: "monospace"
                                }
                                Label {
                                    text: objectItem.modelData.name
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Label {
                                    text: objectItem.modelData.type
                                    color: Theme.muted
                                    font.pixelSize: 11
                                }
                                Label {
                                    text: objectItem.modelData.access
                                    color: objectItem.modelData.writable ? Theme.accent : Theme.muted
                                    font.pixelSize: 11
                                    Layout.preferredWidth: 36
                                }
                                Label {
                                    text: objectItem.modelData.mappable ? "PDO" : ""
                                    color: Theme.success
                                    font.pixelSize: 11
                                    Layout.preferredWidth: 28
                                }
                            }
                        }
                    }
                    RowLayout {
                        visible: root.connection.can_transmit
                        Layout.fillWidth: true
                        Label { text: qsTr("Index") }
                        TextField {
                            id: indexField
                            text: "1018"
                            Layout.preferredWidth: 70
                        }
                        Label { text: qsTr("Sub") }
                        TextField {
                            id: subField
                            text: "01"
                            Layout.preferredWidth: 50
                        }
                        Button {
                            text: qsTr("Read")
                            enabled: root.ready
                            onClicked: root.report(root.canopen.sdo_read(indexField.text, subField.text))
                        }
                        TextField {
                            id: valueField
                            placeholderText: root.canopen.dictionary_file ? qsTr("value") : qsTr("value, hex bytes")
                            Layout.fillWidth: true
                            onAccepted: writeButton.clicked()
                        }
                        Button {
                            id: writeButton
                            text: qsTr("Write")
                            enabled: root.ready
                            onClicked: root.report(root.canopen.sdo_write(indexField.text, subField.text, valueField.text))
                        }
                    }
                    RowLayout {
                        visible: logList.count > 0
                        Layout.fillWidth: true
                        Label { text: qsTr("SDO and NMT"); color: Theme.muted }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Clear")
                            flat: true
                            onClicked: root.canopen.clear_log()
                        }
                    }
                    ListView {
                        id: logList
                        visible: count > 0
                        clip: true
                        Layout.fillWidth: true
                        Layout.preferredHeight: 110
                        ScrollBar.vertical: ScrollBar {}
                        model: root.canopen.log
                        onCountChanged: positionViewAtEnd()
                        delegate: Label {
                            required property var modelData
                            width: logList.width - 10
                            text: modelData.text
                            font.family: "monospace"
                            font.pixelSize: 12
                            color: modelData.error ? Theme.danger : Theme.text
                            elide: Text.ElideRight
                        }
                    }
                }

                ColumnLayout {
                    spacing: 4

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: root.canopen.pdos.length
                                  ? qsTr("PDOs of node %1").arg(root.canopen.node_id)
                                  : qsTr("Load the object dictionary or read the configuration from the node.")
                            color: Theme.muted
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                        }
                        Button {
                            text: qsTr("Read from node")
                            visible: root.connection.can_transmit
                            enabled: root.ready
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Read the communication and mapping parameters of the first four RPDOs and TPDOs over SDO")
                            onClicked: root.report(root.canopen.read_pdo_configuration())
                        }
                    }
                    ListView {
                        id: pdoList
                        clip: true
                        spacing: 6
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ScrollBar.vertical: ScrollBar {}
                        model: root.canopen.pdos
                        delegate: Frame {
                            id: pdoItem
                            required property var modelData
                            width: pdoList.width - 12
                            padding: 8
                            opacity: modelData.valid ? 1 : 0.55

                            ColumnLayout {
                                width: parent.width
                                spacing: 2
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: pdoItem.modelData.name; font.bold: true }
                                    Label {
                                        text: qsTr("COB-ID %1").arg(pdoItem.modelData.cob_id)
                                        font.family: "monospace"
                                    }
                                    Label {
                                        text: pdoItem.modelData.valid ? pdoItem.modelData.transmission : qsTr("disabled")
                                        color: Theme.muted
                                    }
                                    Item { Layout.fillWidth: true }
                                    Pill {
                                        visible: pdoItem.modelData.received
                                        text: qsTr("receiving")
                                        tint: Theme.success
                                    }
                                }
                                Label {
                                    visible: pdoItem.modelData.variables.length === 0
                                    text: qsTr("Nothing mapped")
                                    color: Theme.muted
                                }
                                Repeater {
                                    model: pdoItem.modelData.variables
                                    RowLayout {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        Label {
                                            text: modelData.object
                                            font.family: "monospace"
                                            color: Theme.muted
                                        }
                                        Label {
                                            text: modelData.name
                                            elide: Text.ElideRight
                                            Layout.fillWidth: true
                                        }
                                        Label {
                                            text: qsTr("%1 bit").arg(modelData.bits)
                                            color: Theme.muted
                                        }
                                        Label {
                                            text: modelData.value
                                            font.family: "monospace"
                                            horizontalAlignment: Text.AlignRight
                                            Layout.preferredWidth: 120
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    FileDialog {
        id: dictionaryDialog
        title: qsTr("Object dictionary")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("CANopen device descriptions (*.eds *.dcf)"), qsTr("All files (*)")]
        onAccepted: root.report(root.canopen.load_dictionary(selectedFile))
    }
}
