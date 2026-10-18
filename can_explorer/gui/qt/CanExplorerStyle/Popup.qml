import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.Popup {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    padding: 12

    background: GlassPanel {}

    T.Overlay.modal: Rectangle {
        color: Qt.rgba(0, 0, 0, Theme.dark ? 0.35 : 0.18)
    }

    T.Overlay.modeless: Rectangle {
        color: Qt.rgba(0, 0, 0, 0.2)
    }
}
