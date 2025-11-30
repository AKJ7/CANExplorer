pragma Singleton

import QtQuick 2.15
import QtQuick.Controls 2.15
import CanExplorer 1.0


Item {
    property alias exit: exitAction
    property alias about: aboutAction

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
}