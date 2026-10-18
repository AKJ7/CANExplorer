import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Editor of signal databases: CAN (DBC, DBF, KCD, SYM; ARXML saved as one of those) and LIN (LDF).
ColumnLayout {
    id: root
    readonly property var editor: connectionManager.database_editor
    readonly property bool lin: editor.kind === "lin"
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
        text: root.signal && root.signal[field] !== undefined ? root.signal[field] : ""  // CAN or LIN fields
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
                id: newButton
                text: qsTr("New ▾")
                onClicked: newMenu.popup(newButton, 0, newButton.height)
                Menu {
                    id: newMenu
                    MenuItem {
                        text: qsTr("CAN database")
                        onTriggered: root.confirmDiscard(() => root.editor.new_database())
                    }
                    MenuItem {
                        text: qsTr("LIN database (LDF)")
                        onTriggered: root.confirmDiscard(() => root.editor.new_lin_database())
                    }
                }
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
                text: qsTr("Open a CAN or LIN database to edit its messages or frames, signals and nodes, or create a new one.")
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
                    text: qsTr("New CAN database")
                    onClicked: root.editor.new_database()
                }
                Button {
                    text: qsTr("New LIN database")
                    onClicked: root.editor.new_lin_database()
                }
            }
        }
    }

    // LIN databases have more parts than frames and signals
    TabBar {
        id: linTabs
        visible: root.editor.open && root.lin
        Layout.fillWidth: true
        TabButton { text: qsTr("Frames and signals"); width: implicitWidth }
        TabButton { text: qsTr("Nodes"); width: implicitWidth }
        TabButton { text: qsTr("Encoding types"); width: implicitWidth }
        TabButton { text: qsTr("Schedule tables"); width: implicitWidth }
    }

    RowLayout {
        visible: root.editor.open && (!root.lin || linTabs.currentIndex === 0)
        spacing: Theme.spacing
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Messages
        Card {
            title: (root.lin ? qsTr("Frames (%1)") : qsTr("Messages (%1)")).arg(root.editor.messages.length)
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
                        text: root.lin ? qsTr("The database has no frames yet; add one with +.")
                                       : qsTr("The database has no messages yet; add one with +.")
                        color: Theme.muted
                    }
                }

                // Message properties
                Card {
                    visible: root.message.name !== undefined
                    title: root.lin ? qsTr("Frame") : qsTr("Message")
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
                        FieldLabel { text: root.lin ? qsTr("Identifier (hex, 0–3B)") : qsTr("Identifier (hex)") }
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
                        FieldLabel { text: qsTr("Publisher"); visible: root.lin }
                        ComboBox {
                            visible: root.lin
                            model: root.editor.nodes
                            currentIndex: root.editor.nodes.indexOf(root.message.publisher || "")
                            Layout.fillWidth: true
                            onActivated: index => root.apply(root.editor.update_message({ publisher: currentText }))
                        }
                        FieldLabel { text: qsTr("Cycle time (ms)"); visible: !root.lin }
                        TextField {
                            visible: !root.lin
                            text: root.message.cycle_time || ""
                            placeholderText: qsTr("not cyclic")
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.message.cycle_time)
                                root.apply(root.editor.update_message({ cycle_time: text }), () => text = root.message.cycle_time)
                        }

                        FieldLabel { text: qsTr("Senders"); visible: !root.lin }
                        TextField {
                            visible: !root.lin
                            text: root.message.senders || ""
                            placeholderText: qsTr("Node names, comma separated")
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.message.senders)
                                root.apply(root.editor.update_message({ senders: text }), () => text = root.message.senders)
                        }
                        RowLayout {
                            visible: !root.lin
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

                        FieldLabel { text: qsTr("Comment"); visible: !root.lin }
                        TextField {
                            visible: !root.lin
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
                            FieldLabel { text: qsTr("Unit"); visible: !root.lin }
                            SignalField { field: "unit"; visible: !root.lin }
                            FieldLabel { text: qsTr("Encoding type"); visible: root.lin }
                            ComboBox {
                                visible: root.lin
                                model: [qsTr("(none)")].concat(root.editor.encoding_names)
                                currentIndex: root.signal && root.signal.encoding ? root.editor.encoding_names.indexOf(root.signal.encoding) + 1 : 0
                                Layout.fillWidth: true
                                onActivated: index => root.apply(root.editor.update_signal({ encoding: index === 0 ? "" : currentText }))
                            }

                            FieldLabel { text: qsTr("Start bit") }
                            SignalField { field: "start" }
                            FieldLabel { text: qsTr("Length (bits)") }
                            SignalField { field: "length" }

                            FieldLabel { text: qsTr("Byte order"); visible: !root.lin }
                            ComboBox {
                                visible: !root.lin
                                model: [qsTr("Intel (little endian)"), qsTr("Motorola (big endian)")]
                                currentIndex: root.signal && root.signal.byte_order === "big_endian" ? 1 : 0
                                Layout.fillWidth: true
                                onActivated: index => root.apply(root.editor.update_signal({ byte_order: root.editor.byte_orders[index] }))
                            }
                            FieldLabel { text: qsTr("Value type"); visible: !root.lin }
                            ComboBox {
                                visible: !root.lin
                                model: [qsTr("Unsigned"), qsTr("Signed")]
                                currentIndex: root.signal && root.signal.is_signed ? 1 : 0
                                Layout.fillWidth: true
                                onActivated: index => root.apply(root.editor.update_signal({ is_signed: index === 1 }))
                            }

                            FieldLabel { text: qsTr("Factor"); visible: !root.lin }
                            SignalField { field: "scale"; visible: !root.lin }
                            FieldLabel { text: qsTr("Offset"); visible: !root.lin }
                            SignalField { field: "offset"; visible: !root.lin }

                            FieldLabel { text: qsTr("Minimum"); visible: !root.lin }
                            SignalField { field: "minimum"; placeholderText: qsTr("none"); visible: !root.lin }
                            FieldLabel { text: qsTr("Maximum"); visible: !root.lin }
                            SignalField { field: "maximum"; placeholderText: qsTr("none"); visible: !root.lin }

                            FieldLabel { text: root.lin ? qsTr("Initial value") : qsTr("Initial value (raw)") }
                            SignalField {
                                field: "initial"
                                placeholderText: root.lin ? qsTr("number, or bytes: 1, 2, 3") : qsTr("none")
                            }
                            FieldLabel { text: root.lin ? qsTr("Subscribers") : qsTr("Receivers") }
                            SignalField { field: "receivers"; placeholderText: qsTr("Node names, comma separated") }

                            FieldLabel { text: qsTr("Published by"); visible: root.lin }
                            Label {
                                visible: root.lin
                                text: root.signal && root.signal.publisher ? qsTr("%1 (the publisher of the frame)").arg(root.signal.publisher) : ""
                                color: Theme.text
                                Layout.columnSpan: 3
                            }

                            FieldLabel { text: qsTr("Value descriptions"); visible: !root.lin }
                            SignalField {
                                visible: !root.lin
                                field: "choices"
                                placeholderText: qsTr("e.g. 0=Off; 1=On; 3=Error")
                                Layout.columnSpan: 3
                            }

                            FieldLabel { text: qsTr("Comment"); visible: !root.lin }
                            SignalField {
                                visible: !root.lin
                                field: "comment"
                                Layout.columnSpan: 3
                            }

                            FieldLabel {
                                visible: !!(root.signal && root.signal.multiplex)
                                text: qsTr("Multiplexing")
                            }
                            Label {
                                visible: !!(root.signal && root.signal.multiplex)
                                text: root.signal && root.signal.multiplex ? root.signal.multiplex : ""
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

    // LIN: bus and nodes
    ScrollView {
        id: nodesView
        visible: root.editor.open && root.lin && linTabs.currentIndex === 1
        contentWidth: availableWidth
        clip: true
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            width: nodesView.availableWidth
            spacing: Theme.spacing

            Card {
                title: qsTr("Bus")
                Layout.fillWidth: true
                Layout.preferredHeight: busGrid.implicitHeight + 70
                GridLayout {
                    id: busGrid
                    anchors.fill: parent
                    columns: 4
                    columnSpacing: 12
                    rowSpacing: 8

                    FieldLabel { text: qsTr("Speed (bit/s)") }
                    ComboBox {
                        editable: true
                        model: root.editor.lin_speeds
                        editText: String(root.editor.lin_header.speed || "")
                        Layout.fillWidth: true
                        onAccepted: root.apply(root.editor.set_lin_header({ speed: editText }))
                        onActivated: index => root.apply(root.editor.set_lin_header({ speed: currentText }))
                    }
                    FieldLabel { text: qsTr("Channel name") }
                    TextField {
                        text: root.editor.lin_header.channel_name || ""
                        placeholderText: qsTr("optional")
                        Layout.fillWidth: true
                        onEditingFinished: if (text !== root.editor.lin_header.channel_name)
                            root.apply(root.editor.set_lin_header({ channel_name: text }))
                    }
                    FieldLabel { text: qsTr("Protocol version") }
                    TextField {
                        text: root.editor.lin_header.protocol_version || ""
                        Layout.fillWidth: true
                        onEditingFinished: if (text !== root.editor.lin_header.protocol_version)
                            root.apply(root.editor.set_lin_header({ protocol_version: text }), () => text = root.editor.lin_header.protocol_version)
                    }
                    FieldLabel { text: qsTr("Language version") }
                    TextField {
                        text: root.editor.lin_header.language_version || ""
                        Layout.fillWidth: true
                        onEditingFinished: if (text !== root.editor.lin_header.language_version)
                            root.apply(root.editor.set_lin_header({ language_version: text }), () => text = root.editor.lin_header.language_version)
                    }
                    FieldLabel { text: qsTr("Master node") }
                    TextField {
                        text: root.editor.lin_header.master || ""
                        Layout.fillWidth: true
                        onEditingFinished: if (text !== root.editor.lin_header.master)
                            root.apply(root.editor.set_lin_header({ master: text }), () => text = root.editor.lin_header.master)
                    }
                    FieldLabel { text: qsTr("Time base / jitter (ms)") }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            text: root.editor.lin_header.timebase || ""
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.editor.lin_header.timebase)
                                root.apply(root.editor.set_lin_header({ timebase: text }), () => text = root.editor.lin_header.timebase)
                        }
                        TextField {
                            text: root.editor.lin_header.jitter || ""
                            Layout.fillWidth: true
                            onEditingFinished: if (text !== root.editor.lin_header.jitter)
                                root.apply(root.editor.set_lin_header({ jitter: text }), () => text = root.editor.lin_header.jitter)
                        }
                    }
                }
            }

            Card {
                title: qsTr("Slave nodes (%1)").arg(root.editor.slave_nodes.length)
                Layout.fillWidth: true
                Layout.preferredHeight: 120 + root.editor.slave_nodes.length * 46
                headerItems: [
                    TextField {
                        id: newNodeField
                        placeholderText: qsTr("New node name")
                        Layout.preferredWidth: 180
                        onAccepted: addNodeButton.clicked()
                    },
                    Button {
                        id: addNodeButton
                        text: qsTr("Add")
                        enabled: newNodeField.text.trim().length > 0
                        onClicked: if (root.apply(root.editor.add_node(newNodeField.text.trim()))) newNodeField.text = ""
                    }
                ]

                GridLayout {
                    anchors.fill: parent
                    columns: 8
                    columnSpacing: 10
                    rowSpacing: 6

                    Repeater {
                        model: [qsTr("Name"), qsTr("Configured NAD"), qsTr("Initial NAD"), qsTr("Supplier ID"),
                                qsTr("Function ID"), qsTr("Variant"), qsTr("LIN protocol"), ""]
                        FieldLabel { required property string modelData; text: modelData }
                    }
                    Repeater {
                        model: root.editor.slave_nodes
                        delegate: Repeater {
                            id: nodeRow
                            required property var modelData
                            model: ["name", "configured_nad", "initial_nad", "supplier_id", "function_id", "variant", "lin_protocol", ""]
                            Item {
                                required property string modelData
                                implicitHeight: 36
                                implicitWidth: modelData === "" ? 36 : 110
                                Layout.fillWidth: modelData !== ""
                                TextField {
                                    visible: parent.modelData !== ""
                                    anchors.fill: parent
                                    text: parent.modelData ? nodeRow.modelData[parent.modelData] : ""
                                    font.family: parent.modelData === "name" ? Qt.application.font.family : "monospace"
                                    onEditingFinished: {
                                        const key = parent.modelData
                                        if (text === nodeRow.modelData[key])
                                            return
                                        const change = {}
                                        change[key] = text
                                        const error = key === "name" ? root.editor.rename_node(nodeRow.modelData.name, text)
                                                                      : root.editor.update_node(nodeRow.modelData.name, change)
                                        root.apply(error, () => text = nodeRow.modelData[key])
                                    }
                                }
                                ToolButton {
                                    visible: parent.modelData === ""
                                    anchors.centerIn: parent
                                    text: "✕"
                                    ToolTip.visible: hovered
                                    ToolTip.text: qsTr("Remove the node")
                                    onClicked: root.apply(root.editor.remove_node(nodeRow.modelData.name))
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // LIN: encoding types and schedule tables, both edited as LDF text
    component TextItemsView: RowLayout {
        id: itemsView
        required property var items
        required property string heading
        required property string help
        property var addItem
        property var renameItem
        property var removeItem
        property var setText
        property string current: items.length > 0 ? items[0].name : ""
        readonly property var item: items.find(entry => entry.name === current) || null

        spacing: Theme.spacing

        Card {
            title: qsTr("%1 (%2)").arg(itemsView.heading).arg(itemsView.items.length)
            Layout.preferredWidth: 300
            Layout.fillHeight: true
            ColumnLayout {
                anchors.fill: parent
                spacing: 6
                RowLayout {
                    TextField {
                        id: newItemField
                        placeholderText: qsTr("New name")
                        Layout.fillWidth: true
                        onAccepted: addItemButton.clicked()
                    }
                    Button {
                        id: addItemButton
                        text: qsTr("Add")
                        enabled: newItemField.text.trim().length > 0
                        onClicked: {
                            const name = newItemField.text.trim()
                            if (root.apply(itemsView.addItem(name))) {
                                itemsView.current = name
                                newItemField.text = ""
                            }
                        }
                    }
                }
                ListView {
                    clip: true
                    spacing: 4
                    model: itemsView.items
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    delegate: ItemDelegate {
                        required property var modelData
                        width: ListView.view.width
                        text: modelData.name
                        highlighted: modelData.name === itemsView.current
                        onClicked: itemsView.current = modelData.name
                    }
                }
            }
        }

        Card {
            visible: itemsView.item !== null
            title: itemsView.item ? itemsView.item.name : ""
            Layout.fillWidth: true
            Layout.fillHeight: true
            headerItems: [
                Button {
                    text: qsTr("Remove")
                    onClicked: root.apply(itemsView.removeItem(itemsView.current))
                }
            ]
            ColumnLayout {
                anchors.fill: parent
                spacing: 8
                RowLayout {
                    FieldLabel { text: qsTr("Name") }
                    TextField {
                        text: itemsView.item ? itemsView.item.name : ""
                        Layout.fillWidth: true
                        onEditingFinished: {
                            if (!itemsView.item || text === itemsView.item.name)
                                return
                            const name = text
                            if (root.apply(itemsView.renameItem(itemsView.current, name), () => text = itemsView.current))
                                itemsView.current = name
                        }
                    }
                }
                Label {
                    visible: itemsView.item !== null && itemsView.item.signals !== undefined
                    text: itemsView.item && itemsView.item.signals ? qsTr("Used by: %1").arg(itemsView.item.signals) : qsTr("Used by no signal")
                    color: Theme.muted
                    font.pixelSize: 12
                }
                ScrollView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    TextArea {
                        id: itemText
                        text: itemsView.item ? itemsView.item.text : ""
                        font.family: "monospace"
                        wrapMode: TextEdit.NoWrap
                    }
                }
                RowLayout {
                    Label {
                        text: itemsView.help
                        color: Theme.muted
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    Button {
                        text: qsTr("Apply")
                        highlighted: itemsView.item !== null && itemText.text !== itemsView.item.text
                        enabled: highlighted
                        onClicked: root.apply(itemsView.setText(itemsView.current, itemText.text),
                                              () => itemText.text = itemsView.item.text)
                    }
                }
            }
        }
    }

    TextItemsView {
        visible: root.editor.open && root.lin && linTabs.currentIndex === 2
        Layout.fillWidth: true
        Layout.fillHeight: true
        items: root.editor.encodings
        heading: qsTr("Encoding types")
        help: qsTr('One value per line, as in an LDF: logical_value, 0, "off";  physical_value, 1, 254, 0.5, -40, "degC";  bcd_value;  ascii_value;')
        addItem: name => root.editor.add_encoding(name)
        renameItem: (old, name) => root.editor.rename_encoding(old, name)
        removeItem: name => root.editor.remove_encoding(name)
        setText: (name, text) => root.editor.set_encoding_text(name, text)
    }

    TextItemsView {
        visible: root.editor.open && root.lin && linTabs.currentIndex === 3
        Layout.fillWidth: true
        Layout.fillHeight: true
        items: root.editor.schedules
        heading: qsTr("Schedule tables")
        help: qsTr("One entry per line, as in an LDF: SeatStatus delay 10 ms;  MasterReq delay 20 ms;  AssignNAD { SeatECU } delay 20 ms;")
        addItem: name => root.editor.add_schedule(name)
        renameItem: (old, name) => root.editor.rename_schedule(old, name)
        removeItem: name => root.editor.remove_schedule(name)
        setText: (name, text) => root.editor.set_schedule_text(name, text)
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
