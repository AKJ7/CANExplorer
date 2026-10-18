import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Search and bookmarks of the trace.
RowLayout {
    id: root
    required property var tools  // connection.trace_tools
    required property var table  // The trace FrameTable

    spacing: 8

    function focusSearch() {
        findField.forceActiveFocus()
        findField.selectAll()
    }

    function find(forward) {
        if (findField.text.trim().length > 0)
            root.tools.find(findField.text, root.table.currentRow, forward)
    }

    function toggleBookmarks() {
        return root.tools.toggle_bookmarks(root.table.selectionModel)
    }

    Connections {
        target: root.tools
        function onSearch_finished(row, message) {
            if (row >= 0)
                root.table.showRow(row)
        }
    }

    TextField {
        id: findField
        placeholderText: qsTr("Find: text, 7E8, data=02 ?? 0D or EngineData.EngineSpeed > 3000")
        Layout.fillWidth: true
        Layout.maximumWidth: 520
        onAccepted: root.find(true)
        Keys.onEscapePressed: root.tools.cancel()
        ToolTip.visible: hovered && text.length === 0
        ToolTip.delay: 800
        ToolTip.text: qsTr("Signal comparisons, identifiers and data patterns as for triggers, or any text of the row. "
                           + "Quotes search for the text: \"BEEF\". Enter or F3: next, Shift+F3: previous.")
    }
    ToolButton {
        text: "▲"
        enabled: findField.text.length > 0
        ToolTip.visible: hovered
        ToolTip.text: qsTr("Previous (Shift+F3)")
        onClicked: root.find(false)
    }
    ToolButton {
        text: "▼"
        enabled: findField.text.length > 0
        ToolTip.visible: hovered
        ToolTip.text: qsTr("Next (F3)")
        onClicked: root.find(true)
    }
    BusyIndicator {
        visible: root.tools.searching
        running: visible
        Layout.preferredWidth: 22
        Layout.preferredHeight: 22
    }
    Label {
        text: root.tools.status
        color: root.tools.status === "No match" ? Theme.warning : Theme.muted
        elide: Text.ElideRight
        Layout.fillWidth: true
    }

    Button {
        id: bookmarksButton
        text: qsTr("🔖 %1").arg(root.tools.bookmark_count)
        ToolTip.visible: hovered
        ToolTip.text: qsTr("Bookmarks (Ctrl+B bookmarks the selected rows)")
        onClicked: bookmarkPopup.open()

        Popup {
            id: bookmarkPopup
            y: bookmarksButton.height + 6
            x: bookmarksButton.width - width
            width: 560
            padding: 14
            contentItem: ColumnLayout {
                spacing: 8
                RowLayout {
                    Label {
                        text: qsTr("Bookmarks")
                        color: Theme.text
                        font.bold: true
                        Layout.fillWidth: true
                    }
                    Button {
                        text: qsTr("Clear all")
                        flat: true
                        enabled: root.tools.bookmark_count > 0
                        onClicked: root.tools.clear()
                    }
                }
                Label {
                    visible: root.tools.bookmark_count === 0
                    text: qsTr("Bookmark rows with Ctrl+B or the context menu of the trace.")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                ListView {
                    clip: true
                    spacing: 4
                    model: root.tools.bookmarks
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(contentHeight, 380)
                    ScrollBar.vertical: ScrollBar {}
                    delegate: ItemDelegate {
                        id: entry
                        required property var modelData
                        width: ListView.view.width - 6
                        enabled: modelData.available
                        onClicked: {
                            const row = root.tools.row_of(modelData.index)
                            if (row >= 0) {
                                root.table.showRow(row)
                                bookmarkPopup.close()
                            }
                        }
                        contentItem: ColumnLayout {
                            spacing: 2
                            RowLayout {
                                spacing: 10
                                Rectangle { width: 4; height: 16; color: Theme.warning }
                                Label { text: entry.modelData.time; color: Theme.muted; font.family: "monospace" }
                                Label { text: entry.modelData.id; color: Theme.accent; font.family: "monospace" }
                                Label {
                                    text: entry.modelData.info
                                    color: Theme.text
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                ToolButton {
                                    text: "✕"
                                    ToolTip.visible: hovered
                                    ToolTip.text: qsTr("Remove the bookmark")
                                    onClicked: root.tools.remove(entry.modelData.index)
                                }
                            }
                            TextField {
                                text: entry.modelData.note
                                placeholderText: qsTr("Note")
                                Layout.fillWidth: true
                                onEditingFinished: if (text !== entry.modelData.note) root.tools.set_note(entry.modelData.index, text)
                            }
                        }
                    }
                }
            }
        }
    }
}
