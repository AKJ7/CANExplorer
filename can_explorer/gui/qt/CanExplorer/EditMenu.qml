import QtQuick 2.7
import QtQuick.Controls 2.4
import CanExplorer 1.0

Menu {
    title: qsTr("&Edit")
    MenuItem { action: Actions.copy }
    MenuItem { action: Actions.selectAll }
    MenuSeparator {}
    MenuItem { action: Actions.preferences }
}
