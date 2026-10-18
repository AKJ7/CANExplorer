import QtQuick
import QtQuick.Templates as T
import CanExplorer

// Card with a title, the content and right aligned buttons; the window behind is dimmed.
T.Dialog {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            contentWidth + leftPadding + rightPadding,
                            implicitHeaderWidth,
                            implicitFooterWidth)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             contentHeight + topPadding + bottomPadding
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

    background: Rectangle {
        color: Theme.card
        radius: Theme.radius
        border.color: Theme.border
    }

    header: T.Label {
        text: control.title
        visible: control.title
        elide: Text.ElideRight
        font.bold: true
        font.pixelSize: Theme.titleSize + 1
        color: Theme.text
        leftPadding: 22
        rightPadding: 22
        topPadding: 20
        bottomPadding: 10
    }

    footer: DialogButtonBox {
        visible: count > 0
    }

    T.Overlay.modal: Rectangle {
        color: Qt.rgba(0, 0, 0, Theme.dark ? 0.55 : 0.35)
    }

    T.Overlay.modeless: Rectangle {
        color: Qt.rgba(0, 0, 0, 0.2)
    }
}
