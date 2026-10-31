import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0
import CanExplorer.Plots 1.0

// An interactive Python console over the captured frames, with the reverse-engineering primitives (collect,
// analyse, BitField, candidates, signal_groups). A transcript above, an editor below; Ctrl+Enter runs.
ColumnLayout {
    id: root
    required property var tools
    readonly property var consoleTool: tools.console

    signal fieldUsed()  // Part of the common panel interface; the console does not use them
    signal plotted()
    signal message(string text)

    spacing: 8

    function run() {
        if (input.text.trim().length > 0)
            root.consoleTool.run(input.text)
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true

        Button {
            text: qsTr("Run")
            highlighted: true
            enabled: !root.consoleTool.running && input.text.trim().length > 0
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Run the snippet over the frames captured so far (Ctrl+Enter)")
            onClicked: root.run()
        }
        Button {
            text: qsTr("Clear")
            enabled: root.consoleTool.entries.length > 0
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Clear the transcript and forget all variables")
            onClicked: root.consoleTool.clear()
        }
        BusyIndicator {
            running: root.consoleTool.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.consoleTool.status
            color: Theme.warning
            font.pixelSize: 12
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    // Transcript (or the help text before anything has run)
    Card {
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            ScrollView {
                visible: root.consoleTool.entries.length === 0
                Layout.fillWidth: true
                Layout.fillHeight: true
                Label {
                    text: root.consoleTool.help_text
                    color: Theme.muted
                    font.family: "monospace"
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                    width: root.width - 2 * Theme.spacing
                }
            }

            ListView {
                id: transcript
                visible: root.consoleTool.entries.length > 0
                clip: true
                model: root.consoleTool.entries
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 10
                ScrollBar.vertical: ScrollBar {}
                onCountChanged: positionViewAtEnd()

                delegate: ColumnLayout {
                    required property var modelData
                    width: transcript.width - 12
                    spacing: 1

                    Label {
                        text: "›  " + modelData.source.replace(/\n/g, "\n   ")
                        color: Theme.accent
                        font.family: "monospace"
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    Label {
                        visible: modelData.output.length > 0
                        text: modelData.output
                        color: Theme.muted
                        font.family: "monospace"
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    Label {
                        visible: modelData.value.length > 0
                        text: modelData.value
                        color: Theme.text
                        font.family: "monospace"
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    Label {
                        visible: modelData.error.length > 0
                        text: modelData.error
                        color: Theme.danger
                        font.family: "monospace"
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
            }
        }
    }

    // Editor
    ScrollView {
        Layout.fillWidth: true
        Layout.preferredHeight: 120

        TextArea {
            id: input
            objectName: "consoleInput"
            placeholderText: qsTr("Python here, Ctrl+Enter to run — e.g.  [(f.field.key, f.kind) for f in analyse(frames_of('100')).fields]   (type HELP for the available names)")
            font.family: "monospace"
            font.pixelSize: 13
            wrapMode: TextEdit.NoWrap
            selectByMouse: true
            color: Theme.text
            background: Rectangle {
                color: Theme.cardAlt
                radius: Theme.smallRadius
                border.color: input.activeFocus ? Theme.accent : Theme.border
                border.width: 1
            }
            Keys.onPressed: (event) => {
                if ((event.modifiers & Qt.ControlModifier) && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                    root.run()
                    event.accepted = true
                }
            }

            PythonHighlighter {
                quickDocument: input.textDocument
                keywordColor: Theme.accent
                nameColor: Theme.chart[4]
                numberColor: Theme.chart[2]
                stringColor: Theme.success
                commentColor: Theme.muted
                definitionColor: Theme.warning
            }
        }
    }
}
