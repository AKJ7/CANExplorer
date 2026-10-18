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
        target: Actions.exit
        function onTriggered() {
            mainWindow.close()
        }
    }
}
