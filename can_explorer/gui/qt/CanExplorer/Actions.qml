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
    property alias copy: copyAction
    property alias selectAll: selectAllAction
    property alias preferences: preferencesAction
    property alias convertDatabase: convertDatabaseAction
    property alias editDatabase: editDatabaseAction
    property alias openWorkspace: openWorkspaceAction
    property alias saveWorkspace: saveWorkspaceAction
    property alias saveWorkspaceAs: saveWorkspaceAsAction
    property alias commandPalette: commandPaletteAction
    property alias checkForUpdates: checkForUpdatesAction

    // Open a database file in the editor (e.g. the one of a connection)
    signal editDatabaseFile(string path)
    // Open the workspace at this index of the recent workspaces
    signal openRecentWorkspace(int index)

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

    Action {
        id: copyAction
        text: qsTr("&Copy Selected Rows")
        icon.name: "edit-copy"
        shortcut: StandardKey.Copy
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: selectAllAction
        text: qsTr("Select &All")
        icon.name: "edit-select-all"
        shortcut: StandardKey.SelectAll
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: convertDatabaseAction
        text: qsTr("Con&vert Signal Database...")
        icon.name: "document-save-as"
    }

    Action {
        id: openWorkspaceAction
        text: qsTr("Open &Workspace...")
        icon.name: "document-open"
        shortcut: "Ctrl+Shift+O"
    }

    Action {
        id: saveWorkspaceAction
        text: qsTr("&Save Workspace")
        icon.name: "document-save"
        shortcut: StandardKey.Save
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: saveWorkspaceAsAction
        text: qsTr("Save Workspace &As...")
        icon.name: "document-save-as"
        shortcut: StandardKey.SaveAs
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: editDatabaseAction
        text: qsTr("&Edit Signal Database...")
        icon.name: "document-edit"
    }

    Action {
        id: commandPaletteAction
        text: qsTr("Command &Palette...")
        icon.name: "system-search"
        shortcut: "Ctrl+K"
    }

    Action {
        id: checkForUpdatesAction
        text: qsTr("Check for &Updates...")
        icon.name: "system-software-update"
    }

    Action {
        id: preferencesAction
        text: qsTr("&Preferences...")
        icon.name: "preferences-system"
        shortcut: StandardKey.Preferences
    }
}
