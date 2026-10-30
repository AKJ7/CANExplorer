import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import CanExplorer 1.0


ApplicationWindow {
    visible: true
    flags: Qt.Window | Qt.FramelessWindowHint
    width: 1200
    height: 750
    id: mainWindow
    title: connectionManager.workspace_name ? `${connectionManager.workspace_name} — ${metadata.name}` : metadata.name

    readonly property int currentIndex: Math.min(selectedIndex, connectionManager.connections.length - 1)
    property int selectedIndex: -1
    readonly property var currentConnection: currentIndex >= 0 ? connectionManager.connections[currentIndex] : null
    readonly property var currentConnectionView: currentIndex >= 0 ? connectionViews.itemAt(currentIndex) : null
    property var notifications: []
    // A tool shown instead of the connections: "", "database-editor", "multi-bus", "compare", "ethercat", "physical-io",
    // "doip", "someip" or "gateway"
    property string tool: ""
    property bool closeConfirmed: false
    // Dragged with the handle on the right edge of the sidebar
    property int sidebarWidth: Theme.sidebarWidth
    readonly property int maximumSidebarWidth: Math.min(connectionManager.app_state.max_sidebar_width, width / 2)

    function currentView() {
        return tool === "" ? currentConnectionView : tool === "multi-bus" ? multiBusView : null
    }

    // Everything the command palette offers, for the current state of the window
    function paletteCommands() {
        const commands = []
        const add = (category, title, run, shortcut, enabled) =>
            commands.push({ category: category, title: title, run: run, shortcut: shortcut || "", enabled: enabled !== false })
        const label = action => action.text.replace("&", "").replace("...", "…")
        const actions = [
            [Actions.newConnection, "Ctrl+N"], [Actions.openLogFile, "Ctrl+O"], [Actions.closeConnection, "Ctrl+W"],
            [Actions.openWorkspace, "Ctrl+Shift+O"], [Actions.saveWorkspace, "Ctrl+S"],
            [Actions.saveWorkspaceAs, "Ctrl+Shift+S"], [Actions.editDatabase, ""], [Actions.convertDatabase, ""],
            [Actions.copy, "Ctrl+C"], [Actions.selectAll, "Ctrl+A"], [Actions.preferences, ""],
            [Actions.checkForUpdates, ""], [Actions.about, ""], [Actions.exit, "Ctrl+Q"],
        ]
        for (const [action, shortcut] of actions)
            add(qsTr("Command"), label(action), () => action.trigger(), shortcut, action.enabled)
        const connection = mainWindow.currentConnection
        const view = mainWindow.currentConnectionView
        if (connection && view) {
            for (const page of view.pages)
                add(qsTr("View"), page.title, () => { view.page = page.key; mainWindow.tool = "" })
            add(qsTr("Connection"), connection.connected ? qsTr("Disconnect %1").arg(connection.name)
                                                        : qsTr("Connect %1").arg(connection.name),
                () => connection.connected ? connection.close() : connection.open())
            add(qsTr("Connection"), connection.paused ? qsTr("Resume the trace") : qsTr("Pause the trace"),
                () => connection.set_paused(!connection.paused))
            add(qsTr("Connection"), qsTr("Clear the trace and statistics"), () => connection.clear())
        }
        connectionManager.connections.forEach((item, index) =>
            add(qsTr("Go to"), qsTr("Connection %1").arg(item.name), () => { mainWindow.selectedIndex = index; mainWindow.tool = "" }))
        for (const [key, title] of [["database-editor", qsTr("Database editor")], ["multi-bus", qsTr("Multi-bus trace")],
                                    ["compare", qsTr("Compare recordings")], ["ethercat", qsTr("EtherCAT (CoE)")],
                                    ["physical-io", qsTr("Physical I/O")], ["doip", qsTr("DoIP")],
                                    ["someip", qsTr("SOME/IP")], ["gateway", qsTr("Bus gateway")]])
            add(qsTr("Tool"), title, () => mainWindow.tool = key)
        for (const theme of connectionManager.app_state.themes)
            add(qsTr("Theme"), theme, () => connectionManager.app_state.set_theme(theme))
        for (const language of connectionManager.app_state.languages)
            add(qsTr("Language"), language.name, () => connectionManager.app_state.set_language(language.code))
        return commands
    }

    function notify(text) {
        notifications = notifications.concat([{ time: new Date().toLocaleTimeString(Qt.locale(), "HH:mm:ss"), text: text }])
        toast.show(text)
    }

    Binding {
        target: Theme
        property: "name"
        value: connectionManager.app_state.theme
    }

    color: Theme.background
    palette.window: Theme.background
    palette.windowText: Theme.text
    palette.base: Theme.cardAlt
    palette.alternateBase: Theme.card
    palette.text: Theme.text
    palette.button: Theme.card
    palette.buttonText: Theme.text
    palette.highlight: Theme.accent
    palette.highlightedText: "white"
    palette.light: Qt.lighter(Theme.card, 1.3)
    palette.midlight: Qt.lighter(Theme.card, 1.15)
    palette.mid: Theme.border
    palette.dark: Theme.cardAlt
    palette.shadow: "black"
    palette.toolTipBase: Theme.card
    palette.toolTipText: Theme.text
    palette.placeholderText: Theme.muted
    palette.brightText: "white"
    palette.link: Theme.accent

    Component.onCompleted: {
        sidebarWidth = connectionManager.app_state.sidebar_width()
        const geometry = connectionManager.app_state.window_geometry()
        if (geometry.length === 4) {
            x = geometry[0]
            y = geometry[1]
            width = geometry[2]
            height = geometry[3]
        }
    }
    onClosing: close => {
        if (connectionManager.database_editor.dirty && !closeConfirmed) {
            close.accepted = false
            unsavedDialog.open()
            return
        }
        connectionManager.app_state.save_window_geometry(x, y, width, height)
    }

    // The actions have no menu bar to register their shortcuts
    Shortcut { sequences: ["Ctrl+K", "Ctrl+Shift+P"]; onActivated: Actions.commandPalette.trigger() }
    Shortcut { sequences: [StandardKey.New]; onActivated: Actions.newConnection.trigger() }
    Shortcut { sequences: [StandardKey.Open]; onActivated: Actions.openLogFile.trigger() }
    Shortcut { sequences: [StandardKey.Close]; enabled: Actions.closeConnection.enabled; onActivated: Actions.closeConnection.trigger() }
    Shortcut { sequences: [StandardKey.Copy]; enabled: Actions.copy.enabled; onActivated: Actions.copy.trigger() }
    Shortcut { sequences: [StandardKey.SelectAll]; enabled: Actions.selectAll.enabled; onActivated: Actions.selectAll.trigger() }
    Shortcut { sequences: [StandardKey.Preferences]; onActivated: Actions.preferences.trigger() }
    Shortcut { sequences: [StandardKey.Quit]; onActivated: Actions.exit.trigger() }
    Shortcut { sequence: "Ctrl+Shift+O"; onActivated: Actions.openWorkspace.trigger() }
    Shortcut { sequences: [StandardKey.Save]; enabled: Actions.saveWorkspace.enabled; onActivated: Actions.saveWorkspace.trigger() }
    Shortcut { sequences: [StandardKey.SaveAs]; enabled: Actions.saveWorkspaceAs.enabled; onActivated: Actions.saveWorkspaceAs.trigger() }

    // Workspaces
    property var pendingWorkspace: null  // Opening it replaces the connections: confirmed first

    function openWorkspace(open) {
        if (connectionManager.connections.length === 0) {
            finishOpeningWorkspace(open())
            return
        }
        pendingWorkspace = open
        replaceDialog.open()
    }

    function finishOpeningWorkspace(problems) {
        if (problems)
            for (const problem of problems.split("\n"))
                mainWindow.notify(problem)
        mainWindow.tool = ""
        mainWindow.selectedIndex = connectionManager.workspace_current()
    }

    function saveWorkspace(url) {
        const error = connectionManager.save_workspace(url, mainWindow.currentIndex)
        if (error)
            mainWindow.notify(error)
    }

    FileDialog {
        id: openWorkspaceDialog
        title: qsTr("Open workspace")
        fileMode: FileDialog.OpenFile
        nameFilters: connectionManager.workspace_filters
        onAccepted: mainWindow.openWorkspace(() => connectionManager.open_workspace(selectedFile))
    }

    FileDialog {
        id: saveWorkspaceDialog
        title: qsTr("Save workspace")
        fileMode: FileDialog.SaveFile
        nameFilters: connectionManager.workspace_filters
        defaultSuffix: "cxw"
        onAccepted: mainWindow.saveWorkspace(selectedFile)
    }

    Dialog {
        id: replaceDialog
        title: qsTr("Open workspace")
        modal: true
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Open | Dialog.Cancel
        Label {
            text: qsTr("The open connections are closed and replaced by those of the workspace.")
        }
        onAccepted: {
            const open = mainWindow.pendingWorkspace
            mainWindow.pendingWorkspace = null
            if (open)
                mainWindow.finishOpeningWorkspace(open())
        }
        onRejected: mainWindow.pendingWorkspace = null
    }

    Connections {
        target: Actions.openWorkspace
        function onTriggered() {
            openWorkspaceDialog.open()
        }
    }

    Connections {
        target: Actions.saveWorkspace
        function onTriggered() {
            if (connectionManager.workspace_file) {
                const error = connectionManager.save_current_workspace(mainWindow.currentIndex)
                if (error)
                    mainWindow.notify(error)
            } else {
                saveWorkspaceDialog.open()
            }
        }
    }

    Connections {
        target: Actions.saveWorkspaceAs
        function onTriggered() {
            saveWorkspaceDialog.open()
        }
    }

    Connections {
        target: Actions
        function onOpenRecentWorkspace(index) {
            mainWindow.openWorkspace(() => connectionManager.open_recent_workspace(index))
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Sidebar {
            id: sidebar
            connections: connectionManager.connections
            currentConnection: mainWindow.currentIndex
            pages: mainWindow.currentConnectionView ? mainWindow.currentConnectionView.pages : []
            currentPage: mainWindow.currentConnectionView ? mainWindow.currentConnectionView.page : ""
            currentTool: mainWindow.tool
            onConnectionSelected: index => {
                mainWindow.selectedIndex = index
                mainWindow.tool = ""
            }
            onPageSelected: page => {
                mainWindow.currentConnectionView.page = page
                mainWindow.tool = ""
            }
            onToolSelected: tool => mainWindow.tool = tool
            Layout.fillHeight: true
            Layout.preferredWidth: Math.max(connectionManager.app_state.min_sidebar_width,
                                            Math.min(mainWindow.maximumSidebarWidth, mainWindow.sidebarWidth))
        }

        ColumnLayout {
            spacing: 0
            Layout.fillWidth: true
            Layout.fillHeight: true

            TopBar {
                connection: mainWindow.tool === "" ? mainWindow.currentConnection : null
                title: mainWindow.tool === "database-editor" ? qsTr("Database editor")
                    : mainWindow.tool === "compare" ? qsTr("Compare recordings")
                    : mainWindow.tool === "ethercat" ? qsTr("EtherCAT (CoE)")
                    : mainWindow.tool === "physical-io" ? qsTr("Physical I/O")
                    : mainWindow.tool === "doip" ? qsTr("DoIP")
                    : mainWindow.tool === "someip" ? qsTr("SOME/IP")
                    : mainWindow.tool === "gateway" ? qsTr("Bus gateway")
                    : mainWindow.tool === "multi-bus" ? qsTr("Multi-bus trace") : ""
                pageTitle: mainWindow.tool === "database-editor" ? connectionManager.database_editor.file_name
                    : mainWindow.tool === "compare" ? compareView.pageTitle
                    : mainWindow.tool === "ethercat" ? etherCatView.pageTitle
                    : mainWindow.tool === "physical-io" ? physicalIoView.pageTitle
                    : mainWindow.tool === "doip" ? doipView.pageTitle
                    : mainWindow.tool === "someip" ? someIpView.pageTitle
                    : mainWindow.tool === "gateway" ? gatewayView.pageTitle
                    : mainWindow.tool === "multi-bus" ? multiBusView.pageTitle
                    : mainWindow.currentConnectionView ? mainWindow.currentConnectionView.pageTitle : ""
                notifications: mainWindow.notifications
                onClearNotifications: mainWindow.notifications = []
                Layout.fillWidth: true
            }

            DatabaseEditorView {
                id: databaseEditorView
                visible: mainWindow.tool === "database-editor"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            MultiBusView {
                id: multiBusView
                visible: mainWindow.tool === "multi-bus"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            CompareView {
                id: compareView
                visible: mainWindow.tool === "compare"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            EtherCatView {
                id: etherCatView
                visible: mainWindow.tool === "ethercat"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            PhysicalIoView {
                id: physicalIoView
                visible: mainWindow.tool === "physical-io"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            DoipView {
                id: doipView
                visible: mainWindow.tool === "doip"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            SomeIpView {
                id: someIpView
                visible: mainWindow.tool === "someip"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            GatewayView {
                id: gatewayView
                visible: mainWindow.tool === "gateway"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            StackLayout {
                visible: mainWindow.tool === "" && connectionManager.connections.length > 0
                currentIndex: mainWindow.currentIndex
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24

                Repeater {
                    id: connectionViews
                    model: connectionManager.connections
                    ConnectionView {
                        required property var modelData
                        connection: modelData
                    }
                }
            }

            WelcomeView {
                visible: mainWindow.tool === "" && connectionManager.connections.length === 0
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            // Status line
            Rectangle {
                visible: mainWindow.tool === "" && mainWindow.currentConnection !== null
                color: Theme.surface
                Layout.fillWidth: true
                Layout.preferredHeight: 30
                Label {
                    anchors.fill: parent
                    anchors.leftMargin: 24
                    verticalAlignment: Text.AlignVCenter
                    color: Theme.muted
                    elide: Text.ElideRight
                    text: {
                        const connection = mainWindow.currentConnection
                        if (!connection)
                            return ""
                        let parts = [connection.description, connection.state, qsTr("%1 frames").arg(connection.frame_count)]
                        if (connection.connected) {
                            parts.push(qsTr("Rx %1/s  Tx %2/s").arg(connection.rx_rate.toFixed(0)).arg(connection.tx_rate.toFixed(0)))
                            if (connection.bus_load >= 0)
                                parts.push(qsTr("load %1%").arg(connection.bus_load.toFixed(1)))
                            if (connection.bus_state)
                                parts.push(qsTr("bus %1").arg(connection.bus_state))
                        }
                        if (connection.error_frames > 0)
                            parts.push(qsTr("%1 error frames").arg(connection.error_frames))
                        if (connection.protocol !== "Raw")
                            parts.push(connection.protocol)
                        if (connection.database_file)
                            parts.push(qsTr("DB: %1").arg(connection.database_file.split(/[\\/]/).pop()))
                        if (connection.recording)
                            parts.push(qsTr("● recording to %1").arg(connection.recording_file))
                        return parts.join("  —  ")
                    }
                }
            }
        }
    }

    // Drag the right edge of the sidebar to resize it; a double click restores the default width
    Item {
        id: sidebarHandle
        x: sidebar.width - width / 2
        width: 8
        height: parent.height
        z: 50
        readonly property bool engaged: sidebarHover.hovered || sidebarDrag.active

        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 2
            height: parent.height
            color: Theme.accent
            opacity: sidebarHandle.engaged ? 0.8 : 0
            Behavior on opacity { NumberAnimation { duration: 120 } }
        }
        HoverHandler {
            id: sidebarHover
            cursorShape: Qt.SplitHCursor
        }
        DragHandler {
            id: sidebarDrag
            target: null
            yAxis.enabled: false
            property int startWidth
            onActiveChanged: {
                if (active)
                    startWidth = sidebar.width
                else
                    connectionManager.app_state.set_sidebar_width(mainWindow.sidebarWidth)
            }
            onTranslationChanged: if (active) mainWindow.sidebarWidth = Math.round(Math.max(
                connectionManager.app_state.min_sidebar_width,
                Math.min(mainWindow.maximumSidebarWidth, startWidth + translation.x)))
        }
        TapHandler {
            onDoubleTapped: {
                mainWindow.sidebarWidth = Theme.sidebarWidth
                connectionManager.app_state.set_sidebar_width(Theme.sidebarWidth)
            }
        }
        ToolTip.visible: sidebarHover.hovered && !sidebarDrag.active
        ToolTip.delay: 800
        ToolTip.text: qsTr("Drag to resize, double-click to reset")
    }

    // Resizing along the edges and corners, as the window has no frame
    component ResizeEdge: Item {
        id: edge
        required property int edges
        property int cursor: Qt.ArrowCursor
        z: 100
        HoverHandler { cursorShape: edge.cursor }
        DragHandler {
            target: null
            onActiveChanged: if (active) mainWindow.startSystemResize(edge.edges)
        }
    }
    readonly property int resizeMargin: mainWindow.visibility === Window.Maximized ? 0 : 5
    ResizeEdge { edges: Qt.LeftEdge; cursor: Qt.SizeHorCursor; x: 0; y: mainWindow.resizeMargin; width: mainWindow.resizeMargin; height: parent.height - 2 * mainWindow.resizeMargin }
    ResizeEdge { edges: Qt.RightEdge; cursor: Qt.SizeHorCursor; x: parent.width - width; y: mainWindow.resizeMargin; width: mainWindow.resizeMargin; height: parent.height - 2 * mainWindow.resizeMargin }
    ResizeEdge { edges: Qt.TopEdge; cursor: Qt.SizeVerCursor; x: mainWindow.resizeMargin; y: 0; width: parent.width - 2 * mainWindow.resizeMargin; height: mainWindow.resizeMargin }
    ResizeEdge { edges: Qt.BottomEdge; cursor: Qt.SizeVerCursor; x: mainWindow.resizeMargin; y: parent.height - height; width: parent.width - 2 * mainWindow.resizeMargin; height: mainWindow.resizeMargin }
    ResizeEdge { edges: Qt.LeftEdge | Qt.TopEdge; cursor: Qt.SizeFDiagCursor; width: mainWindow.resizeMargin * 2; height: width }
    ResizeEdge { edges: Qt.RightEdge | Qt.TopEdge; cursor: Qt.SizeBDiagCursor; x: parent.width - width; width: mainWindow.resizeMargin * 2; height: width }
    ResizeEdge { edges: Qt.LeftEdge | Qt.BottomEdge; cursor: Qt.SizeBDiagCursor; y: parent.height - height; width: mainWindow.resizeMargin * 2; height: width }
    ResizeEdge { edges: Qt.RightEdge | Qt.BottomEdge; cursor: Qt.SizeFDiagCursor; x: parent.width - width; y: parent.height - height; width: mainWindow.resizeMargin * 2; height: width }

    // Error toast; the messages are kept in the notifications of the top bar
    Rectangle {
        id: toast
        property alias text: toastLabel.text
        function show(message) {
            text = message
            opacity = 1
            toastTimer.restart()
        }
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        anchors.margins: 40
        width: Math.min(460, toastLabel.implicitWidth + 32)
        height: toastLabel.implicitHeight + 24
        radius: Theme.smallRadius
        color: Theme.card
        border.color: Theme.danger
        opacity: 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: 200 } }
        Label {
            id: toastLabel
            anchors.fill: parent
            anchors.margins: 12
            color: Theme.text
            wrapMode: Text.Wrap
        }
        MouseArea {
            anchors.fill: parent
            onClicked: toast.opacity = 0
        }
        Timer {
            id: toastTimer
            interval: 6000
            onTriggered: toast.opacity = 0
        }
    }

    AboutDialog {
        id: aboutDialog
    }

    NewConnectionDialog {
        id: newConnectionDialog
    }

    ReplayDialog {
        id: replayDialog
    }

    PreferencesDialog {
        id: preferencesDialog
    }

    ConvertDatabaseDialog {
        id: convertDatabaseDialog
    }

    Connections {
        target: connectionManager
        function onError_occurred(message) {
            mainWindow.notify(message)
        }
        function onConnections_changed() {
            mainWindow.selectedIndex = connectionManager.connections.length - 1
        }
    }

    Connections {
        target: Actions.about
        function onTriggered() {
            aboutDialog.open();
        }
    }

    Connections {
        target: Actions.newConnection
        function onTriggered() {
            newConnectionDialog.open()
        }
    }

    Connections {
        target: Actions.openLogFile
        function onTriggered() {
            replayDialog.open()
        }
    }

    Connections {
        target: Actions.closeConnection
        function onTriggered() {
            connectionManager.remove_connection(mainWindow.currentIndex)
        }
    }

    Connections {
        target: Actions.copy
        function onTriggered() {
            const view = mainWindow.currentView()
            if (view)
                view.copySelection()
        }
    }

    Connections {
        target: Actions.selectAll
        function onTriggered() {
            const view = mainWindow.currentView()
            if (view)
                view.selectAll()
        }
    }

    Connections {
        target: Actions.editDatabase
        function onTriggered() {
            mainWindow.tool = "database-editor"
        }
    }

    Connections {
        target: Actions
        function onEditDatabaseFile(path) {
            mainWindow.tool = "database-editor"
            databaseEditorView.openPath(path)
        }
        function onShowTool(tool) {
            mainWindow.tool = tool
        }
    }

    Dialog {
        id: unsavedDialog
        title: qsTr("Unsaved database changes")
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(460, Overlay.overlay.width - 48)
        standardButtons: Dialog.Save | Dialog.Discard | Dialog.Cancel
        Label {
            width: unsavedDialog.availableWidth
            wrapMode: Text.Wrap
            text: qsTr("Save the changes to %1 before quitting?").arg(connectionManager.database_editor.file_name)
        }
        onAccepted: {
            const editor = connectionManager.database_editor
            const error = editor.writable ? editor.save() : qsTr("Use \"Save as\" in the database editor first")
            if (error) {
                mainWindow.tool = "database-editor"
                mainWindow.notify(error)
                return
            }
            mainWindow.closeConfirmed = true
            mainWindow.close()
        }
        onDiscarded: {
            close()
            mainWindow.closeConfirmed = true
            mainWindow.close()
        }
    }

    Connections {
        target: Actions.convertDatabase
        function onTriggered() {
            convertDatabaseDialog.open()
        }
    }

    Connections {
        target: Actions.preferences
        function onTriggered() {
            preferencesDialog.open()
        }
    }

    UpdateDialog {
        id: updateDialog
        parent: Overlay.overlay
    }

    Connections {
        target: Actions.checkForUpdates
        function onTriggered() {
            // An update found already is shown at once; otherwise check now
            if (connectionManager.updates.state === "available")
                updateDialog.open()
            else
                updateDialog.checkNow()
        }
    }

    Connections {
        target: connectionManager.updates
        function onUpdate_available(version) {
            mainWindow.notify(qsTr("CANExplorer %1 is available: ⋮ → Check for Updates…").arg(version))
        }
    }

    CommandPalette {
        id: commandPalette
        parent: Overlay.overlay
        provider: mainWindow.paletteCommands
    }

    Connections {
        target: Actions.commandPalette
        function onTriggered() {
            commandPalette.open()
        }
    }

    Connections {
        target: Actions.exit
        function onTriggered() {
            mainWindow.close()
        }
    }
}
