import QtQuick
import QtQuick.Templates as T
import CanExplorer

// Value in the middle, − and + buttons on the sides.
T.SpinBox {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            contentItem.implicitWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding,
                             up.implicitIndicatorHeight, down.implicitIndicatorHeight)

    leftPadding: padding + (control.mirrored ? (up.indicator ? up.indicator.width : 0) : (down.indicator ? down.indicator.width : 0))
    rightPadding: padding + (control.mirrored ? (down.indicator ? down.indicator.width : 0) : (up.indicator ? up.indicator.width : 0))

    validator: IntValidator {
        locale: control.locale.name
        bottom: Math.min(control.from, control.to)
        top: Math.max(control.from, control.to)
    }

    contentItem: TextInput {
        z: 2
        text: control.displayText
        font: control.font
        color: control.enabled ? Theme.text : Theme.alpha(Theme.text, 0.4)
        selectionColor: Theme.accent
        selectedTextColor: "white"
        horizontalAlignment: Qt.AlignHCenter
        verticalAlignment: Qt.AlignVCenter
        readOnly: !control.editable
        validator: control.validator
        inputMethodHints: control.inputMethodHints
        clip: width < implicitWidth
    }

    component StepIndicator: Rectangle {
        property bool pressed: false
        property bool hovered: false
        property string symbol: ""
        property bool active: true
        implicitWidth: 30
        implicitHeight: 34
        height: control.height
        radius: Theme.smallRadius
        color: pressed ? Theme.alpha(Theme.text, 0.16) : hovered && active ? Theme.alpha(Theme.text, 0.08) : "transparent"
        Text {
            anchors.centerIn: parent
            text: parent.symbol
            font.pixelSize: 16
            color: parent.active && control.enabled ? Theme.text : Theme.alpha(Theme.text, 0.3)
        }
    }

    up.indicator: StepIndicator {
        x: control.mirrored ? 0 : control.width - width
        pressed: control.up.pressed
        hovered: control.up.hovered
        active: control.value < control.to
        symbol: "+"
    }

    down.indicator: StepIndicator {
        x: control.mirrored ? control.width - width : 0
        pressed: control.down.pressed
        hovered: control.down.hovered
        active: control.value > control.from
        symbol: "−"
    }

    background: Rectangle {
        implicitWidth: 140
        implicitHeight: 34
        radius: Theme.smallRadius
        color: Theme.cardAlt
        opacity: control.enabled ? 1 : 0.6
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? Theme.accent : Theme.border
    }
}
