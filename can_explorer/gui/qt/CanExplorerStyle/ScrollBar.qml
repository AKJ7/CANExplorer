import QtQuick
import QtQuick.Templates as T
import CanExplorer

// Thin rounded handle on a transparent track; wider while hovered or pressed.
T.ScrollBar {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    padding: 2
    visible: control.policy !== T.ScrollBar.AlwaysOff
    minimumSize: orientation === Qt.Horizontal ? height / width : width / height

    // The track keeps its size; only the drawn handle widens, so nothing beside the bar moves
    contentItem: Item {
        readonly property bool active: control.hovered || control.pressed
        implicitWidth: control.interactive ? 6 : 3
        implicitHeight: control.interactive ? 6 : 3
        opacity: control.policy === T.ScrollBar.AlwaysOn || control.size < 1.0 ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 150 } }

        Rectangle {
            readonly property bool vertical: control.orientation === Qt.Vertical
            // Grows while hovered or dragged; only this number is animated, the geometry follows it
            property real thickness: parent.active ? 6 : 3
            Behavior on thickness { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
            x: vertical ? parent.width - thickness : 0
            y: vertical ? 0 : parent.height - thickness
            width: vertical ? thickness : parent.width
            height: vertical ? parent.height : thickness
            radius: thickness / 2
            color: control.pressed ? Theme.muted : Theme.alpha(Theme.muted, control.hovered ? 0.7 : 0.45)
        }
    }
}
