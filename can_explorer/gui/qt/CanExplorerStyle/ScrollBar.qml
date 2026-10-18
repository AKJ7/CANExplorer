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

    contentItem: Rectangle {
        implicitWidth: control.interactive ? 10 : 4
        implicitHeight: control.interactive ? 10 : 4
        radius: Math.min(width, height) / 2
        color: control.pressed ? Theme.muted : Theme.alpha(Theme.muted, control.hovered ? 0.7 : 0.45)
        opacity: control.policy === T.ScrollBar.AlwaysOn || control.size < 1.0 ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 150 } }
    }
}
