import QtQuick 2.15
import CanExplorer 1.0

// The CANExplorer diamond mark (a rounded diamond frame around a solid diamond), drawn in the current theme's
// accent colour so the logo follows the GUI theme. The proportions match docs/assets/logo.svg.
Item {
    id: root
    property color color: Theme.accent
    readonly property real s: Math.min(width, height)

    Item {
        anchors.centerIn: parent
        width: root.s
        height: root.s

        Rectangle {  // Diamond frame
            anchors.centerIn: parent
            width: root.s * 0.543
            height: width
            radius: width * 0.116
            rotation: 45
            color: "transparent"
            border.width: root.s * 0.093
            border.color: root.color
            antialiasing: true
        }
        Rectangle {  // Centre diamond
            anchors.centerIn: parent
            width: root.s * 0.219
            height: width
            radius: width * 0.173
            rotation: 45
            color: root.color
            antialiasing: true
        }
    }
}
