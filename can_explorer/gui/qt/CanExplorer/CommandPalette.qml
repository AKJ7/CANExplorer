import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Ctrl+K: find and run any command, view, tool, connection or theme by typing part of its name.
Popup {
    id: root
    // Called when the palette opens; returns [{ title, category, shortcut, run, enabled }]
    property var provider: function() { return [] }
    property var commands: []
    readonly property var matches: filter(commands, field.text)

    modal: true
    focus: true
    padding: 10
    x: Math.round((parent.width - width) / 2)
    y: Math.round(parent.height * 0.12)
    width: Math.min(640, parent.width - 48)
    height: Math.min(460, parent.height * 0.7)

    // Subsequence match: every character of the query in order, scored by how close and early they are
    function score(text, query) {
        const haystack = text.toLowerCase()
        let position = -1, score = 0, previous = -2
        for (const character of query.toLowerCase()) {
            if (character === " ")
                continue
            position = haystack.indexOf(character, position + 1)
            if (position < 0)
                return -1
            score += position === previous + 1 ? 3 : 1  // Consecutive characters count more
            if (position === 0 || " .:/-(".includes(haystack[position - 1]))
                score += 2  // Starts of words
            previous = position
        }
        return score - haystack.length * 0.01
    }

    function filter(commands, query) {
        if (!query.trim())
            return commands
        return commands
            .map(command => ({ command: command, score: score(`${command.category}: ${command.title}`, query) }))
            .filter(entry => entry.score >= 0)
            .sort((a, b) => b.score - a.score)
            .map(entry => entry.command)
    }

    function run(index) {
        const command = matches[index]
        if (!command || command.enabled === false)
            return
        close()
        command.run()
    }

    onAboutToShow: {
        commands = provider()
        field.text = ""
        list.currentIndex = 0
    }
    onOpened: field.forceActiveFocus()

    contentItem: ColumnLayout {
        spacing: 8

        TextField {
            id: field
            placeholderText: qsTr("Type a command, view, tool, connection or theme…")
            Layout.fillWidth: true
            onTextChanged: list.currentIndex = 0
            Keys.onDownPressed: list.currentIndex = Math.min(list.count - 1, list.currentIndex + 1)
            Keys.onUpPressed: list.currentIndex = Math.max(0, list.currentIndex - 1)
            Keys.onReturnPressed: root.run(list.currentIndex)
            Keys.onEnterPressed: root.run(list.currentIndex)
            Keys.onEscapePressed: root.close()
        }

        ListView {
            id: list
            clip: true
            Layout.fillWidth: true
            Layout.fillHeight: true
            model: root.matches
            highlightMoveDuration: 0
            ScrollBar.vertical: ScrollBar {}
            delegate: ItemDelegate {
                id: item
                required property var modelData
                required property int index
                width: list.width - 8
                highlighted: ListView.isCurrentItem
                enabled: modelData.enabled !== false
                onClicked: root.run(index)
                contentItem: RowLayout {
                    spacing: 10
                    Label {
                        text: item.modelData.category
                        color: Theme.muted
                        font.pixelSize: 12
                        Layout.preferredWidth: 100
                        elide: Text.ElideRight
                    }
                    Label {
                        text: item.modelData.title
                        color: item.enabled ? Theme.text : Theme.muted
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Label {
                        text: item.modelData.shortcut || ""
                        color: Theme.muted
                        font.pixelSize: 12
                        visible: text.length > 0
                    }
                }
            }
        }

        Label {
            visible: list.count === 0
            text: qsTr("No matching command")
            color: Theme.muted
            Layout.alignment: Qt.AlignHCenter
        }
        Label {
            text: qsTr("↑↓ to choose · Enter to run · Esc to close")
            color: Theme.muted
            font.pixelSize: 11
            Layout.alignment: Qt.AlignRight
        }
    }
}
