pragma Singleton

import QtQuick 2.15
import QtQuick.Controls 2.15
import CanExplorer 1.0


Item {
    property alias exit: exitAction
    property alias about: aboutAction
    property alias newConnection: newConnectionAction
    property alias closeConnection: closeConnectionAction
    property alias openLogFile: openLogFileAction

    Action {
        id: exitAction
        text: qsTr("&Exit")
        icon.name: "application-exit"
        shortcut: StandardKey.Quit
    }

    Action {
        id: aboutAction
        text: qsTr("&About")
        icon.name: "help-about"
    }

    Action {
        id: newConnectionAction
        text: qsTr("&New Connection...")
        icon.name: "network-wired"
        shortcut: StandardKey.New
    }

    Action {
        id: openLogFileAction
        text: qsTr("&Replay Log File...")
        icon.name: "document-open"
        shortcut: StandardKey.Open
    }

    Action {
        id: closeConnectionAction
        text: qsTr("&Close Connection")
        icon.name: "window-close"
        shortcut: StandardKey.Close
        enabled: connectionManager.connections.length > 0
    }
}
