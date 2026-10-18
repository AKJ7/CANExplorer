import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


ApplicationWindow {
    visible: true
    width: 1200
    height: 750
    id: mainWindow
    title: metadata.name

    readonly property var currentConnection: tabBar.currentIndex >= 0
        ? connectionManager.connections[tabBar.currentIndex] : null

    function currentView() {
        return tabBar.currentIndex >= 0 ? connectionViews.itemAt(tabBar.currentIndex) : null
    }

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

    menuBar: MenuBar {
        id: applicationMenu
        FileMenu {}
        EditMenu {}
        HelpMenu {}
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        visible: connectionManager.connections.length > 0

        TabBar {
            id: tabBar
            Layout.fillWidth: true

            Repeater {
                model: connectionManager.connections
                TabButton {
                    required property var modelData
                    text: (modelData.connected ? "● " : "") + modelData.name
                    width: implicitWidth
                }
            }
        }

        StackLayout {
            currentIndex: tabBar.currentIndex
            Layout.fillWidth: true
            Layout.fillHeight: true

            Repeater {
                id: connectionViews
                model: connectionManager.connections
                ConnectionView {
                    required property var modelData
                    connection: modelData
                }
            }
        }
    }

    ColumnLayout {
        anchors.centerIn: parent
        visible: connectionManager.connections.length === 0
        spacing: 12

        Label {
            text: qsTr("No connection yet")
            font.pixelSize: 18
            Layout.alignment: Qt.AlignHCenter
        }
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            Button {
                action: Actions.newConnection
            }
            Button {
                action: Actions.openLogFile
            }
        }
    }

    footer: Label {
        padding: 4
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

    MessageDialog {
        id: errorDialog
        title: qsTr("Error")
        buttons: MessageDialog.Ok
    }

    Connections {
        target: connectionManager
        function onError_occurred(message) {
            errorDialog.text = message
            errorDialog.open()
        }
        function onConnections_changed() {
            tabBar.currentIndex = connectionManager.connections.length - 1
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
            connectionManager.remove_connection(tabBar.currentIndex)
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
