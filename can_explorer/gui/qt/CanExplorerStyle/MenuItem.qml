import QtQuick
import QtQuick.Controls.impl
import QtQuick.Templates as T
import CanExplorer

T.MenuItem {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding,
                             implicitIndicatorHeight + topPadding + bottomPadding)

    padding: 6
    horizontalPadding: 12
    spacing: 10
    icon.width: 16
    icon.height: 16
    icon.color: contentColor

    readonly property color contentColor: !control.enabled ? Theme.alpha(Theme.text, 0.35) : control.highlighted ? Theme.accent : Theme.text

    contentItem: IconLabel {
        readonly property real arrowPadding: control.subMenu && control.arrow ? control.arrow.width + control.spacing : 0
        readonly property real indicatorPadding: control.checkable && control.indicator ? control.indicator.width + control.spacing : 0
        leftPadding: !control.mirrored ? indicatorPadding : arrowPadding
        rightPadding: control.mirrored ? indicatorPadding : arrowPadding

        spacing: control.spacing
        mirrored: control.mirrored
        display: control.display
        alignment: Qt.AlignLeft

        icon: control.icon
        text: control.text
        font: control.font
        color: control.contentColor
    }

    indicator: Text {
        x: control.mirrored ? control.width - width - control.rightPadding : control.leftPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        visible: control.checkable
        text: control.checked ? "✓" : ""
        color: Theme.accent
        font.bold: true
    }

    arrow: Text {
        x: control.mirrored ? control.leftPadding : control.width - width - control.rightPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        visible: control.subMenu
        text: "›"
        font.pixelSize: 16
        color: control.enabled ? Theme.muted : Theme.alpha(Theme.muted, 0.4)
    }

    background: Rectangle {
        implicitWidth: 210
        implicitHeight: 34
        x: 0
        y: 1
        width: control.width
        height: control.height - 2
        radius: Theme.smallRadius - 2
        color: control.down ? Theme.alpha(Theme.accent, 0.24)
            : control.highlighted ? Theme.alpha(Theme.accent, 0.14) : "transparent"
    }
}
