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
        text: control.textAt(index)  // Honours textRole for every kind of model (lists, arrays of objects, models)
        font.weight: control.currentIndex === index ? Font.DemiBold : Font.Normal
        highlighted: control.highlightedIndex === index
        hoverEnabled: control.hoverEnabled
    }

    // A drawn chevron rather than a "▾" character: the glyph was tiny, sat below the centre, hugged the border and
    // depends on the font having it. It points up while the list is open.
    indicator: Item {
        implicitWidth: 30
        implicitHeight: 24
        x: control.mirrored ? control.padding : control.width - width - control.padding
        y: control.topPadding + (control.availableHeight - height) / 2

        Chevron {
            anchors.centerIn: parent
            anchors.horizontalCenterOffset: control.mirrored ? 2 : -2
            color: !control.enabled ? Theme.alpha(Theme.muted, 0.4)
                : control.popup.visible || control.down ? Theme.accent
                : control.hovered ? Theme.text : Theme.muted
            rotation: control.popup.visible ? 180 : 0
        }
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
        selectedTextColor: Theme.onAccent
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
            id: popupList
            clip: true
            // An empty model would collapse the popup to its padding; keep room for the placeholder instead.
            implicitHeight: control.count > 0 ? contentHeight : 32
            model: control.delegateModel
            currentIndex: control.highlightedIndex
            highlightMoveDuration: 0
            boundsBehavior: Flickable.StopAtBounds
            T.ScrollIndicator.vertical: ScrollIndicator {}

            Text {
                visible: control.count === 0
                width: popupList.width
                height: 32
                leftPadding: 12
                rightPadding: 12
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                text: qsTr("No items")
                color: Theme.muted
                font.family: control.font.family
                font.pixelSize: control.font.pixelSize
                font.italic: true
            }
        }

        background: GlassPanel {
            radius: Theme.smallRadius + 2
            shadow: false
        }
    }
}
