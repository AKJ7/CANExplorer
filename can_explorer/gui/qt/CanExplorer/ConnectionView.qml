import QtQuick 2.15
import QtQuick.Controls 2.15
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
        { key: "security", title: qsTr("Security events"), icon: "⛨" },
        { key: "reverse", title: qsTr("Reverse engineering"), icon: "⌕" },
        { key: "media", title: qsTr("Video & map"), icon: "◉" },
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
     .concat(connection.can_transmit ? [{ key: "restbus", title: qsTr("Restbus"), icon: "⤭" }] : [])
     .concat(connection.can_transmit ? [{ key: "tests", title: qsTr("Tests"), icon: "✓" }] : [])
    // What each view is doing, for the mark after it in the sidebar: { key: { status, text } } with status "ok"
    // (running), "warning" or "error" (StatusDot). Kept apart from `pages`, so a change does not rebuild the list.
    readonly property var pageActivity: {
        const activity = {}
        const mark = (key, status, text) => { activity[key] = { status: status, text: text } }
        const health = connection.bus_health ? connection.bus_health.state : ""
        if (health === "bus-off")
            mark("health", "error", qsTr("Bus off"))
        else if (health === "passive" || health === "warning")
            mark("health", "warning", health === "passive" ? qsTr("Error passive") : qsTr("Error warning"))
        const events = connection.security_event_counts || {}
        if ((events.high || 0) > 0)
            mark("security", "error", qsTr("%1 high-severity events").arg(events.high))
        else if ((events.events || 0) > 0)
            mark("security", "warning", qsTr("%1 events").arg(events.events))
        if (connection.media && connection.media.camera_recording)
            mark("media", "ok", qsTr("Recording the camera"))
        if (connection.diagnostics && connection.diagnostics.tester_present)
            mark("diagnostics", "ok", qsTr("Tester present"))
        if (connection.obd && connection.obd.live)
            mark("obd", "ok", qsTr("Live values"))
        if (connection.flashing && connection.flashing.running)
            mark("flash", "ok", qsTr("Flashing"))
        else if (connection.flashing && connection.flashing.state === "failed")
            mark("flash", "error", qsTr("Flashing failed"))
        if (connection.uds_scan && connection.uds_scan.running)
            mark("uds-scan", "ok", qsTr("Scanning"))
        else if (connection.uds_scan && connection.uds_scan.state === "failed")
            mark("uds-scan", "error", qsTr("Scan failed"))
        if (connection.xcp && (connection.xcp.polling || connection.xcp.connected))
            mark("xcp", "ok", connection.xcp.polling ? qsTr("Measuring") : qsTr("Connected to the ECU"))
        if (connection.gmlan && connection.gmlan.tester_present)
            mark("gmlan", "ok", qsTr("Tester present"))
        if (connection.schedule_running)
            mark("lin", "ok", qsTr("Schedule running"))
        if (connection.simulation && connection.simulation.running)
            mark("simulation", "ok", qsTr("Simulation running"))
        if (connection.restbus && connection.restbus.running)
            mark("restbus", "ok", qsTr("Restbus running"))
        const tests = connection.tests
        if (tests && tests.running)
            mark("tests", "ok", qsTr("Running %1").arg(tests.running_case))
        else if (tests && (tests.summary.verdict === "failed" || tests.summary.verdict === "error"))
            mark("tests", "error", qsTr("Last run failed"))
        return activity
    }
    readonly property var pageKeys: ["dashboard", "trace", "overview", "signals", "health", "reverse", "diagnostics", "obd", "flash", "uds-scan", "xcp", "gmlan", "j1850", "j1939", "canopen", "lin", "simulation", "restbus", "tests", "media", "security"]
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
    // The normal text colour, so a changed byte can fade from the highlight colour back to it
    Binding {
        target: root.connection.overview
        property: "text_color"
        value: String(Theme.text)
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
        // AUTOSAR E2E protections of an ARXML database: their CRC is checked in the trace
        Label {
            readonly property var protections: root.connection.e2e_protections
            readonly property int checkedCount: protections.filter(row => row.checked).length
            visible: protections.length > 0
            text: qsTr("E2E: %1 of %2 checked").arg(checkedCount).arg(protections.length)
            color: checkedCount < protections.length ? Theme.warning : Theme.muted
            ToolTip.visible: e2eHover.hovered
            ToolTip.text: qsTr("AUTOSAR E2E protections from the database; the trace shows the CRC check of each frame:")
                + "\n" + protections.map(row => row.message + ": " + row.profile
                    + (row.checked ? "" : " — " + qsTr("not checked") + " (" + row.reason + ")")).join("\n")
            HoverHandler { id: e2eHover }
        }
        // AUTOSAR SecOC secured PDUs of an ARXML database: their MAC and freshness are checked in the trace
        Label {
            readonly property var protections: root.connection.secoc_protections
            readonly property var statistics: root.connection.secoc_statistics
            readonly property int checkedCount: protections.filter(row => row.checked).length
            // Frames that failed verification (MAC or freshness) since the connection started
            readonly property int failures: Object.values(statistics).reduce(
                (sum, row) => sum + row.mac_errors + row.freshness_errors, 0)
            function counts(name) {
                const row = statistics[name]
                return row ? " — " + qsTr("%1 ok, %2 MAC errors, %3 freshness errors, %4 sent")
                    .arg(row.ok).arg(row.mac_errors).arg(row.freshness_errors).arg(row.sent) : ""
            }
            visible: protections.length > 0
            text: qsTr("SecOC: %1 of %2 checked").arg(checkedCount).arg(protections.length)
                + (failures > 0 ? " · " + qsTr("%1 failed").arg(failures) : "")
            color: failures > 0 ? Theme.danger : checkedCount < protections.length ? Theme.warning : Theme.muted
            ToolTip.visible: secocHover.hovered
            ToolTip.text: qsTr("AUTOSAR SecOC secured PDUs from the database; the trace shows the MAC and freshness check of each frame. Keys come from the Security Manager:")
                + "\n" + protections.map(row => row.message + " (Data ID " + row.data_id + "): "
                    + (row.checked ? qsTr("key %1").arg(row.key) : qsTr("not checked") + " (" + row.reason + ")")
                    + counts(row.message)).join("\n")
            HoverHandler { id: secocHover }
            TapHandler { onTapped: Actions.securityManager.trigger() }
        }
        CheckBox {
            id: autoScrollBox
            visible: root.page === "trace"
            text: qsTr("Auto-scroll")
            checked: true
        }
        TimeFormatBox {
            id: timeFormatBox
            visible: root.page === "trace"
        }
        CheckBox {
            id: fadeBox
            visible: root.page === "overview"
            text: qsTr("Fade changes")
            checked: root.connection.overview.fade
            onToggled: root.connection.overview.set_fade(checked)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Changed bytes glow and cool over a second, so you can watch which bytes are active")
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
                    columnWidths: [timeFormatBox.columnWidth, 40, 70, 90, 40, 200, 0]
                    autoScroll: autoScrollBox.checked
                    onAutoScrollInterrupted: autoScrollBox.checked = false
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
                onContextMenuRequested: overviewMenu.popup()
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
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: RestbusView {
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
        // Loaded when shown: the video player and camera preview cost nothing otherwise. Loaded by URL, not as an
        // inline type, so a missing QtMultimedia backend (e.g. no libpulse on Linux) only breaks this page.
        Loader {
            readonly property bool shown: root.page === "media"
            function sync() {
                if (shown)
                    setSource(Qt.resolvedUrl("MediaView.qml"), { connection: root.connection });
                else
                    setSource("");
            }
            onShownChanged: sync()
            Component.onCompleted: sync()
        }
        SecurityEventsView {
            connection: root.connection
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

    // Context menu of the overview rows
    Menu {
        id: overviewMenu
        MenuItem {
            text: qsTr("Copy selected rows\tCtrl+C")
            onTriggered: overviewTable.copySelection()
        }
        MenuItem {
            text: qsTr("Select all\tCtrl+A")
            onTriggered: overviewTable.selectAll()
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

    FileChooser {
        id: exportDialog
        title: qsTr("Export trace")
        fileMode: FileChooser.SaveFile
        nameFilters: connectionManager.log_file_filters.slice(1)
        // When none is typed, the suffix of the chosen format (e.g. "asc.gz"), else this one
        defaultSuffix: "asc"
        onAccepted: {
            const selection = traceTable.selectionModel.hasSelection ? traceTable.selectionModel : null
            statusLabel.show(root.connection.export_trace(selectedFile, selection))
        }
    }

    FileChooser {
        id: databaseDialog
        title: qsTr("Load signal database")
        fileMode: FileChooser.OpenFile
        nameFilters: connectionManager.database_file_filters
        onAccepted: root.connection.load_database(selectedFile)
    }
}
