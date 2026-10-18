import QtQuick
import QtQuick.Controls.impl
import QtQuick.Templates as T
import CanExplorer

T.TextField {
    id: control

    implicitWidth: implicitBackgroundWidth + leftInset + rightInset
                   || Math.max(contentWidth, placeholder.implicitWidth) + leftPadding + rightPadding
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             contentHeight + topPadding + bottomPadding,
                             placeholder.implicitHeight + topPadding + bottomPadding)

    padding: 7
    leftPadding: 12
    rightPadding: 12

    color: control.enabled ? Theme.text : Theme.alpha(Theme.text, 0.4)
    selectionColor: Theme.accent
    selectedTextColor: "white"
    placeholderTextColor: Theme.muted
    verticalAlignment: TextInput.AlignVCenter

    PlaceholderText {
        id: placeholder
        x: control.leftPadding
        y: control.topPadding
        width: control.width - (control.leftPadding + control.rightPadding)
        height: control.height - (control.topPadding + control.bottomPadding)

        text: control.placeholderText
        font: control.font
        color: control.placeholderTextColor
        verticalAlignment: control.verticalAlignment
        visible: !control.length && !control.preeditText && (!control.activeFocus || control.horizontalAlignment !== Qt.AlignHCenter)
        elide: Text.ElideRight
        renderType: control.renderType
    }

    background: Rectangle {
        implicitWidth: 160
        implicitHeight: 34
        radius: Theme.smallRadius
        color: control.readOnly ? Theme.alpha(Theme.cardAlt, 0.5) : Theme.cardAlt
        opacity: control.enabled ? 1 : 0.6
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? Theme.accent
            : control.hovered && !control.readOnly ? Theme.alpha(Theme.accent, 0.5) : Theme.border
    }
}
