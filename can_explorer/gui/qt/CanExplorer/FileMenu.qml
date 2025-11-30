import QtQuick 2.7
import QtQuick.Controls 2.4
import CanExplorer 1.0

Menu {
    title: qsTr("&File")
    Action { text: qsTr("&New...") }
    Action { text: qsTr("&Open...") }
    Action { text: qsTr("&Save...") }
    Action { text: qsTr("Save &As...") }
    MenuSeparator {}
    MenuItem {
        action: Actions.exit
    }
}

