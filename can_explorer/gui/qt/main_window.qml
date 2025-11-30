import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import CanExplorer 1.0


ApplicationWindow {
    visible: true
    width: 600
    height: 500
    id: mainWindow
    title: metadata.name

    Text {
        anchors.centerIn: parent
        text: "Hello World"
        font.pixelSize: 24
    }

    MenuBar {
        id: applicationMenu
        width: parent.width
        FileMenu {}
        EditMenu {}
        HelpMenu {}
    }

    Component.onCompleted: {
    }

    AboutDialog {
        id: aboutDialog
        visible: false
    }

    Connections {
        target: Actions.about
        function onTriggered() {
            aboutDialog.open();
        }
    }

    Connections {
        target: Actions.exit
        function onTriggered() {
            mainWindow.close()
        }
    }

    header: TabBar {
        id: tabBar
        width: parent.width
        TabButton {
            text: qsTr("Start")
        }
        visible: false
    }
}