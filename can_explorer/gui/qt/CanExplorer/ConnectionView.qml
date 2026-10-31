import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


ColumnLayout {
    id: root
    required property var connection
    spacing: Theme.spacing

    // Views shown in the sidebar; `page` is the key of the current one
    readonly property var pages: [
        { key: "dashboard", title: qsTr("Dashboard"), icon: "▦" },
        { key: "trace", title: qsTr("Trace"), icon: "☰" },
        { key: "overview", title: qsTr("Overview"), icon: "▤" },
        { key: "signals", title: qsTr("Signals"), icon: "∿" },
        { key: "health", title: qsTr("Bus health"), icon: "♥" },
        { key: "reverse", title: qsTr("Reverse engineering"), icon: "⌕" },
    ].concat(connection.can_transmit ? [{ key: "diagnostics", title: qsTr("Diagnostics"), icon: "✚" }] : [])
     .concat(connection.can_transmit && !connection.is_lin && !connection.is_j1850 ? [{ key: "obd", title: qsTr("OBD-II"), icon: "⊙" }] : [])
     .concat(connection.can_transmit ? [{ key: "flash", title: qsTr("Flashing"), icon: "⇪" }] : [])
     .concat(connection.can_transmit ? [{ key: "uds-scan", title: qsTr("UDS discovery"), icon: "⊚" }] : [])
     .concat(connection.can_transmit ? [{ key: "xcp", title: qsTr("XCP/CCP"), icon: "⟐" }] : [])
     .concat(connection.can_transmit ? [{ key: "gmlan", title: qsTr("GMLAN"), icon: "◈" }] : [])
     .concat(connection.is_j1850 ? [{ key: "j1850", title: qsTr("J1850 / Class 2"), icon: "◇" }] : [])
     .concat(connection.protocol === "J1939" ? [{ key: "j1939", title: qsTr("J1939"), icon: "⛟" }] : [])
     .concat(connection.protocol === "CANopen" ? [{ key: "canopen", title: qsTr("CANopen"), icon: "⚙" }] : [])
     .concat(connection.is_lin ? [{ key: "lin", title: qsTr("LIN Schedule"), icon: "⏱" }] : [])
     .concat(connection.can_transmit ? [{ key: "simulation", title: qsTr("Simulation"), icon: "▶" }] : [])
     .concat(connection.can_transmit ? [{ key: "tests", title: qsTr("Tests"), icon: "✓" }] : [])
    readonly property var pageKeys: ["dashboard", "trace", "overview", "signals", "health", "reverse", "diagnostics", "obd", "flash", "uds-scan", "xcp", "gmlan", "j1850", "j1939", "canopen", "lin", "simulation", "tests"]
    property string page: "dashboard"
    readonly property string pageTitle: (pages.find(item => item.key === page) || pages[0]).title
    readonly property bool tablePage: page === "trace" || page === "overview"

    // Table shown on the current page, used by Copy / Select All
    // Bytes that differ from the previous frame of an identifier are shown in this colour in the overview
    Binding {
        target: root.connection.overview
        property: "highlight_color"
        value: String(Theme.warning)
    }

    readonly property var currentTable: page === "trace" ? traceTable : page === "overview" ? overviewTable : null

    function copySelection() {
        return currentTable ? currentTable.copySelection() : 0
    }

    function selectAll() {
        if (currentTable)
            currentTable.selectAll()
    }

    // Playback of a replayed log file
    PlaybackBar {
        visible: root.connection.is_replay && root.connection.replay.active
        playback: root.connection.replay
        restartAction: () => root.connection.open()
        Layout.fillWidth: true
    }

    // Log file transmitted on the bus
    PlaybackBar {
        visible: root.connection.bus_replay.active
        playback: root.connection.bus_replay
        heading: qsTr("Replaying %1 on the bus").arg(root.connection.bus_replay.file_name)
            + (root.connection.bus_replay.loop ? qsTr(" (pass %1)").arg(root.connection.bus_replay.iteration + 1) : "")
        stopAction: () => root.connection.bus_replay.stop()
        restartAction: () => { const error = busReplayDialog.start(); if (error) statusLabel.show(error) }
        dismissAction: () => root.connection.bus_replay.dismiss()
        Layout.fillWidth: true
    }

    RowLayout {
        visible: root.tablePage || root.page === "signals"
        spacing: 8
        Layout.fillWidth: true

        Button {
            text: qsTr("Pause view")
            checkable: true
            checked: root.connection.paused
            onToggled: root.connection.set_paused(checked)
        }
        Button {
            text: qsTr("Clear")
            onClicked: root.connection.clear()
        }
        Button {
            text: root.connection.recording ? qsTr("● Stop recording") : qsTr("Record…")
            highlighted: root.connection.recording
            onClicked: root.connection.recording ? root.connection.stop_recording() : recordDialog.open()
            ToolTip.visible: hovered && root.connection.recording
            ToolTip.text: root.connection.recording_status
        }
        Button {
            visible: root.connection.can_transmit
            text: qsTr("Replay to bus…")
            enabled: root.connection.connected
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Transmit the frames of a log file on this bus with their recorded timing")
            onClicked: busReplayDialog.open()
        }
        Button {
            text: root.connection.mqtt.enabled ? qsTr("● MQTT") : qsTr("MQTT…")
            highlighted: root.connection.mqtt.enabled
            ToolTip.visible: hovered
            ToolTip.text: root.connection.mqtt.enabled
                ? root.connection.mqtt.topic + ": " + root.connection.mqtt.state
                : qsTr("Publish the signals and frames to an MQTT broker")
            onClicked: mqttDialog.open()
        }
        Button {
            text: root.connection.mcp_server.running ? qsTr("● Agent") : qsTr("Agent…")
            highlighted: root.connection.mcp_server.running
            ToolTip.visible: hovered
            ToolTip.text: root.connection.mcp_server.running
                ? root.connection.mcp_server.url + " (" + root.connection.mcp_server.tier + ")"
                : qsTr("Expose this connection to an AI agent over a local MCP server")
            onClicked: mcpServerDialog.open()
        }
        Button {
            text: qsTr("Export…")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Save the selected trace rows, or all displayed rows, to a log file")
            onClicked: exportDialog.open()
        }
        Button {
            text: root.connection.database_file ? qsTr("Unload database") : qsTr("Load database…")
            onClicked: root.connection.database_file ? root.connection.unload_database() : databaseDialog.open()
        }
        Button {
            visible: /\.(dbc|dbf|kcd|sym|arxml|ldf)$/i.test(root.connection.database_file)
            text: qsTr("Edit database")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Open the database in the editor; saving it updates the decoding")
            onClicked: Actions.editDatabaseFile(root.connection.database_file)
        }
        ComboBox {
            id: busBox
            visible: root.connection.database_buses.length > 1
            model: [qsTr("All buses")].concat(root.connection.database_buses)
            currentIndex: Math.max(0, root.connection.database_buses.indexOf(root.connection.database_bus) + 1)
            onActivated: index => root.connection.set_database_bus(index === 0 ? "" : currentText)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("The database describes several buses; decode with the messages of this one")
        }
        CheckBox {
            id: autoScrollBox
            visible: root.page === "trace"
            text: qsTr("Auto-scroll")
            checked: true
        }

        Item { Layout.fillWidth: true }

        Pill {
            visible: root.connection.recording
            text: root.connection.recording_triggered ? root.connection.recording_status : qsTr("● REC")
            tint: root.connection.recording_triggered && root.connection.recording_status.startsWith("armed")
                ? Theme.warning : Theme.danger
        }
        Pill {
            visible: root.connection.database_file.length > 0
            text: root.connection.database_file.split(/[\\/]/).pop()
            tint: Theme.chart[4]
        }
        Pill {
            visible: root.connection.protocol !== "Raw"
            text: root.connection.protocol
            tint: Theme.chart[5]
        }
        Pill {
            text: qsTr("%1 frames").arg(root.connection.frame_count.toLocaleString(Qt.locale(), "f", 0))
            tint: Theme.accent
        }
    }

    StackLayout {
        currentIndex: root.pageKeys.indexOf(root.page)
        Layout.fillWidth: true
        Layout.fillHeight: true

        DashboardView {
            connection: root.connection
        }
        Card {
            padding: 1
            ColumnLayout {
                anchors.fill: parent
                spacing: 0
                TraceFindBar {
                    id: findBar
                    tools: root.connection.trace_tools
                    table: traceTable
                    Layout.fillWidth: true
                    Layout.margins: 8
                }
                FrameTable {
                    id: traceTable
                    model: root.connection.trace
                    columnWidths: [110, 40, 70, 90, 40, 200, 0]
                    autoScroll: autoScrollBox.checked
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    onContextMenuRequested: row => {
                        traceMenu.row = row
                        traceMenu.popup()
                    }
                }
            }
        }
        Card {
            padding: 1
            FrameTable {
                id: overviewTable
                anchors.fill: parent
                model: root.connection.overview
                columnWidths: [90, 40, 70, 80, 90, 40, 200, 0]
                sortable: true
            }
        }
        Card {
            SignalsView {
                anchors.fill: parent
                connection: root.connection
            }
        }
        BusHealthView {
            connection: root.connection
        }
        ReverseView {
            connection: root.connection
            onShowPage: page => root.page = page
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: DiagnosticsView {
                    diagnostics: root.connection.diagnostics
                    connected: root.connection.connected
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit && !root.connection.is_lin && !root.connection.is_j1850
                sourceComponent: ObdView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: FlashView {
                    flashing: root.connection.flashing
                    diagnostics: root.connection.diagnostics
                    connected: root.connection.connected
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: UdsScanView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: XcpView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: GmlanView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.is_j1850
                sourceComponent: J1850View {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.protocol === "J1939"
                sourceComponent: J1939View {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.protocol === "CANopen"
                sourceComponent: CanOpenView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.is_lin
                sourceComponent: LinScheduleView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: SimulationView {
                    connection: root.connection
                }
            }
        }
        Loader {
            active: root.connection.can_transmit
            sourceComponent: TestsView {
                connection: root.connection
            }
        }
    }

    TransmitPanel {
        connection: root.connection
        visible: root.connection.can_transmit && root.tablePage
        Layout.fillWidth: true
        Layout.preferredHeight: implicitHeight
    }

    Label {
        id: statusLabel
        visible: text.length > 0
        color: Theme.muted
        Timer {
            id: statusTimer
            interval: 4000
            onTriggered: statusLabel.text = ""
        }
        function show(message) {
            text = message
            statusTimer.restart()
        }
    }

    BusReplayDialog {
        id: busReplayDialog
        connection: root.connection
    }

    RecordDialog {
        id: recordDialog
        connection: root.connection
    }

    MqttDialog {
        id: mqttDialog
        connection: root.connection
    }

    McpServerDialog {
        id: mcpServerDialog
        objectName: "mcpServerDialog"
        connection: root.connection
    }

    Connections {
        target: root.connection
        function onRecording_event(text) {
            statusLabel.show(text)
        }
    }

    // Context menu of the trace rows
    Menu {
        id: traceMenu
        property int row: -1
        MenuItem {
            text: qsTr("Bookmark / remove bookmark\tCtrl+B")
            onTriggered: findBar.toggleBookmarks()
        }
        MenuItem {
            text: qsTr("Copy selected rows\tCtrl+C")
            onTriggered: traceTable.copySelection()
        }
        MenuItem {
            visible: root.connection.is_replay
            height: visible ? implicitHeight : 0
            text: qsTr("Replay from here")
            onTriggered: root.connection.replay_from_row(traceMenu.row)
        }
        MenuItem {
            text: qsTr("Find…\tCtrl+F")
            onTriggered: findBar.focusSearch()
        }
    }

    // Shortcuts of the trace; only the visible connection reacts
    Shortcut {
        sequence: StandardKey.Find
        enabled: root.visible && root.page === "trace"
        onActivated: findBar.focusSearch()
    }
    Shortcut {
        sequences: [StandardKey.FindNext, "F3"]
        enabled: root.visible && root.page === "trace"
        onActivated: findBar.find(true)
    }
    Shortcut {
        sequences: [StandardKey.FindPrevious, "Shift+F3"]
        enabled: root.visible && root.page === "trace"
        onActivated: findBar.find(false)
    }
    Shortcut {
        sequence: "Ctrl+B"
        enabled: root.visible && root.page === "trace"
        onActivated: findBar.toggleBookmarks()
    }

    FileDialog {
        id: exportDialog
        title: qsTr("Export trace")
        fileMode: FileDialog.SaveFile
        nameFilters: connectionManager.log_file_filters.slice(1)
        // The suffix of the chosen format when none is typed (e.g. "asc.gz")
        defaultSuffix: selectedNameFilter.extensions.length > 0 ? selectedNameFilter.extensions[0] : "asc"
        onAccepted: {
            const selection = traceTable.selectionModel.hasSelection ? traceTable.selectionModel : null
            statusLabel.show(root.connection.export_trace(selectedFile, selection))
        }
    }

    FileDialog {
        id: databaseDialog
        title: qsTr("Load signal database")
        fileMode: FileDialog.OpenFile
        nameFilters: connectionManager.database_file_filters
        onAccepted: root.connection.load_database(selectedFile)
    }
}
