import QtQuick 2.15
import QtQuick.Controls 2.15
import CanExplorer 1.0

Menu {
    title: qsTr("&File")
    MenuItem {
        action: Actions.newConnection
    }
    Menu {
        id: recentConnectionsMenu
        title: qsTr("Recent &Connections")
        enabled: connectionManager.app_state.recent_connections.length > 0

        Instantiator {
            model: connectionManager.app_state.recent_connections
            delegate: MenuItem {
                required property var modelData
                required property int index
                text: modelData.label
                onTriggered: connectionManager.open_recent_connection(index)
            }
            onObjectAdded: (index, object) => recentConnectionsMenu.insertItem(index, object)
            onObjectRemoved: (index, object) => recentConnectionsMenu.removeItem(object)
        }
    }
    MenuSeparator {}
    MenuItem {
        action: Actions.openLogFile
    }
    Menu {
        id: recentLogFilesMenu
        title: qsTr("Recent &Log Files")
        enabled: connectionManager.app_state.recent_log_files.length > 0

        Instantiator {
            model: connectionManager.app_state.recent_log_files
            delegate: MenuItem {
                required property var modelData
                required property int index
                text: modelData.path
                onTriggered: connectionManager.open_recent_log_file(index)
            }
            onObjectAdded: (index, object) => recentLogFilesMenu.insertItem(index, object)
            onObjectRemoved: (index, object) => recentLogFilesMenu.removeItem(object)
        }
    }
    MenuSeparator {}
    MenuItem {
        action: Actions.convertDatabase
    }
    MenuSeparator {}
    MenuItem {
        action: Actions.closeConnection
    }
    MenuSeparator {}
    MenuItem {
        action: Actions.exit
    }
}
