import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.CheckBox {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding,
                             implicitIndicatorHeight + topPadding + bottomPadding)

    padding: 6
    spacing: 8

    indicator: Rectangle {
        implicitWidth: 18
        implicitHeight: 18
        x: control.text ? (control.mirrored ? control.width - width - control.rightPadding : control.leftPadding)
                        : control.leftPadding + (control.availableWidth - width) / 2
        y: control.topPadding + (control.availableHeight - height) / 2
        radius: 5
        opacity: control.enabled ? 1 : 0.45
        color: control.checkState !== Qt.Unchecked ? Theme.accent
            : control.hovered ? Theme.alpha(Theme.text, 0.08) : Theme.cardAlt
        border.width: control.checkState !== Qt.Unchecked ? 0 : (control.visualFocus ? 2 : 1)
        border.color: control.visualFocus ? Theme.accent : Theme.alpha(Theme.muted, 0.7)

        Text {
            anchors.centerIn: parent
            text: control.checkState === Qt.PartiallyChecked ? "–" : "✓"
            visible: control.checkState !== Qt.Unchecked
            color: "white"
            font.pixelSize: 13
            font.bold: true
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
