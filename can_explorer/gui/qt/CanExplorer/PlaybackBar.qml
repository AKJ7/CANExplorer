import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Controls of a playback (a replayed log file, or a log file transmitted on the bus): pause, single step,
// breakpoints, progress and speed.
Card {
    id: root
    required property var playback
    property string heading: ""
    // Optional actions: stop, restart and hide; shown when set
    property var stopAction: null
    property var restartAction: null
    property var dismissAction: null

    padding: 12
    implicitHeight: root.playback.hit_text ? 88 : 64

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

    ColumnLayout {
    anchors.fill: parent
    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
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
                color: Theme.onAccent
                font: playButton.font
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        ToolButton {
            text: "⏭"
            enabled: root.running
            font.pixelSize: 14
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Step: play the next frame, then pause")
            onClicked: root.playback.step()
        }
        ToolButton {
            id: breakpointsButton
            text: root.playback.breakpoint_rows.length > 0 ? "● " + root.playback.breakpoint_rows.length : "●"
            font.pixelSize: 13
            palette.buttonText: root.playback.breakpoint_rows.some(b => b.enabled) ? Theme.danger : Theme.muted
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Breakpoints: pause the playback after a frame, a signal condition or a time")
            onClicked: breakpointsPopup.open()
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
            text: "◀◀"
            font.pixelSize: 12
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
            text: "▶▶"
            font.pixelSize: 12
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

    // Where the playback stopped
    Label {
        visible: text.length > 0
        text: root.playback.hit_text
        color: Theme.warning
        font.family: "monospace"
        font.pixelSize: 12
        elide: Text.ElideRight
        Layout.fillWidth: true
    }
    }

    Popup {
        id: breakpointsPopup
        y: -height - 8
        x: Math.max(0, breakpointsButton.x - 40)
        width: 560
        padding: 12
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        function report(error) {
            breakpointError.text = error || ""
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: 8

            Label { text: qsTr("Breakpoints"); font.bold: true }
            Label {
                visible: root.playback.breakpoint_rows.length === 0
                text: qsTr("The playback pauses right after a frame that hits a breakpoint; Resume or Step goes on.")
                color: Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Repeater {
                model: root.playback.breakpoint_rows
                delegate: RowLayout {
                    required property var modelData
                    required property int index
                    Layout.fillWidth: true
                    CheckBox {
                        checked: modelData.enabled
                        onToggled: root.playback.set_breakpoint_enabled(index, checked)
                    }
                    Label { text: modelData.label; elide: Text.ElideRight; Layout.fillWidth: true }
                    Label {
                        text: modelData.skip > 0 ? qsTr("%1 hits (passes %2)").arg(modelData.hits).arg(modelData.skip)
                                                 : qsTr("%1 hits").arg(modelData.hits)
                        color: Theme.muted
                        font.pixelSize: 12
                    }
                    ToolButton {
                        text: "✕"
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Remove")
                        onClicked: root.playback.remove_breakpoint(index)
                    }
                }
            }
            MenuSeparator { Layout.fillWidth: true }
            RowLayout {
                Layout.fillWidth: true
                ComboBox {
                    id: kindBox
                    model: [qsTr("Frame"), qsTr("Signal condition"), qsTr("Time")]
                    Layout.preferredWidth: 150
                }
                TextField {
                    id: firstField
                    placeholderText: kindBox.currentIndex === 0 ? qsTr("Identifiers, e.g. 7DF or 100-1FF ext")
                                   : kindBox.currentIndex === 1 ? qsTr("Message.Signal > 3000, or … changes")
                                   : qsTr("Seconds, e.g. 12.5")
                    Layout.fillWidth: true
                    onAccepted: addButton.clicked()
                }
                TextField {
                    id: dataField
                    visible: kindBox.currentIndex === 0
                    placeholderText: qsTr("Data, e.g. 02 10 ??")
                    Layout.preferredWidth: 130
                    onAccepted: addButton.clicked()
                }
                SpinBox {
                    id: skipBox
                    visible: kindBox.currentIndex !== 2
                    from: 0; to: 100000; value: 0
                    editable: true
                    Layout.preferredWidth: 110
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Hits to let pass before pausing (e.g. 9 to stop at the 10th)")
                }
                Button {
                    id: addButton
                    text: qsTr("Add")
                    onClicked: {
                        let error = ""
                        if (kindBox.currentIndex === 0)
                            error = root.playback.add_frame_breakpoint(firstField.text, dataField.text, skipBox.value)
                        else if (kindBox.currentIndex === 1)
                            error = root.playback.add_signal_breakpoint(firstField.text, skipBox.value)
                        else {
                            const seconds = Number(firstField.text.replace(",", "."))
                            error = isNaN(seconds) ? qsTr("Not a number of seconds") : root.playback.add_time_breakpoint(seconds)
                        }
                        breakpointsPopup.report(error)
                        if (!error) {
                            firstField.text = ""
                            dataField.text = ""
                            skipBox.value = 0
                        }
                    }
                }
            }
            Label {
                id: breakpointError
                visible: text.length > 0
                color: text.startsWith("Added") ? Theme.muted : Theme.danger
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Remove All")
                    enabled: root.playback.breakpoint_rows.length > 0
                    onClicked: root.playback.clear_breakpoints()
                }
                Button { text: qsTr("Close"); onClicked: breakpointsPopup.close() }
            }
        }
    }
}
