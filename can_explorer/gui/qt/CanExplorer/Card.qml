import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Rounded panel with an optional title; children are placed below the title.
Rectangle {
    id: root
    property string title: ""
    property alias headerItems: header.data
    default property alias content: body.data
    property int padding: Theme.spacing

    color: Theme.card
    radius: Theme.radius
    border.color: Theme.alpha(Theme.border, 0.6)
    border.width: 1

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: root.padding
        spacing: 10

        RowLayout {
            id: header
            visible: root.title.length > 0 || children.length > 1
            Layout.fillWidth: true
            Label {
                text: root.title
                visible: text.length > 0
                color: Theme.text
                font.pixelSize: Theme.titleSize
                font.bold: true
                Layout.fillWidth: true
            }
        }

        Item {
            id: body
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }
}
