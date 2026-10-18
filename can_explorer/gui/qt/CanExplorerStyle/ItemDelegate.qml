import QtQuick
import QtQuick.Controls.impl
import QtQuick.Templates as T
import CanExplorer

T.ItemDelegate {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding,
                             implicitIndicatorHeight + topPadding + bottomPadding)

    padding: 8
    horizontalPadding: 12
    spacing: 8
    icon.width: 16
    icon.height: 16
    icon.color: Theme.text

    contentItem: IconLabel {
        spacing: control.spacing
        mirrored: control.mirrored
        display: control.display
        alignment: control.display === IconLabel.IconOnly || control.display === IconLabel.TextUnderIcon ? Qt.AlignCenter : Qt.AlignLeft
        icon: control.icon
        text: control.text
        font: control.font
        color: !control.enabled ? Theme.alpha(Theme.text, 0.4) : control.highlighted ? Theme.accent : Theme.text
    }

    background: Rectangle {
        implicitWidth: 100
        implicitHeight: 34
        radius: Theme.smallRadius - 2
        color: control.down ? Theme.alpha(Theme.accent, 0.24)
            : control.highlighted ? Theme.alpha(Theme.accent, 0.16)
            : control.hovered ? Theme.alpha(Theme.text, 0.06) : "transparent"
    }
}
