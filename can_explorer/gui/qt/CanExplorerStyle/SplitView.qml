import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.SplitView {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    handle: Rectangle {
        implicitWidth: control.orientation === Qt.Horizontal ? 7 : control.width
        implicitHeight: control.orientation === Qt.Horizontal ? control.height : 7
        color: "transparent"

        Rectangle {
            anchors.centerIn: parent
            width: control.orientation === Qt.Horizontal ? (parent.T.SplitHandle.hovered || parent.T.SplitHandle.pressed ? 3 : 1) : parent.width
            height: control.orientation === Qt.Horizontal ? parent.height : (parent.T.SplitHandle.hovered || parent.T.SplitHandle.pressed ? 3 : 1)
            radius: 1
            color: parent.T.SplitHandle.pressed ? Theme.accent
                : parent.T.SplitHandle.hovered ? Theme.alpha(Theme.accent, 0.6) : Theme.border
        }
    }
}
