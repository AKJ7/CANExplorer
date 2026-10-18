import QtQuick
import QtQuick.Controls.impl
import QtQuick.Templates as T
import CanExplorer

// Pill shaped; highlighted and checked buttons are filled with the accent colour, flat buttons are text only.
T.Button {
    id: control
    readonly property bool filled: control.highlighted || control.checked

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    padding: 6
    horizontalPadding: 16
    spacing: 6
    icon.width: 16
    icon.height: 16
    icon.color: contentColor

    readonly property color contentColor: !control.enabled ? Theme.alpha(Theme.text, 0.35)
        : control.filled ? "white" : control.flat ? Theme.accent : Theme.text

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
        implicitWidth: 80
        implicitHeight: 34
        radius: height / 2
        visible: !control.flat || control.hovered || control.down || control.visualFocus
        color: control.filled
            ? (!control.enabled ? Theme.alpha(Theme.accent, 0.4)
               : control.down ? Qt.darker(Theme.accent, 1.12)
               : control.hovered ? Qt.lighter(Theme.accent, 1.12) : Theme.accent)
            : control.flat ? Theme.alpha(Theme.accent, control.down ? 0.2 : 0.12)
            : control.down ? Theme.alpha(Theme.text, 0.16)
            : control.hovered && control.enabled ? Theme.alpha(Theme.text, 0.09) : Theme.cardAlt
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus ? Theme.accent
            : control.filled || control.flat ? "transparent" : Theme.border
    }
}
