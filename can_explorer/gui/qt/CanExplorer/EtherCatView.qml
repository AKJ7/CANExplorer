import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// CANopen over EtherCAT: the slaves of a network adapter, their EtherCAT states, their object dictionaries over
// CoE SDO, their PDO assignment and emergency messages.
ColumnLayout {
    id: root
    readonly property var ethercat: connectionManager.ethercat
    readonly property string pageTitle: ethercat.is_open ? ethercat.adapter_description : ""
    readonly property bool ready: ethercat.is_open && !ethercat.busy
    readonly property bool slaveReady: ready && ethercat.selected > 0

    function report(error) {
        errorLabel.text = error
    }

    function stateColor(state, error) {
        return error ? Theme.danger
             : state === "Op" ? Theme.success
             : state === "Safe-Op" ? Theme.warning
             : state === "Pre-Op" ? Theme.accent : Theme.muted
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("Adapter"); font.bold: true }
        ComboBox {
            id: adapterBox
            enabled: !root.ethercat.is_open
            model: root.ethercat.adapters
            textRole: "description"
            valueRole: "name"
            Layout.preferredWidth: 280
            ToolTip.visible: hovered && currentValue !== undefined
            ToolTip.text: currentValue || ""
        }
        ToolButton {
            text: "⟳"
            enabled: !root.ethercat.is_open
            ToolTip.visible: hovered
            ToolTip.text: qsTr("List the network adapters again")
            onClicked: root.ethercat.refresh_adapters()
        }
        Button {
            text: root.ethercat.is_open ? qsTr("Close") : qsTr("Open")
            enabled: !root.ethercat.busy
            onClicked: root.ethercat.is_open ? root.ethercat.close_network()
                                             : root.report(root.ethercat.open_network(adapterBox.currentValue))
        }
        Button {
            text: qsTr("Scan")
            enabled: root.ready
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Find the slaves again and bring them to Pre-Op")
            onClicked: root.report(root.ethercat.scan())
        }
        Item { Layout.fillWidth: true }
        ComboBox {
            id: stateBox
            model: root.ethercat.states
            textRole: "name"
            valueRole: "value"
            currentIndex: 1
            Layout.preferredWidth: 120
        }
        Button {
            text: qsTr("To slave")
            enabled: root.slaveReady
            onClicked: root.report(root.ethercat.request_state(stateBox.currentValue, false))
        }
        Button {
            text: qsTr("To all")
            enabled: root.ready
            onClicked: root.report(root.ethercat.request_state(stateBox.currentValue, true))
        }
        BusyIndicator {
            running: root.ethercat.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
    }

    Label {
        visible: !root.ethercat.available
        text: qsTr("Only the simulated network is available: real EtherCAT adapters need the pysoem package (the \"ethercat\" extra).")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    Label {
        id: errorLabel
        color: Theme.danger
        visible: text.length > 0
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Slaves and emergency messages
        ColumnLayout {
            SplitView.preferredWidth: 300
            SplitView.minimumWidth: 220
            spacing: 2

            Label {
                text: !root.ethercat.is_open ? qsTr("Open a network adapter to find its slaves")
                    : root.ethercat.slaves.length ? qsTr("Slaves") : qsTr("No slaves found")
                color: Theme.muted
                wrapMode: Text.Wrap
                padding: 4
                Layout.fillWidth: true
            }
            ListView {
                id: slaveList
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.ethercat.slaves
                delegate: ItemDelegate {
                    id: slaveItem
                    required property var modelData
                    width: slaveList.width - 10
                    highlighted: root.ethercat.selected === modelData.position
                    onClicked: root.ethercat.select(modelData.position)
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Vendor %1, product %2, revision %3").arg(modelData.vendor)
                        .arg(modelData.product).arg(modelData.revision)
                        + (modelData.al_status ? "\n" + modelData.al_status : "")
                        + (modelData.coe ? "" : "\n" + qsTr("No CoE mailbox"))

                    contentItem: RowLayout {
                        spacing: 8
                        Label {
                            text: slaveItem.modelData.position
                            color: Theme.muted
                            font.family: "monospace"
                            Layout.preferredWidth: 20
                        }
                        Label {
                            text: slaveItem.modelData.name
                            font.bold: true
                            elide: Text.ElideRight
                            opacity: slaveItem.modelData.coe ? 1 : 0.6
                            Layout.fillWidth: true
                        }
                        Pill {
                            visible: slaveItem.modelData.emcy_count > 0
                            text: qsTr("%1 EMCY").arg(slaveItem.modelData.emcy_count)
                            tint: Theme.danger
                        }
                        Pill {
                            text: slaveItem.modelData.state
                            tint: root.stateColor(slaveItem.modelData.state, slaveItem.modelData.error)
                        }
                    }
                }
            }
            Label {
                text: qsTr("Emergencies of slave %1").arg(root.ethercat.selected)
                color: Theme.muted
                padding: 4
                visible: root.ethercat.emergencies.length > 0
            }
            ListView {
                id: emcyList
                visible: count > 0
                clip: true
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(contentHeight, 160)
                ScrollBar.vertical: ScrollBar {}
                model: root.ethercat.emergencies
                delegate: Label {
                    required property var modelData
                    width: emcyList.width - 10
                    leftPadding: 4
                    text: `${modelData.time}  ${modelData.code} ${modelData.text}`
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

        // Object dictionary and PDOs of the selected slave
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

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: root.ethercat.dictionary_source
                                ? qsTr("Object dictionary of slave %1 from %2").arg(root.ethercat.selected).arg(root.ethercat.dictionary_source)
                                : qsTr("Read the object dictionary from the slave, load its EDS file, or enter an index and sub-index below.")
                            color: Theme.muted
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Read from slave")
                            enabled: root.slaveReady
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Read the objects with the SDO information service")
                            onClicked: root.report(root.ethercat.read_dictionary())
                        }
                        Button {
                            text: root.ethercat.dictionary_source ? qsTr("Unload") : qsTr("EDS file…")
                            enabled: root.ethercat.selected > 0
                            onClicked: root.ethercat.dictionary_source ? root.ethercat.unload_dictionary() : dictionaryDialog.open()
                        }
                    }
                    TextField {
                        id: objectFilter
                        visible: root.ethercat.objects.length > 0
                        placeholderText: qsTr("Filter objects by index or name")
                        Layout.fillWidth: true
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
                            return root.ethercat.objects.filter(entry => !filter
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
                            onDoubleClicked: root.report(root.ethercat.sdo_read(modelData.index, modelData.subindex))
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
                            enabled: root.slaveReady
                            onClicked: root.report(root.ethercat.sdo_read(indexField.text, subField.text))
                        }
                        TextField {
                            id: valueField
                            placeholderText: root.ethercat.dictionary_source ? qsTr("value") : qsTr("value, hex bytes")
                            Layout.fillWidth: true
                            onAccepted: writeButton.clicked()
                        }
                        Button {
                            id: writeButton
                            text: qsTr("Write")
                            enabled: root.slaveReady
                            onClicked: root.report(root.ethercat.sdo_write(indexField.text, subField.text, valueField.text))
                        }
                    }
                    RowLayout {
                        visible: logList.count > 0
                        Layout.fillWidth: true
                        Label { text: qsTr("Log"); color: Theme.muted }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Clear")
                            flat: true
                            onClicked: root.ethercat.clear_log()
                        }
                    }
                    ListView {
                        id: logList
                        visible: count > 0
                        clip: true
                        Layout.fillWidth: true
                        Layout.preferredHeight: 110
                        ScrollBar.vertical: ScrollBar {}
                        model: root.ethercat.log
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
                            text: root.ethercat.pdos.length
                                  ? qsTr("PDOs assigned to the sync managers of slave %1").arg(root.ethercat.selected)
                                  : qsTr("Read the PDO assignment (0x1C12, 0x1C13) and mapping from the slave.")
                            color: Theme.muted
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                        }
                        Button {
                            text: qsTr("Read from slave")
                            enabled: root.slaveReady
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Read the assigned PDOs, their mapping and the mapped objects' values over SDO")
                            onClicked: root.report(root.ethercat.read_pdo_configuration())
                        }
                    }
                    ListView {
                        id: pdoList
                        clip: true
                        spacing: 6
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ScrollBar.vertical: ScrollBar {}
                        model: root.ethercat.pdos
                        delegate: Frame {
                            id: pdoItem
                            required property var modelData
                            width: pdoList.width - 12
                            padding: 8

                            ColumnLayout {
                                width: parent.width
                                spacing: 2
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: pdoItem.modelData.name; font.bold: true }
                                    Label {
                                        text: pdoItem.modelData.kind === "RxPDO" ? qsTr("outputs") : qsTr("inputs")
                                        color: Theme.muted
                                    }
                                    Item { Layout.fillWidth: true }
                                    Label {
                                        text: qsTr("%1 bit").arg(pdoItem.modelData.bits)
                                        color: Theme.muted
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
        onAccepted: root.report(root.ethercat.load_dictionary(selectedFile))
    }
}
