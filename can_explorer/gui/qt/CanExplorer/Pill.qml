import QtQuick 2.15
import QtQuick.Controls 2.15
import CanExplorer 1.0

// Small rounded status label.
Rectangle {
    id: root
    property alias text: label.text
    property color tint: Theme.accent

    implicitWidth: label.implicitWidth + 20
    implicitHeight: label.implicitHeight + 8
    radius: height / 2
    color: Theme.alpha(tint, 0.18)

    Label {
        id: label
        anchors.centerIn: parent
        color: root.tint
        font.bold: true
        font.pixelSize: 12
    }
}
