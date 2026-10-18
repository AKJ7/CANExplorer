import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import CanExplorer 1.0


ApplicationWindow {
    visible: true
    flags: Qt.Window | Qt.FramelessWindowHint
    width: 1200
    height: 750
    id: mainWindow
    title: metadata.name

    readonly property int currentIndex: Math.min(selectedIndex, connectionManager.connections.length - 1)
    property int selectedIndex: -1
    readonly property var currentConnection: currentIndex >= 0 ? connectionManager.connections[currentIndex] : null
    readonly property var currentConnectionView: currentIndex >= 0 ? connectionViews.itemAt(currentIndex) : null
    property var notifications: []

    function currentView() {
        return currentConnectionView
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
        const geometry = connectionManager.app_state.window_geometry()
        if (geometry.length === 4) {
            x = geometry[0]
            y = geometry[1]
            width = geometry[2]
            height = geometry[3]
        }
    }
    onClosing: connectionManager.app_state.save_window_geometry(x, y, width, height)

    // The actions have no menu bar to register their shortcuts
    Shortcut { sequences: [StandardKey.New]; onActivated: Actions.newConnection.trigger() }
    Shortcut { sequences: [StandardKey.Open]; onActivated: Actions.openLogFile.trigger() }
    Shortcut { sequences: [StandardKey.Close]; enabled: Actions.closeConnection.enabled; onActivated: Actions.closeConnection.trigger() }
    Shortcut { sequences: [StandardKey.Copy]; enabled: Actions.copy.enabled; onActivated: Actions.copy.trigger() }
    Shortcut { sequences: [StandardKey.SelectAll]; enabled: Actions.selectAll.enabled; onActivated: Actions.selectAll.trigger() }
    Shortcut { sequences: [StandardKey.Preferences]; onActivated: Actions.preferences.trigger() }
    Shortcut { sequences: [StandardKey.Quit]; onActivated: Actions.exit.trigger() }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Sidebar {
            id: sidebar
            connections: connectionManager.connections
            currentConnection: mainWindow.currentIndex
            pages: mainWindow.currentConnectionView ? mainWindow.currentConnectionView.pages : []
            currentPage: mainWindow.currentConnectionView ? mainWindow.currentConnectionView.page : ""
            onConnectionSelected: index => mainWindow.selectedIndex = index
            onPageSelected: page => mainWindow.currentConnectionView.page = page
            Layout.fillHeight: true
            Layout.preferredWidth: implicitWidth
        }

        ColumnLayout {
            spacing: 0
            Layout.fillWidth: true
            Layout.fillHeight: true

            TopBar {
                connection: mainWindow.currentConnection
                pageTitle: mainWindow.currentConnectionView ? mainWindow.currentConnectionView.pageTitle : ""
                notifications: mainWindow.notifications
                onClearNotifications: mainWindow.notifications = []
                Layout.fillWidth: true
            }

            StackLayout {
                visible: connectionManager.connections.length > 0
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
                visible: connectionManager.connections.length === 0
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
            }

            // Status line
            Rectangle {
                visible: mainWindow.currentConnection !== null
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

    Connections {
        target: Actions.exit
        function onTriggered() {
            mainWindow.close()
        }
    }
}
