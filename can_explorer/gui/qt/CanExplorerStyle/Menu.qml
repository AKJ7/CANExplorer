import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.Menu {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            contentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             contentHeight + topPadding + bottomPadding)

    margins: 0
    padding: 6
    overlap: 4

    delegate: MenuItem {}

    contentItem: ListView {
        implicitHeight: contentHeight
        model: control.contentModel
        interactive: Window.window
            ? contentHeight + control.topPadding + control.bottomPadding > control.height
            : false
        clip: true
        currentIndex: control.currentIndex
        T.ScrollIndicator.vertical: ScrollIndicator {}
    }

    enter: Transition {
        NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 100 }
    }

    background: Rectangle {
        implicitWidth: 230
        implicitHeight: 38
        color: Theme.card
        radius: Theme.smallRadius + 2
        border.color: Theme.border
    }

    T.Overlay.modal: Rectangle {
        color: Qt.rgba(0, 0, 0, 0.2)
    }
}
