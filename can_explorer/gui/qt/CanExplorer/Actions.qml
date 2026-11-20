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
    property alias securityManager: securityManagerAction
    property alias convertDatabase: convertDatabaseAction
    property alias editDatabase: editDatabaseAction
    property alias openWorkspace: openWorkspaceAction
    property alias saveWorkspace: saveWorkspaceAction
    property alias saveWorkspaceAs: saveWorkspaceAsAction
    property alias commandPalette: commandPaletteAction
    property alias checkForUpdates: checkForUpdatesAction
    property alias help: helpAction
    property alias systemReport: systemReportAction

    // Open a database file in the editor (e.g. the one of a connection)
    signal editDatabaseFile(string path)
    // Open the workspace at this index of the recent workspaces
    signal openRecentWorkspace(int index)
    // Show a tool page, e.g. "database-editor"
    signal showTool(string tool)

    // A line icon of images/menu, tinted by the style with the menu text colour (so it follows the theme and the
    // enabled state). Bundled rather than an icon theme name: Windows and macOS have no icon theme.
    function menuIcon(name) {
        return Qt.resolvedUrl(`images/menu/${name}.svg`)
    }

    Action {
        id: exitAction
        text: qsTr("&Exit")
        icon.source: menuIcon("exit")
        shortcut: StandardKey.Quit
    }

    Action {
        id: aboutAction
        text: qsTr("&About")
        icon.source: menuIcon("info")
    }

    Action {
        id: helpAction
        text: qsTr("&Help")
        icon.source: menuIcon("help")
        shortcut: StandardKey.HelpContents
    }

    // Copies versions, the Qt setup and what each CAN backend detected, to paste into a bug report
    Action {
        id: systemReportAction
        text: qsTr("Copy &System Report")
        icon.source: menuIcon("clipboard")
    }

    Action {
        id: newConnectionAction
        text: qsTr("&New Connection...")
        icon.source: menuIcon("plug")
        shortcut: StandardKey.New
    }

    Action {
        id: openLogFileAction
        text: qsTr("&Replay Log File...")
        icon.source: menuIcon("file-play")
        shortcut: StandardKey.Open
    }

    Action {
        id: closeConnectionAction
        text: qsTr("&Close Connection")
        icon.source: menuIcon("plug-off")
        shortcut: StandardKey.Close
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: copyAction
        text: qsTr("&Copy Selected Rows")
        icon.source: menuIcon("copy")
        shortcut: StandardKey.Copy
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: selectAllAction
        text: qsTr("Select &All")
        icon.source: menuIcon("select-all")
        shortcut: StandardKey.SelectAll
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: convertDatabaseAction
        text: qsTr("Con&vert Signal Database...")
        icon.source: menuIcon("convert")
    }

    Action {
        id: openWorkspaceAction
        text: qsTr("Open &Workspace...")
        icon.source: menuIcon("folder-open")
        shortcut: "Ctrl+Shift+O"
    }

    Action {
        id: saveWorkspaceAction
        text: qsTr("&Save Workspace")
        icon.source: menuIcon("save")
        shortcut: StandardKey.Save
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: saveWorkspaceAsAction
        text: qsTr("Save Workspace &As...")
        icon.source: menuIcon("save-as")
        shortcut: StandardKey.SaveAs
        enabled: connectionManager.connections.length > 0
    }

    Action {
        id: editDatabaseAction
        text: qsTr("&Edit Signal Database...")
        icon.source: menuIcon("database-edit")
    }

    Action {
        id: commandPaletteAction
        text: qsTr("Command &Palette...")
        icon.source: menuIcon("search")
        shortcut: "Ctrl+K"
    }

    Action {
        id: checkForUpdatesAction
        text: qsTr("Check for &Updates...")
        icon.source: menuIcon("refresh")
    }

    Action {
        id: securityManagerAction
        text: qsTr("Security &Manager...")
        icon.source: menuIcon("shield-key")
    }

    Action {
        id: preferencesAction
        text: qsTr("&Preferences...")
        icon.source: menuIcon("sliders")
        shortcut: StandardKey.Preferences
    }
}
