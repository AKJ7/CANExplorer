import QtQuick 2.7
import QtQuick.Controls 2.4
import CanExplorer 1.0


Menu
{
    id: helpMenu
    // title: catalog.i18nc("@title:menu menubar:toplevel", &Help)
    title: qsTr("&Help")
    MenuItem {
        action: Actions.about
    }
 }



