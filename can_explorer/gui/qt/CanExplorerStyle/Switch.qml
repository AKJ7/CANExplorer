import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.Switch {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding,
                             implicitIndicatorHeight + topPadding + bottomPadding)

    padding: 6
    spacing: 8

    indicator: Rectangle {
        implicitWidth: 38
        implicitHeight: 20
        x: control.text ? (control.mirrored ? control.width - width - control.rightPadding : control.leftPadding)
                        : control.leftPadding + (control.availableWidth - width) / 2
        y: control.topPadding + (control.availableHeight - height) / 2
        radius: height / 2
        opacity: control.enabled ? 1 : 0.45
        color: control.checked ? Theme.accent : Theme.alpha(Theme.muted, 0.35)
        border.width: control.visualFocus ? 2 : 0
        border.color: Theme.text

        Rectangle {
            x: control.checked ? parent.width - width - 3 : 3
            y: 3
            width: parent.height - 6
            height: width
            radius: width / 2
            color: "white"
            Behavior on x { NumberAnimation { duration: 120 } }
        }
    }

    contentItem: Text {
        leftPadding: control.indicator && !control.mirrored ? control.indicator.width + control.spacing : 0
        rightPadding: control.indicator && control.mirrored ? control.indicator.width + control.spacing : 0
        text: control.text
        font: control.font
        color: control.enabled ? Theme.text : Theme.alpha(Theme.text, 0.4)
        elide: Text.ElideRight
        verticalAlignment: Text.AlignVCenter
    }
}
