import QtQuick
import QtQuick.Controls.impl
import QtQuick.Templates as T
import CanExplorer

T.ComboBox {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding,
                             implicitIndicatorHeight + topPadding + bottomPadding)

    leftPadding: padding + (!control.mirrored || !indicator || !indicator.visible ? 0 : indicator.width + spacing)
    rightPadding: padding + (control.mirrored || !indicator || !indicator.visible ? 0 : indicator.width + spacing)

    delegate: ItemDelegate {
        width: ListView.view.width
        text: control.textRole ? (Array.isArray(control.model) ? modelData[control.textRole] : model[control.textRole]) : modelData
        font.weight: control.currentIndex === index ? Font.DemiBold : Font.Normal
        highlighted: control.highlightedIndex === index
        hoverEnabled: control.hoverEnabled
    }

    indicator: Text {
        x: control.mirrored ? control.padding : control.width - width - control.padding - 6
        y: control.topPadding + (control.availableHeight - height) / 2
        text: "▾"
        color: control.enabled ? Theme.muted : Theme.alpha(Theme.muted, 0.4)
        font.pixelSize: 14
    }

    contentItem: T.TextField {
        leftPadding: !control.mirrored ? 12 : control.editable && activeFocus ? 3 : 1
        rightPadding: control.mirrored ? 12 : control.editable && activeFocus ? 3 : 1
        topPadding: 6 - control.padding
        bottomPadding: 6 - control.padding

        text: control.editable ? control.editText : control.displayText

        enabled: control.editable
        autoScroll: control.editable
        readOnly: control.down
        inputMethodHints: control.inputMethodHints
        validator: control.validator
        selectByMouse: control.selectTextByMouse

        font: control.font
        color: control.enabled ? Theme.text : Theme.alpha(Theme.text, 0.4)
        selectionColor: Theme.accent
        selectedTextColor: "white"
        verticalAlignment: Text.AlignVCenter
    }

    background: Rectangle {
        implicitWidth: 140
        implicitHeight: 34
        radius: Theme.smallRadius
        color: control.down ? Theme.alpha(Theme.text, 0.1)
            : control.hovered && control.enabled && !control.editable ? Qt.lighter(Theme.cardAlt, Theme.dark ? 1.25 : 0.97)
            : Theme.cardAlt
        opacity: control.enabled ? 1 : 0.6
        border.width: control.activeFocus || control.visualFocus ? 2 : 1
        border.color: control.activeFocus || control.visualFocus || control.down ? Theme.accent : Theme.border
    }

    popup: T.Popup {
        y: control.height + 4
        width: control.width
        height: Math.min(contentItem.implicitHeight + topPadding + bottomPadding, control.Window.height - topMargin - bottomMargin)
        topMargin: 6
        bottomMargin: 6
        padding: 4

        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.delegateModel
            currentIndex: control.highlightedIndex
            highlightMoveDuration: 0
            boundsBehavior: Flickable.StopAtBounds
            T.ScrollIndicator.vertical: ScrollIndicator {}
        }

        background: GlassPanel {
            radius: Theme.smallRadius + 2
            shadow: false
        }
    }
}
