import QtQuick
import QtQuick.Templates as T
import CanExplorer

// Card with a title, the content and right aligned buttons; the window behind is dimmed.
T.Dialog {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding,
                            implicitHeaderWidth,
                            implicitFooterWidth)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding
                             + (implicitHeaderHeight > 0 ? implicitHeaderHeight + spacing : 0)
                             + (implicitFooterHeight > 0 ? implicitFooterHeight + spacing : 0))

    padding: 22
    topPadding: 6

    enter: Transition {
        NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 120 }
        NumberAnimation { property: "scale"; from: 0.96; to: 1.0; duration: 120; easing.type: Easing.OutCubic }
    }
    exit: Transition {
        NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 90 }
    }

    background: GlassPanel {
        radius: Theme.radius + 4
    }

    // The title moves the dialog. Centred dialogs are anchored, which would pin them: the anchor is released
    // while the dialog is dragged and restored when it closes, so it opens centred again.
    property var _anchor: undefined
    property bool _moved: false
    onClosed: {
        if (_moved) {
            anchors.centerIn = _anchor
            _moved = false
        }
    }

    header: T.Label {
        id: titleLabel
        text: control.title
        visible: control.title
        // No eliding: an elided title makes the dialog's implicit width depend on itself (binding loop)
        font.bold: true
        font.pixelSize: Theme.titleSize + 1
        color: Theme.text
        leftPadding: 22
        rightPadding: 22
        topPadding: 20
        bottomPadding: 10

        HoverHandler {
            cursorShape: dragHandler.active ? Qt.ClosedHandCursor : Qt.OpenHandCursor
        }
        DragHandler {
            id: dragHandler
            target: null
            property point start
            onActiveChanged: {
                if (!active)
                    return
                if (!control._moved) {
                    control._anchor = control.anchors.centerIn
                    const x = control.x, y = control.y
                    control.anchors.centerIn = undefined
                    control.x = x
                    control.y = y
                    control._moved = true
                }
                start = Qt.point(control.x, control.y)
            }
            onTranslationChanged: {
                if (!active)
                    return
                // Keep the dialog inside the window (x and y are relative to the item the dialog is declared in)
                const overlay = control.T.Overlay.overlay
                const origin = overlay && control.parent ? control.parent.mapToItem(overlay, 0, 0) : Qt.point(0, 0)
                const width = overlay ? overlay.width : 1e6, height = overlay ? overlay.height : 1e6
                control.x = Math.max(-origin.x, Math.min(width - origin.x - control.width, start.x + translation.x))
                control.y = Math.max(-origin.y, Math.min(height - origin.y - control.height, start.y + translation.y))
            }
        }
    }

    footer: DialogButtonBox {
        visible: count > 0
    }

    T.Overlay.modal: Rectangle {
        color: Qt.rgba(0, 0, 0, Theme.dark ? 0.35 : 0.18)
    }

    T.Overlay.modeless: Rectangle {
        color: Qt.rgba(0, 0, 0, 0.2)
    }
}
