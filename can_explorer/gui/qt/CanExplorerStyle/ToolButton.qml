import QtQuick
import QtQuick.Controls.impl
import QtQuick.Templates as T
import CanExplorer

T.ToolButton {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    padding: 6
    horizontalPadding: 10
    spacing: 6
    icon.width: 18
    icon.height: 18
    icon.color: contentColor

    readonly property color contentColor: !control.enabled ? Theme.alpha(Theme.text, 0.35) : control.checked ? Theme.accent : Theme.text

    contentItem: IconLabel {
        spacing: control.spacing
        mirrored: control.mirrored
        display: control.display
        icon: control.icon
        text: control.text
        font: control.font
        color: control.contentColor
    }

    background: Rectangle {
        implicitWidth: 34
        implicitHeight: 34
        radius: Theme.smallRadius
        color: control.checked ? Theme.alpha(Theme.accent, 0.16)
            : control.down ? Theme.alpha(Theme.text, 0.14)
            : control.hovered && control.enabled ? Theme.alpha(Theme.text, 0.08) : "transparent"
        border.width: control.visualFocus ? 2 : 0
        border.color: Theme.accent
    }
}
