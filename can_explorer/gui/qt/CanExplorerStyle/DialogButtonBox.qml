import QtQuick
import QtQuick.Templates as T
import CanExplorer

T.DialogButtonBox {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            (control.count === 1 ? implicitContentWidth * 2 : implicitContentWidth) + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)
    contentWidth: contentItem.contentWidth

    spacing: 10
    padding: 22
    topPadding: 12
    alignment: Qt.AlignRight

    // The button accepting the dialog is the primary one
    delegate: Button {
        width: Math.max(96, implicitWidth)
        highlighted: T.DialogButtonBox.buttonRole === T.DialogButtonBox.AcceptRole
            || T.DialogButtonBox.buttonRole === T.DialogButtonBox.YesRole
            || T.DialogButtonBox.buttonRole === T.DialogButtonBox.ApplyRole
    }

    contentItem: ListView {
        implicitWidth: contentWidth
        model: control.contentModel
        spacing: control.spacing
        orientation: ListView.Horizontal
        boundsBehavior: Flickable.StopAtBounds
        snapMode: ListView.SnapToItem
    }

    background: Item {
        implicitHeight: 34
    }
}
