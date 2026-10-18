import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Editor of CAN signal databases (DBC, DBF, KCD, SYM; ARXML saved as one of those).
ColumnLayout {
    id: root
    readonly property var editor: connectionManager.database_editor
    readonly property var message: editor.message
    readonly property var signal: editor.signals.find(item => item.name === editor.current_signal) || null
    readonly property int signalIndex: editor.signals.findIndex(item => item.name === editor.current_signal)
    property string status: ""
    property bool statusIsError: false
    property var pendingAction: null

    spacing: Theme.spacing

    function show(text, error) {
        status = text
        statusIsError = error
        statusTimer.restart()
    }

    // Run an edit; on errors show them and let `restore` reset the field to the stored value
    function apply(error, restore) {
        if (error) {
            show(error, true)
            if (restore)
                restore()
        }
        return !error
    }

    // Opening, creating or reverting discards unsaved changes: ask first
    function confirmDiscard(action) {
        if (!editor.dirty) {
            action()
            return
        }
        pendingAction = action
        discardDialog.open()
    }

    function openFile(url) {
        const error = editor.open_file(url)
        if (error)
            show(error, true)
    }

    // Open a file given as a path (e.g. the database of a connection), asking first about unsaved changes
    function openPath(path) {
        if (path === editor.file_path)
            return
        confirmDiscard(() => {
            const error = editor.open_path(path)
            if (error)
                show(error, true)
        })
    }

    function signalColor(index) {
        return index === -2 ? Theme.danger : index < 0 ? Qt.color("transparent") : Qt.color(Theme.chart[index % Theme.chart.length])
    }

    component FieldLabel: Label {
        color: Theme.muted
        font.pixelSize: 12
    }

    // A text property of the selected signal, applied when editing finishes
    component SignalField: TextField {
        property string field
        text: root.signal ? root.signal[field] : ""
        Layout.fillWidth: true
        onEditingFinished: {
            if (!root.signal || text === String(root.signal[field]))
                return
            const change = {}
            change[field] = text
            root.apply(root.editor.update_signal(change), () => text = root.signal[field])
        }
    }

    // Header
    Card {
        Layout.fillWidth: true
        Layout.preferredHeight: 76
        padding: 14

        RowLayout {
            anchors.fill: parent
            spacing: 10

            ColumnLayout {
                spacing: 2
                Layout.fillWidth: true
                RowLayout {
                    spacing: 8
                    Label {
                        text: root.editor.open ? root.editor.file_name : qsTr("No database open")
                        color: Theme.text
                        font.pixelSize: Theme.titleSize
                        font.bold: true
                        elide: Text.ElideMiddle
                        Layout.maximumWidth: 360
                    }
                    Rectangle {
                        visible: root.editor.dirty
                        width: 8
                        height: 8
                        radius: 4
                        color: Theme.warning
                        ToolTip.visible: dirtyHover.hovered
                        ToolTip.text: qsTr("Unsaved changes")
                        HoverHandler { id: dirtyHover }
                    }
                    Pill {
                        visible: root.editor.format.length > 0
                        text: root.editor.format
                        tint: Theme.chart[4]
                    }
                }
                Label {
                    text: root.status || root.editor.file_path || qsTr("Signal databases: DBC, DBF, KCD and SYM; ARXML can be opened and saved as one of them")
                    color: root.status ? (root.statusIsError ? Theme.danger : Theme.success) : Theme.muted
                    font.pixelSize: 12
                    elide: Text.ElideMiddle
                    Layout.fillWidth: true
                }
            }

            Button {
                text: qsTr("New")
                onClicked: root.confirmDiscard(() => root.editor.new_database())
            }
            Button {
                text: qsTr("Open…")
                onClicked: root.confirmDiscard(() => openDialog.open())
            }
            Button {
                text: qsTr("Save")
                highlighted: root.editor.dirty
                enabled: root.editor.open && (root.editor.dirty || !root.editor.writable)
                onClicked: {
                    if (!root.editor.writable) {
                        saveDialog.open()
                        return
                    }
                    const error = root.editor.save()
                    root.show(error || qsTr("Saved %1").arg(root.editor.file_name), error.length > 0)
                }
            }
            Button {
                text: qsTr("Save as…")
                enabled: root.editor.open
                onClicked: saveDialog.open()
            }
            Button {
                text: qsTr("Revert")
                enabled: root.editor.dirty && root.editor.file_path.length > 0
                onClicked: root.confirmDiscard(() => root.editor.revert())
            }
        }
        Timer {
            id: statusTimer
            interval: 6000
            onTriggered: root.status = ""
        }
    }

    // Nothing open
    Card {
        visible: !root.editor.open
        Layout.fillWidth: true
        Layout.fillHeight: true
        ColumnLayout {
            anchors.centerIn: parent
            spacing: 14
            Label {
                text: qsTr("Open a signal database to edit its messages, signals and nodes, or create a new one.")
                color: Theme.muted
                Layout.alignment: Qt.AlignHCenter
            }
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                Button {
                    text: qsTr("Open…")
                    highlighted: true
                    onClicked: openDialog.open()
                }
                Button {
                    text: qsTr("New database")
                    onClicked: root.editor.new_database()
                }
            }
        }
    }

    RowLayout {
        visible: root.editor.open
        spacing: Theme.spacing
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Messages
        Card {
            title: qsTr("Messages (%1)").arg(root.editor.messages.length)
            Layout.preferredWidth: 330
            Layout.fillHeight: true
            headerItems: [
                ToolButton {
                    text: "+"
                    font.pixelSize: 18
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Add a message")
                    onClicked: root.apply(root.editor.add_message())
                },
                ToolButton {
                    text: "−"
                    font.pixelSize: 18
                    enabled: root.editor.current_message.length > 0
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Remove the selected message")
                    onClicked: root.apply(root.editor.remove_message())
                }
            ]

            ColumnLayout {
                anchors.fill: parent
                spacing: 8
                TextField {
                    id: messageSearch
                    placeholderText: qsTr("Search name or ID")
                    Layout.fillWidth: true
                }
                ListView {
                    id: messageList
                    clip: true
                    spacing: 4
                    model: root.editor.messages.filter(item => {
                        const query = messageSearch.text.trim().toLowerCase()
                        return !query || item.name.toLowerCase().includes(query) || item.id.toLowerCase().includes(query)
                    })
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ScrollBar.vertical: ScrollBar {}
                    delegate: ItemDelegate {
                        required property var modelData
                        width: ListView.view.width - 6
                        highlighted: modelData.name === root.editor.current_message
                        onClicked: root.editor.select_message(modelData.name)
                        contentItem: RowLayout {
                            spacing: 8
                            Label {
                                text: modelData.id
                                color: Theme.accent
                                font.family: "monospace"
                                Layout.preferredWidth: 72
                            }
                            Label {
                                text: modelData.name
                                color: Theme.text
                                font.bold: parent.parent.highlighted
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            Pill {
                                visible: modelData.fd
                                text: "FD"
                                tint: Theme.chart[5]
                            }
                            Label {
                                text: modelData.signals
                                color: Theme.muted
                                font.pixelSize: 12
                            }
                            Rectangle {
                                visible: modelData.issue
                                width: 8
                                height: 8
                                radius: 4
                                color: Theme.danger
                            }
                        }
                    }
                }
                Label {
                    text: qsTr("Nodes: %1").arg(root.editor.nodes.join(", ") || "–")
                    color: Theme.muted
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }
        }

        ScrollView {
            id: detailScroll
            contentWidth: availableWidth
            clip: true
            Layout.fillWidth: true
            Layout.fillHeight: true

            ColumnLayout {
                width: detailScroll.availableWidth
                spacing: Theme.spacing

                Card {
                    visible: root.editor.messages.length === 0
                    Layout.fillWidth: true
                    Layout.preferredHeight: 80
                    Label {
                        anchors.centerIn: parent
                        text: qsTr("The database has no messages yet; add one with +.")
                        color: Theme.muted
                    }
                }

                // Message properties
                Card {
                    visible: root.message.name !== undefined
                    title: qsTr("Message")
                    Layout.fillWidth: true
                    Layout.preferredHeight: messageGrid.implicitHeight + 70

                    GridLayout {
                        id: messageGrid
                        anchors.fill: parent
                        columns: 4
                        columnSpacing: 12
                        rowSpacing: 8

                        FieldLabel { text: qsTr("Name") }
                        TextField {
                            text: root.message.name || ""
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.message.name)
                                root.apply(root.editor.update_message({ name: text }), () => text = root.message.name)
                        }
                        FieldLabel { text: qsTr("Identifier (hex)") }
                        TextField {
                            text: root.message.frame_id || ""
                            font.family: "monospace"
                            Layout.fillWidth: true
                            onEditingFinished: if (text.toUpperCase() !== root.message.frame_id)
                                root.apply(root.editor.update_message({ frame_id: "0x" + text.replace(/^0x/i, "") }),
                                           () => text = root.message.frame_id)
                        }

                        FieldLabel { text: qsTr("Length (bytes)") }
                        ComboBox {
                            model: root.message.lengths || []
                            currentIndex: (root.message.lengths || []).indexOf(String(root.message.length))
                            Layout.fillWidth: true
                            onActivated: index => root.apply(root.editor.update_message({ length: Number(currentText) }))
                        }
                        FieldLabel { text: qsTr("Cycle time (ms)") }
                        TextField {
                            text: root.message.cycle_time || ""
                            placeholderText: qsTr("not cyclic")
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.message.cycle_time)
                                root.apply(root.editor.update_message({ cycle_time: text }), () => text = root.message.cycle_time)
                        }

                        FieldLabel { text: qsTr("Senders") }
                        TextField {
                            text: root.message.senders || ""
                            placeholderText: qsTr("Node names, comma separated")
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.message.senders)
                                root.apply(root.editor.update_message({ senders: text }), () => text = root.message.senders)
                        }
                        RowLayout {
                            Layout.columnSpan: 2
                            CheckBox {
                                text: qsTr("Extended (29 bit)")
                                checked: root.message.extended || false
                                onToggled: root.apply(root.editor.update_message({ is_extended_frame: checked }),
                                                      () => checked = root.message.extended)
                            }
                            CheckBox {
                                text: qsTr("CAN FD")
                                checked: root.message.fd || false
                                onToggled: root.apply(root.editor.update_message({ is_fd: checked }), () => checked = root.message.fd)
                            }
                        }

                        FieldLabel { text: qsTr("Comment") }
                        TextField {
                            text: root.message.comment || ""
                            Layout.columnSpan: 3
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.message.comment)
                                root.apply(root.editor.update_message({ comment: text }))
                        }
                    }
                }

                // Signals
                Card {
                    visible: root.message.name !== undefined
                    title: qsTr("Signals (%1)").arg(root.editor.signals.length)
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.max(signalGrid.implicitHeight, 200) + 70
                    headerItems: [
                        ToolButton {
                            text: "+"
                            font.pixelSize: 18
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Add a signal")
                            onClicked: root.apply(root.editor.add_signal())
                        },
                        ToolButton {
                            text: "−"
                            font.pixelSize: 18
                            enabled: root.signal !== null
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Remove the selected signal")
                            onClicked: root.apply(root.editor.remove_signal())
                        }
                    ]

                    RowLayout {
                        anchors.fill: parent
                        spacing: 14

                        ListView {
                            clip: true
                            spacing: 4
                            model: root.editor.signals
                            Layout.preferredWidth: 250
                            Layout.fillHeight: true
                            ScrollBar.vertical: ScrollBar {}
                            delegate: ItemDelegate {
                                required property var modelData
                                required property int index
                                width: ListView.view.width - 6
                                highlighted: modelData.name === root.editor.current_signal
                                onClicked: root.editor.select_signal(modelData.name)
                                contentItem: RowLayout {
                                    spacing: 8
                                    Rectangle {
                                        width: 10
                                        height: 10
                                        radius: 3
                                        color: root.signalColor(index)
                                    }
                                    Label {
                                        text: modelData.name
                                        color: Theme.text
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                    Label {
                                        text: `${modelData.start}|${modelData.length}${modelData.byte_order === "big_endian" ? "M" : "I"}`
                                        color: Theme.muted
                                        font.family: "monospace"
                                        font.pixelSize: 12
                                    }
                                }
                            }
                        }

                        Label {
                            visible: root.signal === null
                            text: qsTr("No signal selected")
                            color: Theme.muted
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignTop
                        }

                        GridLayout {
                            id: signalGrid
                            visible: root.signal !== null
                            columns: 4
                            columnSpacing: 12
                            rowSpacing: 8
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignTop

                            FieldLabel { text: qsTr("Name") }
                            SignalField { field: "name" }
                            FieldLabel { text: qsTr("Unit") }
                            SignalField { field: "unit" }

                            FieldLabel { text: qsTr("Start bit") }
                            SignalField { field: "start" }
                            FieldLabel { text: qsTr("Length (bits)") }
                            SignalField { field: "length" }

                            FieldLabel { text: qsTr("Byte order") }
                            ComboBox {
                                model: [qsTr("Intel (little endian)"), qsTr("Motorola (big endian)")]
                                currentIndex: root.signal && root.signal.byte_order === "big_endian" ? 1 : 0
                                Layout.fillWidth: true
                                onActivated: index => root.apply(root.editor.update_signal({ byte_order: root.editor.byte_orders[index] }))
                            }
                            FieldLabel { text: qsTr("Value type") }
                            ComboBox {
                                model: [qsTr("Unsigned"), qsTr("Signed")]
                                currentIndex: root.signal && root.signal.is_signed ? 1 : 0
                                Layout.fillWidth: true
                                onActivated: index => root.apply(root.editor.update_signal({ is_signed: index === 1 }))
                            }

                            FieldLabel { text: qsTr("Factor") }
                            SignalField { field: "scale" }
                            FieldLabel { text: qsTr("Offset") }
                            SignalField { field: "offset" }

                            FieldLabel { text: qsTr("Minimum") }
                            SignalField { field: "minimum"; placeholderText: qsTr("none") }
                            FieldLabel { text: qsTr("Maximum") }
                            SignalField { field: "maximum"; placeholderText: qsTr("none") }

                            FieldLabel { text: qsTr("Initial value (raw)") }
                            SignalField { field: "initial"; placeholderText: qsTr("none") }
                            FieldLabel { text: qsTr("Receivers") }
                            SignalField { field: "receivers"; placeholderText: qsTr("Node names, comma separated") }

                            FieldLabel { text: qsTr("Value descriptions") }
                            SignalField {
                                field: "choices"
                                placeholderText: qsTr("e.g. 0=Off; 1=On; 3=Error")
                                Layout.columnSpan: 3
                            }

                            FieldLabel { text: qsTr("Comment") }
                            SignalField {
                                field: "comment"
                                Layout.columnSpan: 3
                            }

                            FieldLabel {
                                visible: root.signal !== null && root.signal.multiplex !== ""
                                text: qsTr("Multiplexing")
                            }
                            Label {
                                visible: root.signal !== null && root.signal.multiplex !== ""
                                text: root.signal ? root.signal.multiplex : ""
                                color: Theme.text
                                Layout.columnSpan: 3
                            }
                        }
                    }
                }

                // Bit layout
                Card {
                    visible: root.message.name !== undefined && root.message.length > 0
                    title: qsTr("Layout")
                    Layout.fillWidth: true
                    Layout.preferredHeight: 86 + (root.message.length || 0) * 30

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 2

                        RowLayout {
                            spacing: 2
                            Label { text: ""; Layout.preferredWidth: 56 }
                            Repeater {
                                model: 8
                                Label {
                                    required property int index
                                    text: 7 - index
                                    color: Theme.muted
                                    font.pixelSize: 11
                                    horizontalAlignment: Text.AlignHCenter
                                    Layout.fillWidth: true
                                }
                            }
                        }
                        Repeater {
                            model: root.message.length || 0
                            RowLayout {
                                id: byteRow
                                required property int index
                                spacing: 2
                                Label {
                                    text: qsTr("Byte %1").arg(byteRow.index)
                                    color: Theme.muted
                                    font.pixelSize: 11
                                    Layout.preferredWidth: 56
                                }
                                Repeater {
                                    model: 8
                                    Rectangle {
                                        required property int index
                                        readonly property int bit: byteRow.index * 8 + 7 - index
                                        readonly property int owner: root.editor.layout.length > bit ? root.editor.layout[bit] : -1
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 26
                                        radius: 4
                                        color: owner === -1 ? Theme.cardAlt : Theme.alpha(root.signalColor(owner), owner === root.signalIndex ? 0.95 : 0.45)
                                        border.width: owner >= 0 && owner === root.signalIndex ? 2 : 0
                                        border.color: Theme.text
                                        Label {
                                            anchors.centerIn: parent
                                            text: parent.bit
                                            color: parent.owner === -1 ? Theme.muted : "white"
                                            font.pixelSize: 11
                                        }
                                        TapHandler {
                                            onTapped: if (parent.owner >= 0) root.editor.select_signal(root.editor.signals[parent.owner].name)
                                        }
                                        ToolTip.visible: bitHover.hovered && parent.owner !== -1
                                        ToolTip.text: parent.owner === -2 ? qsTr("Bit %1: several signals").arg(parent.bit)
                                            : parent.owner >= 0 ? qsTr("Bit %1: %2").arg(parent.bit).arg(root.editor.signals[parent.owner].name) : ""
                                        HoverHandler { id: bitHover }
                                    }
                                }
                            }
                        }
                    }
                }

                // Problems and notes
                Card {
                    visible: root.editor.issues.length > 0 || root.editor.notes.length > 0
                    title: root.editor.issues.length > 0 ? qsTr("Problems (%1)").arg(root.editor.issues.length) : qsTr("Notes")
                    Layout.fillWidth: true
                    Layout.preferredHeight: 60 + (root.editor.issues.length + root.editor.notes.length) * 24

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 4
                        Repeater {
                            model: root.editor.issues
                            Label {
                                required property string modelData
                                text: "● " + modelData
                                color: Theme.danger
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                        }
                        Repeater {
                            model: root.editor.notes
                            Label {
                                required property string modelData
                                text: "ⓘ " + modelData
                                color: Theme.warning
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }
        }
    }

    FileDialog {
        id: openDialog
        title: qsTr("Open signal database")
        fileMode: FileDialog.OpenFile
        nameFilters: root.editor.open_filters
        onAccepted: root.openFile(selectedFile)
    }

    FileDialog {
        id: saveDialog
        title: qsTr("Save signal database as")
        fileMode: FileDialog.SaveFile
        nameFilters: root.editor.save_filters
        defaultSuffix: selectedNameFilter.extensions.length > 0 ? selectedNameFilter.extensions[0] : "dbc"
        onAccepted: {
            const error = root.editor.save_as(selectedFile)
            root.show(error || qsTr("Saved %1").arg(root.editor.file_name), error.length > 0)
        }
    }

    Dialog {
        id: discardDialog
        title: qsTr("Discard changes?")
        modal: true
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Discard | Dialog.Cancel
        Label {
            text: qsTr("%1 has unsaved changes.").arg(root.editor.file_name)
        }
        onDiscarded: {
            close()
            const action = root.pendingAction
            root.pendingAction = null
            if (action)
                action()
        }
        onRejected: root.pendingAction = null
    }
}
