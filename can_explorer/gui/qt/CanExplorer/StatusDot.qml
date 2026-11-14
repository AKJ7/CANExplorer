import QtQuick 2.15
import QtQuick.Controls 2.15
import CanExplorer 1.0

// A small status indicator whose shape, not only its colour, tells the state, so it reads for colour-blind users
// and in the monochrome Night Vision theme:
//   "ok"      — filled circle (success colour)      "idle"  — hollow ring (muted)
//   "warning" — filled diamond (warning colour)     "error" — filled circle with "!" (danger colour)
// An empty status hides it. Pair it with a tooltip or label naming the state where there is room.
Item {
    id: root
    property string status: ""
    property int size: 10
    readonly property color tint: status === "ok" ? Theme.success
        : status === "warning" ? Theme.warning
        : status === "error" ? Theme.danger : Theme.muted

    visible: status !== ""
    // The error mark is a little larger so its "!" stays legible
    implicitWidth: status === "error" ? size + 4 : size
    implicitHeight: implicitWidth

    Rectangle {
        visible: root.status === "ok" || root.status === "idle" || root.status === "error"
        anchors.fill: parent
        radius: width / 2
        color: root.status === "idle" ? "transparent" : root.tint
        border.width: root.status === "idle" ? 2 : 0
        border.color: root.tint
    }
    Rectangle {
        visible: root.status === "warning"
        anchors.centerIn: parent
        width: Math.round(root.size * 0.78)
        height: width
        rotation: 45
        radius: 1.5
        color: root.tint
    }
    Text {
        visible: root.status === "error"
        anchors.centerIn: parent
        text: "!"
        color: Theme.onDanger
        font.pixelSize: root.size
        font.bold: true
    }
}
