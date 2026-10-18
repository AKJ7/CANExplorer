import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.Popup {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            contentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             contentHeight + topPadding + bottomPadding)

    padding: 12

    background: Rectangle {
        color: Theme.card
        radius: Theme.radius
        border.color: Theme.border
    }

    T.Overlay.modal: Rectangle {
        color: Qt.rgba(0, 0, 0, Theme.dark ? 0.55 : 0.35)
    }

    T.Overlay.modeless: Rectangle {
        color: Qt.rgba(0, 0, 0, 0.2)
    }
}
