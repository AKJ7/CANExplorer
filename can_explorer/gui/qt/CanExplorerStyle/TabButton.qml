import QtQuick
import QtQuick.Controls.impl
import QtQuick.Templates as T
import CanExplorer

// Text tab; the current one is accent coloured and underlined.
T.TabButton {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    padding: 8
    horizontalPadding: 14
    spacing: 6
    icon.width: 16
    icon.height: 16
    icon.color: contentColor

    readonly property color contentColor: !control.enabled ? Theme.alpha(Theme.text, 0.35)
        : control.checked ? Theme.accent : control.hovered ? Theme.text : Theme.muted

    contentItem: IconLabel {
        spacing: control.spacing
        mirrored: control.mirrored
        display: control.display
        icon: control.icon
        text: control.text
        font: control.font
        color: control.contentColor
    }

    background: Item {
        implicitHeight: 38
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 2
            radius: 1
            visible: control.checked
            color: Theme.accent
        }
    }
}
