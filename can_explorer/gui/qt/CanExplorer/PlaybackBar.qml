import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Controls of a playback (a replayed log file, or a log file transmitted on the bus): pause, progress and speed.
Card {
    id: root
    required property var playback
    property string heading: ""
    // Optional actions: stop, restart and hide; shown when set
    property var stopAction: null
    property var restartAction: null
    property var dismissAction: null

    padding: 12
    implicitHeight: 64

    function formatTime(seconds) {
        if (seconds < 0)
            return "–"
        const minutes = Math.floor(seconds / 60)
        const rest = seconds - minutes * 60
        return `${minutes}:${rest < 10 ? "0" : ""}${rest.toFixed(1)}`
    }

    readonly property bool running: playback.state === "playing" || playback.state === "paused"
    readonly property color stateColor: playback.state === "playing" ? Theme.success
        : playback.state === "paused" ? Theme.warning
        : playback.state === "error" ? Theme.danger : Theme.muted

    RowLayout {
        anchors.fill: parent
        spacing: 12

        RoundButton {
            id: playButton
            text: root.running && !root.playback.paused ? "❚❚" : "▶"
            enabled: root.running || root.restartAction !== null
            highlighted: true
            font.pixelSize: 13
            Layout.preferredWidth: 38
            Layout.preferredHeight: 38
            ToolTip.visible: hovered
            ToolTip.text: !root.running ? qsTr("Play again") : root.playback.paused ? qsTr("Resume") : qsTr("Pause")
            onClicked: {
                if (root.running)
                    root.playback.set_paused(!root.playback.paused)
                else if (root.restartAction)
                    root.restartAction()
            }
            background: Rectangle {
                radius: width / 2
                color: playButton.enabled ? (playButton.hovered ? Qt.lighter(Theme.accent, 1.12) : Theme.accent)
                    : Theme.alpha(Theme.accent, 0.35)
            }
            contentItem: Label {
                text: playButton.text
                color: "white"
                font: playButton.font
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        ColumnLayout {
            spacing: 2
            Layout.preferredWidth: 220
            Label {
                text: root.heading || root.playback.file_name
                color: Theme.text
                font.bold: true
                elide: Text.ElideMiddle
                Layout.fillWidth: true
            }
            RowLayout {
                spacing: 8
                Pill {
                    text: root.playback.state
                    tint: root.stateColor
                }
                Label {
                    text: qsTr("%1 frames").arg(root.playback.frames.toLocaleString(Qt.locale(), "f", 0))
                    color: Theme.muted
                    font.pixelSize: 12
                }
            }
        }

        ToolButton {
            text: "⏪"
            enabled: root.playback.can_seek
            ToolTip.visible: hovered
            ToolTip.text: qsTr("10 s back")
            onClicked: root.playback.seek(root.playback.position - 10)
        }
        Label {
            text: root.formatTime(positionSlider.pressed ? positionSlider.value : root.playback.position)
            color: Theme.text
            font.family: "monospace"
        }
        // Drag to jump within the recording; the jump happens on release
        Slider {
            id: positionSlider
            from: 0
            to: Math.max(root.playback.duration, 0.001)
            enabled: root.playback.can_seek
            Layout.fillWidth: true
            onPressedChanged: if (!pressed) root.playback.seek(value)
            // Follows the playback, except while dragged (dragging would otherwise end a plain binding)
            Binding on value {
                value: root.playback.position
                when: !positionSlider.pressed
                restoreMode: Binding.RestoreNone
            }
            ToolTip.visible: hovered && !enabled && root.running
            ToolTip.text: qsTr("The length of the recording is being determined")
        }
        ToolButton {
            text: "⏩"
            enabled: root.playback.can_seek
            ToolTip.visible: hovered
            ToolTip.text: qsTr("10 s forward")
            onClicked: root.playback.seek(root.playback.position + 10)
        }
        Label {
            text: root.formatTime(root.playback.duration)
            color: Theme.muted
            font.family: "monospace"
        }

        ComboBox {
            id: speedBox
            model: root.playback.speed_labels
            currentIndex: Math.max(0, root.playback.speeds.indexOf(root.playback.speed))
            onActivated: index => root.playback.set_speed(root.playback.speeds[index])
            Layout.preferredWidth: 96
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Playback speed relative to the recorded timing")
        }

        ToolButton {
            visible: root.stopAction !== null && root.running
            text: "■"
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Stop")
            onClicked: root.stopAction()
        }
        ToolButton {
            visible: root.dismissAction !== null && !root.running
            text: "✕"
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Close")
            onClicked: root.dismissAction()
        }
    }
}
