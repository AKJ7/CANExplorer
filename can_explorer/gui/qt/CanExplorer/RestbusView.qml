import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Restbus simulation of one connection: transmit its database messages the way their nodes would, so the ECU under
// test sees the rest of the bus. Choose which messages to send and how (cyclic, on change, if active), exclude the
// node you are testing, edit the sent values, and make signals count or carry a checksum.
//
// The lists' models are the message and signal names (they change only with the database); each row reads its data
// through rb.revision, so an edit updates the rows in place instead of rebuilding the list under the handler.
ColumnLayout {
    id: root
    required property var connection
    readonly property var rb: connection.restbus
    // The expanded messages, by name (kept here: the rows are delegates and may be recreated when scrolled)
    property var expanded: ({})
    readonly property var modeNames: ({
        "cyclic": qsTr("Cyclic"),
        "on_change": qsTr("On change"),
        "cyclic_on_change": qsTr("Cyclic + on change"),
        "if_active": qsTr("If active")
    })
    readonly property var modeChoices: rb.modes.map(mode => ({ mode: mode.mode, name: modeNames[mode.mode] || mode.name }))

    spacing: 8

    function toggleExpanded(name) {
        const copy = Object.assign({}, expanded)
        copy[name] = !copy[name]
        expanded = copy
    }

    function stateColor(state) {
        switch (state) {
        case "sending": return Theme.success
        case "stopped": return Theme.danger
        case "inactive":
        case "on change": return Theme.warning
        default: return Theme.muted
        }
    }

    function stateName(state) {
        switch (state) {
        case "sending": return qsTr("sending")
        case "stopped": return qsTr("stopped")
        case "inactive": return qsTr("inactive")
        case "on change": return qsTr("on change")
        case "ready": return qsTr("ready")
        default: return ""
        }
    }

    // Controls and configuration
    RowLayout {
        Layout.fillWidth: true
        Layout.leftMargin: 12
        Layout.rightMargin: 12
        Layout.topMargin: 12
        spacing: 10
        enabled: root.rb.has_database

        Label { text: qsTr("Default cycle"); color: Theme.muted }
        SpinBox {
            id: cycleBox
            from: 1
            to: 60000
            stepSize: 10
            value: root.rb.default_cycle_ms
            enabled: !root.rb.running
            editable: true
            Layout.preferredWidth: 120
            onValueModified: root.rb.set_default_cycle(value)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Period (ms) for messages without a cycle time in the database")
        }

        ToolSeparator {}

        Button { text: qsTr("All"); enabled: !root.rb.running; onClicked: root.rb.set_all(true) }
        Button { text: qsTr("None"); enabled: !root.rb.running; onClicked: root.rb.set_all(false) }
        Button {
            text: qsTr("Cyclic only")
            enabled: !root.rb.running
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Enable only the messages that have a cycle time")
            onClicked: root.rb.enable_cyclic_only()
        }

        ToolSeparator {}

        Label { text: qsTr("Exclude"); color: Theme.muted }
        ComboBox {
            id: nodeBox
            Layout.preferredWidth: 130
            enabled: !root.rb.running && root.rb.nodes.length > 0
            model: root.rb.nodes
        }
        Button {
            text: qsTr("Exclude node")
            enabled: !root.rb.running && root.rb.nodes.length > 0
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Stop sending the messages of the ECU you are testing")
            onClicked: if (nodeBox.currentText) statusLabel.show(qsTr("%1 message(s) disabled").arg(root.rb.exclude_node(nodeBox.currentText)))
        }

        Item { Layout.fillWidth: true }

        Pill {
            visible: root.rb.has_database
            text: qsTr("%1 / %2 on").arg(root.rb.enabled_count).arg(root.rb.message_count)
            tint: Theme.accent
        }
        Pill {
            visible: root.rb.problem_count > 0
            text: qsTr("%1 stopped").arg(root.rb.problem_count)
            tint: Theme.danger
        }
        Button {
            text: root.rb.running ? qsTr("Stop") : qsTr("Start")
            highlighted: !root.rb.running
            enabled: root.rb.has_database && (root.rb.running || root.rb.can_start)
            onClicked: {
                if (root.rb.running)
                    root.rb.stop()
                else {
                    const error = root.rb.start()
                    if (error)
                        statusLabel.show(error)
                }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.leftMargin: 12
        Layout.rightMargin: 12
        spacing: 16
        visible: root.rb.has_database

        CheckBox {
            text: qsTr("Hardware timing")
            checked: root.rb.hardware_timing
            onToggled: root.rb.set_hardware_timing(checked)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Time the frames that never change with the interface's own periodic transmission, where it has one (less jitter). Frames that change every time — counters, checksums, E2E, SecOC, rotated multiplexer pages — are always timed by CANExplorer.")
        }
        CheckBox {
            text: qsTr("Raw values")
            checked: root.rb.raw_values
            onToggled: root.rb.set_raw_values(checked)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Show and enter signal values as raw (unscaled) numbers instead of physical values")
        }
        Item { Layout.fillWidth: true }
        Label {
            readonly property real load: root.rb.expected_load
            visible: load >= 0
            text: qsTr("Expected bus load %1 %").arg(load.toFixed(1))
            color: load > 70 ? Theme.danger : load > 50 ? Theme.warning : Theme.muted
            ToolTip.visible: loadHover.hovered
            ToolTip.text: qsTr("The enabled periodic messages at the connection's bitrate, without bit stuffing (which adds up to about 20 %). If-active messages count at their active period, on-change ones not at all.")
            HoverHandler { id: loadHover }
        }
    }

    Label {
        id: statusLabel
        visible: text.length > 0
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 12
        Layout.rightMargin: 12
        Timer { id: statusTimer; interval: 6000; onTriggered: statusLabel.text = "" }
        function show(message) { text = message; statusTimer.restart() }
    }

    // Content: the message table, or a centred message when there is no database
    Item {
        Layout.fillWidth: true
        Layout.fillHeight: true

        Label {
            anchors.centerIn: parent
            width: parent.width * 0.7
            visible: !root.rb.has_database
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            color: Theme.muted
            text: qsTr("Load a signal database (DBC/ARXML/…) on this connection to simulate its messages.")
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 12
            anchors.topMargin: 0
            visible: root.rb.has_database
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                Layout.rightMargin: 12
                spacing: 12
                Label { text: qsTr("Send"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 44 }
                Label { text: qsTr("ID"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 90 }
                Label { text: qsTr("Message"); color: Theme.muted; font.bold: true; Layout.fillWidth: true }
                Label { text: qsTr("Sender"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 110 }
                Label { text: qsTr("Mode"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 150 }
                Label { text: qsTr("Cycle"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 70 }
                Label { text: qsTr("State"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 80 }
                Label { text: qsTr("Signals"); color: Theme.muted; font.bold: true; Layout.preferredWidth: 60 }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.alpha(Theme.border, 0.6) }

            ListView {
                id: list
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: root.rb.message_names
                ScrollBar.vertical: ScrollBar {}

                delegate: Column {
                    id: messageRow
                    required property string modelData
                    readonly property string name: modelData
                    readonly property var row: (root.rb.revision, root.rb.message_row(name))
                    readonly property bool open: root.expanded[name] === true
                    width: ListView.view.width

                    RowLayout {
                        width: parent.width
                        height: 34
                        spacing: 12
                        CheckBox {
                            Layout.preferredWidth: 44
                            checked: messageRow.row.enabled === true
                            onToggled: {
                                const name = messageRow.name, on = checked
                                Qt.callLater(() => root.rb.set_enabled(name, on))
                            }
                            ToolTip.visible: hovered && messageRow.row.sent_by_nodes === false
                            ToolTip.text: qsTr("The database marks this message as not sent by its node (NoMsgSendType)")
                        }
                        Label {
                            text: (messageRow.row.id || "") + (messageRow.row.extended ? "x" : "")
                            color: Theme.text
                            font.family: "monospace"
                            Layout.preferredWidth: 90
                        }
                        RowLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Label { text: messageRow.name; color: Theme.text; elide: Text.ElideRight; Layout.fillWidth: true }
                            // Badges: what is computed for every frame, and the frame format
                            Label {
                                visible: messageRow.row.secured === true
                                text: "SecOC"
                                color: messageRow.row.problem ? Theme.danger : Theme.success
                                font.pixelSize: 11
                                ToolTip.visible: secocHover.hovered
                                ToolTip.text: messageRow.row.problem
                                    ? qsTr("Not sent: %1").arg(messageRow.row.problem)
                                    : qsTr("Secured for every frame: a new freshness value and MAC, with the key from the Security Manager")
                                HoverHandler { id: secocHover }
                            }
                            Label {
                                visible: (messageRow.row.e2e || "") !== ""
                                text: messageRow.row.e2e || ""
                                color: messageRow.row.note ? Theme.warning : Theme.success
                                font.pixelSize: 11
                                ToolTip.visible: e2eHover.hovered
                                ToolTip.text: messageRow.row.note || qsTr("Protected for every frame: a new E2E counter and CRC, from the ARXML's E2E configuration")
                                HoverHandler { id: e2eHover }
                            }
                            Label {
                                visible: (messageRow.row.computed || []).length > 0
                                text: qsTr("counted")
                                color: Theme.success
                                font.pixelSize: 11
                                ToolTip.visible: computedHover.hovered
                                ToolTip.text: qsTr("Computed for every frame: %1").arg((messageRow.row.computed || []).join(", "))
                                HoverHandler { id: computedHover }
                            }
                            Label {
                                visible: messageRow.row.fd === true
                                text: messageRow.row.brs ? "FD·BRS" : "FD"
                                color: Theme.muted
                                font.pixelSize: 11
                            }
                            Label {
                                visible: messageRow.row.hardware_timed === true
                                text: "HW"
                                color: Theme.muted
                                font.pixelSize: 11
                                ToolTip.visible: hwHover.hovered
                                ToolTip.text: qsTr("Timed by the interface")
                                HoverHandler { id: hwHover }
                            }
                        }
                        Label { text: messageRow.row.senders || ""; color: Theme.muted; elide: Text.ElideRight; Layout.preferredWidth: 110 }
                        ComboBox {
                            Layout.preferredWidth: 150
                            model: root.modeChoices
                            textRole: "name"
                            valueRole: "mode"
                            currentIndex: Math.max(0, root.modeChoices.findIndex(mode => mode.mode === messageRow.row.mode))
                            onActivated: {
                                const name = messageRow.name, mode = currentValue
                                Qt.callLater(() => root.rb.set_mode(name, mode))
                            }
                            ToolTip.visible: hovered
                            ToolTip.text: messageRow.row.mode === messageRow.row.database_mode
                                ? qsTr("The send mode of the database")
                                : qsTr("Changed from the database's send mode")
                        }
                        Label {
                            text: messageRow.row.mode === "on_change" ? qsTr("event")
                                : (messageRow.row.period_ms + " ms" + (messageRow.row.cycle_ms > 0 ? "" : "*"))
                            color: Theme.muted
                            Layout.preferredWidth: 70
                            ToolTip.visible: cycleHover.hovered && messageRow.row.cycle_ms === 0 && messageRow.row.mode !== "on_change"
                            ToolTip.text: qsTr("No cycle time in the database: sent at the default cycle")
                            HoverHandler { id: cycleHover }
                        }
                        Label {
                            text: root.stateName(messageRow.row.state)
                            color: root.stateColor(messageRow.row.state)
                            elide: Text.ElideRight
                            Layout.preferredWidth: 80
                            ToolTip.visible: stateHover.hovered && (messageRow.row.problem || "") !== ""
                            ToolTip.text: messageRow.row.problem || ""
                            HoverHandler { id: stateHover }
                        }
                        Label {
                            text: messageRow.row.signal_count > 0 ? (messageRow.open ? "▾ " : "▸ ") + messageRow.row.signal_count : "–"
                            color: messageRow.row.signal_count > 0 ? Theme.accent : Theme.muted
                            Layout.preferredWidth: 60
                            MouseArea {
                                anchors.fill: parent
                                enabled: messageRow.row.signal_count > 0
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.toggleExpanded(messageRow.name)
                            }
                        }
                    }

                    // Expanded: send now, multiplexer pages, and the signals with their values and roles
                    Column {
                        x: 56
                        width: parent.width - 56
                        visible: messageRow.open
                        spacing: 6
                        bottomPadding: 10

                        Label {
                            visible: (messageRow.row.problem || "") !== ""
                            text: qsTr("Not sent: %1").arg(messageRow.row.problem || "")
                            color: Theme.danger
                            wrapMode: Text.Wrap
                            width: parent.width
                        }

                        RowLayout {
                            spacing: 8
                            Button {
                                text: qsTr("Send now")
                                enabled: root.rb.can_start
                                ToolTip.visible: hovered
                                ToolTip.text: qsTr("Send this message at once, with its repetitions — as an on-change message does when a value changes")
                                onClicked: {
                                    const error = root.rb.send_now(messageRow.name)
                                    if (error)
                                        statusLabel.show(error)
                                }
                            }
                            Label {
                                visible: (messageRow.row.pages || []).length > 0
                                text: qsTr("Multiplexer pages")
                                color: Theme.muted
                                Layout.leftMargin: 12
                            }
                            Repeater {
                                model: messageRow.open ? (messageRow.row.pages || []) : []
                                delegate: CheckBox {
                                    required property var modelData
                                    text: String(modelData)
                                    checked: (messageRow.row.selected_pages || []).indexOf(modelData) >= 0
                                    onToggled: {
                                        const selected = (messageRow.row.selected_pages || []).filter(page => page !== modelData)
                                        if (checked)
                                            selected.push(modelData)
                                        const name = messageRow.name
                                        Qt.callLater(() => root.rb.set_pages(name, selected))
                                    }
                                    ToolTip.visible: hovered
                                    ToolTip.text: qsTr("Send this page; the selected pages take turns, one per frame")
                                }
                            }
                        }

                        Flow {
                            width: parent.width
                            spacing: 18
                            Repeater {
                                model: messageRow.open ? root.rb.signal_names(messageRow.name) : []
                                delegate: RowLayout {
                                    id: signalRow
                                    required property string modelData
                                    readonly property var sig: (root.rb.revision, root.rb.signal_row(messageRow.name, modelData))
                                    readonly property bool editable: sig.role === "value" && !sig.computed
                                    spacing: 6

                                    Label {
                                        text: signalRow.modelData + (signalRow.sig.page ? " (" + qsTr("page %1").arg(signalRow.sig.page) + ")" : "")
                                              + (signalRow.sig.unit ? " [" + signalRow.sig.unit + "]" : "") + ":"
                                        color: Theme.muted
                                    }
                                    // A value table: pick a choice by name
                                    ComboBox {
                                        visible: signalRow.editable && (signalRow.sig.choices || []).length > 0 && !root.rb.raw_values
                                        model: signalRow.sig.choices || []
                                        currentIndex: Math.max(0, (signalRow.sig.choices || []).indexOf(signalRow.sig.value))
                                        Layout.preferredWidth: 120
                                        onActivated: {
                                            const message = messageRow.name, signal = signalRow.modelData, choice = currentText
                                            Qt.callLater(() => root.rb.set_value(message, signal, choice))
                                        }
                                    }
                                    TextField {
                                        visible: !((signalRow.sig.choices || []).length > 0 && !root.rb.raw_values) || !signalRow.editable
                                        enabled: signalRow.editable
                                        text: signalRow.sig.computed ? qsTr("rotated")
                                            : signalRow.sig.role === "counter" ? qsTr("counts")
                                            : signalRow.sig.role === "checksum" ? qsTr("computed")
                                            : (signalRow.sig.value || "")
                                        Layout.preferredWidth: 80
                                        selectByMouse: true
                                        ToolTip.visible: hovered && (signalRow.sig.minimum !== "" || signalRow.sig.maximum !== "")
                                        ToolTip.text: qsTr("Range %1 … %2").arg(signalRow.sig.minimum).arg(signalRow.sig.maximum)
                                        onEditingFinished: {
                                            const message = messageRow.name, signal = signalRow.modelData, value = text
                                            Qt.callLater(() => root.rb.set_value(message, signal, value))
                                        }
                                    }
                                    // What the signal is: a value as set, a counter, or a checksum
                                    ComboBox {
                                        id: roleBox
                                        visible: signalRow.sig.can_count === true
                                        Layout.preferredWidth: 110
                                        model: signalRow.sig.can_checksum
                                            ? [qsTr("Value"), qsTr("Counter"), qsTr("Checksum")]
                                            : [qsTr("Value"), qsTr("Counter")]
                                        readonly property var roles: ["value", "counter", "checksum"]
                                        currentIndex: Math.max(0, roles.indexOf(signalRow.sig.role))
                                        onActivated: index => {
                                            const message = messageRow.name, signal = signalRow.modelData
                                            const role = roles[index], algorithm = signalRow.sig.algorithm
                                            const constant = "0x" + Number(signalRow.sig.constant || 0).toString(16)
                                            Qt.callLater(() => {
                                                const error = root.rb.set_role(message, signal, role, algorithm, constant)
                                                if (error)
                                                    statusLabel.show(error)
                                            })
                                        }
                                        ToolTip.visible: hovered
                                        ToolTip.text: qsTr("Counter: counts up by one per frame and wraps at its maximum. Checksum: computed over the other bytes of the frame for every frame.")
                                    }
                                    ComboBox {
                                        visible: signalRow.sig.role === "checksum"
                                        Layout.preferredWidth: 200
                                        model: root.rb.checksum_algorithms
                                        textRole: "name"
                                        valueRole: "algorithm"
                                        currentIndex: Math.max(0, root.rb.checksum_algorithms.findIndex(a => a.algorithm === signalRow.sig.algorithm))
                                        onActivated: {
                                            const message = messageRow.name, signal = signalRow.modelData, algorithm = currentValue
                                            const constant = "0x" + Number(signalRow.sig.constant || 0).toString(16)
                                            Qt.callLater(() => root.rb.set_role(message, signal, "checksum", algorithm, constant))
                                        }
                                    }
                                    TextField {
                                        visible: signalRow.sig.role === "checksum"
                                        text: "0x" + Number(signalRow.sig.constant || 0).toString(16).toUpperCase().padStart(2, "0")
                                        Layout.preferredWidth: 60
                                        selectByMouse: true
                                        ToolTip.visible: hovered
                                        ToolTip.text: qsTr("A constant folded into the checksum (XORed into a CRC or XOR, added to a sum); 0 for the plain algorithm")
                                        onEditingFinished: {
                                            const message = messageRow.name, signal = signalRow.modelData
                                            const algorithm = signalRow.sig.algorithm, constant = text
                                            Qt.callLater(() => {
                                                const error = root.rb.set_role(message, signal, "checksum", algorithm, constant)
                                                if (error)
                                                    statusLabel.show(error)
                                            })
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
}
