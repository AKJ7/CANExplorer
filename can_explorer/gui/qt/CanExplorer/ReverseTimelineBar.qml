import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Temporal "flow" playback for the Bits tab: scrub through the selected identifier's frames, or play them back,
// and watch the bit grid above show the payload at that moment.
Rectangle {
    id: root
    required property var timeline

    implicitHeight: 44
    radius: Theme.smallRadius
    color: Theme.alpha(Theme.cardAlt, 0.6)
    border.color: Theme.alpha(Theme.border, 0.6)

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        spacing: 8

        Button {
            text: root.timeline.playing ? qsTr("❚❚") : qsTr("▶")
            highlighted: root.timeline.playing  // Fill while playing, so the state is clear
            implicitWidth: 40
            horizontalPadding: 0  // The style's wide text padding would clip the icon at this width
            ToolTip.visible: hovered
            ToolTip.text: root.timeline.playing ? qsTr("Pause") : qsTr("Play")
            onClicked: root.timeline.toggle()
        }
        Button {
            text: qsTr("«")
            implicitWidth: 34
            horizontalPadding: 0
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Previous frame")
            onClicked: root.timeline.step(-1)
        }
        Button {
            text: qsTr("»")
            implicitWidth: 34
            horizontalPadding: 0
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Next frame")
            onClicked: root.timeline.step(1)
        }

        Slider {
            id: scrubber
            from: 0
            to: 1
            value: root.timeline.position
            Layout.fillWidth: true
            // Dragging scrubs; playback moves the handle via the value binding above
            onMoved: root.timeline.set_position(value)
            ToolTip.visible: hovered || pressed
            ToolTip.text: root.timeline.time_text
        }

        Label {
            text: root.timeline.time_text
            color: Theme.text
            font.family: "monospace"
            font.pixelSize: 12
            Layout.minimumWidth: 70
            horizontalAlignment: Text.AlignRight
        }
        Label {
            text: qsTr("frame %1 / %2").arg(root.timeline.frame).arg(root.timeline.frames)
            color: Theme.muted
            font.pixelSize: 12
            Layout.minimumWidth: 110
        }

        Label { text: qsTr("Speed"); color: Theme.muted; font.pixelSize: 12 }
        ComboBox {
            id: speedBox
            model: root.timeline.speeds
            currentIndex: root.timeline.speeds.indexOf(root.timeline.speed)
            displayText: qsTr("%1×").arg(currentValue)
            Layout.preferredWidth: 80
            onActivated: root.timeline.set_speed(currentValue)
        }
    }
}
