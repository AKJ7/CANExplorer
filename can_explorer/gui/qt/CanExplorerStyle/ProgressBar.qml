import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.ProgressBar {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    contentItem: Item {
        implicitWidth: 200
        implicitHeight: 6
        clip: true

        Rectangle {
            id: bar
            width: control.indeterminate ? parent.width * 0.3 : control.visualPosition * parent.width
            height: parent.height
            radius: 3
            color: Theme.accent

            NumberAnimation on x {
                running: control.indeterminate && control.visible
                from: -bar.width
                to: control.availableWidth
                duration: 1200
                loops: Animation.Infinite
            }
        }
    }

    background: Rectangle {
        implicitWidth: 200
        implicitHeight: 6
        y: (control.height - height) / 2
        height: 6
        radius: 3
        color: Theme.alpha(Theme.muted, 0.3)
    }
}
