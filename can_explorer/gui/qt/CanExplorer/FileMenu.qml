import QtQuick 2.7
import QtQuick.Controls 2.4
import CanExplorer 1.0

Menu {
    title: qsTr("&File")
    MenuItem {
        action: Actions.newConnection
    }
    MenuItem {
        action: Actions.openLogFile
    }
    MenuItem {
        action: Actions.closeConnection
    }
    MenuSeparator {}
    MenuItem {
        action: Actions.exit
    }
}
