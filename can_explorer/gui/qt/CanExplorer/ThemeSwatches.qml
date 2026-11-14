import QtQuick 2.15
import CanExplorer 1.0

// A strip of colour swatches previewing a theme: its background and card, then accent, success, warning and danger.
// "System" shows the palette it currently resolves to.
Rectangle {
    id: root
    property string themeName: Theme.name
    readonly property var palette: Theme.palettes[Theme.resolve(themeName)] || Theme.palettes["Dark"]

    implicitWidth: row.implicitWidth + 8
    implicitHeight: 20
    radius: 5
    color: palette.background
    border.width: 1
    border.color: palette.border

    Row {
        id: row
        anchors.centerIn: parent
        spacing: 3
        Repeater {
            model: ["card", "accent", "success", "warning", "danger"]
            Rectangle {
                required property string modelData
                width: 12
                height: 12
                radius: 3
                color: root.palette[modelData]
                border.width: modelData === "card" ? 1 : 0
                border.color: root.palette.border
            }
        }
    }
}
