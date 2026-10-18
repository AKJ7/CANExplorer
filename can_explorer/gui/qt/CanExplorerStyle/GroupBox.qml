import QtQuick
import QtQuick.Templates as T
import CanExplorer

// Rounded box with the title inside, at the top.
T.GroupBox {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            contentWidth + leftPadding + rightPadding,
                            implicitLabelWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             contentHeight + topPadding + bottomPadding)

    spacing: 10
    padding: 14
    topPadding: padding + (implicitLabelWidth > 0 ? implicitLabelHeight + spacing : 0)

    label: Text {
        x: control.leftPadding
        y: control.padding
        width: control.availableWidth
        text: control.title
        font.bold: true
        font.pixelSize: 12
        font.letterSpacing: 0.6
        color: Theme.muted
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: Theme.smallRadius + 2
        color: Theme.alpha(Theme.cardAlt, 0.6)
        border.color: Theme.alpha(Theme.border, 0.8)
    }
}
