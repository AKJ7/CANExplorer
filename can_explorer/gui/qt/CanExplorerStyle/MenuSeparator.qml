import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.MenuSeparator {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    horizontalPadding: 8
    verticalPadding: 4

    contentItem: Rectangle {
        implicitWidth: 200
        implicitHeight: 1
        color: Theme.border
    }
}
