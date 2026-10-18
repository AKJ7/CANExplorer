import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Key figure with an icon, a trend and a sparkline.
Card {
    id: root
    property string icon: ""
    property string value: ""
    property string label: ""
    property string trend: ""
    property bool trendPositive: true
    property color accentColor: Theme.accent
    property var history: []

    implicitHeight: 168
    Layout.fillWidth: true
    Layout.preferredHeight: implicitHeight

    ColumnLayout {
        anchors.fill: parent
        spacing: 4

        Rectangle {
            width: 44
            height: 44
            radius: 22
            color: root.accentColor
            Label {
                anchors.centerIn: parent
                text: root.icon
                color: "white"
                font.pixelSize: 20
                font.bold: true
            }
        }
        Item { Layout.preferredHeight: 4 }
        Label {
            text: root.value
            color: Theme.text
            font.pixelSize: Theme.valueSize
            font.bold: true
        }
        Label {
            text: root.label
            color: Theme.muted
        }
        RowLayout {
            Layout.fillWidth: true
            Label {
                text: root.trend
                color: root.trendPositive ? Theme.success : Theme.danger
                font.bold: true
                Layout.fillWidth: true
            }
            Sparkline {
                values: root.history
                color: root.accentColor
                Layout.preferredWidth: 96
                Layout.preferredHeight: 28
            }
        }
    }
}
