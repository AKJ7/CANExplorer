import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// The in-app help: the documentation pages (served by the HelpController) with a table of contents, search,
// back/forward history and a link to the full online docs. Opened from the top bar, the "?" button, or F1.
Popup {
    id: root

    property var controller

    modal: true
    focus: true
    padding: 0
    onClosed: if (root.controller) root.controller.set_visible(false)

    // The controller owns the open/closed state so any part of the app can open help; mirror it here.
    Connections {
        target: root.controller
        function onVisible_changed() {
            if (root.controller.visible)
                root.open()
            else
                root.close()
        }
    }

    x: Math.round((parent.width - width) / 2)
    y: Math.round((parent.height - height) / 2)
    width: Math.min(1040, parent.width - 64)
    height: Math.round(parent.height * 0.86)

    property var results: []

    function follow(link) {
        if (link.indexOf("help:") === 0)
            root.controller.navigate(link.substring(5))
        else
            Qt.openUrlExternally(link)
    }

    onOpened: searchField.text = ""

    background: Rectangle {
        color: Theme.background
        radius: Theme.radius
        border.color: Theme.alpha(Theme.border, 0.8)
    }

    contentItem: ColumnLayout {
        spacing: 0

        // Header: history, title, search, online link, close
        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 14
            spacing: 10

            ToolButton {
                text: "←"
                enabled: root.controller && root.controller.can_go_back
                onClicked: root.controller.back()
                ToolTip.visible: hovered; ToolTip.text: qsTr("Back")
            }
            ToolButton {
                text: "→"
                enabled: root.controller && root.controller.can_go_forward
                onClicked: root.controller.forward()
                ToolTip.visible: hovered; ToolTip.text: qsTr("Forward")
            }
            Label {
                text: root.controller ? root.controller.title : ""
                color: Theme.text
                font.pixelSize: 18
                font.bold: true
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            TextField {
                id: searchField
                placeholderText: qsTr("Search the docs…")
                Layout.preferredWidth: 240
                color: Theme.text
                placeholderTextColor: Theme.muted
                onTextChanged: root.results = (text.trim() && root.controller) ? root.controller.search(text) : []
                background: Rectangle {
                    radius: height / 2
                    color: Theme.cardAlt
                    border.color: searchField.activeFocus ? Theme.accent : Theme.alpha(Theme.border, 0.8)
                }
            }
            Button {
                text: qsTr("Open online")
                onClicked: if (root.controller) Qt.openUrlExternally(root.controller.online_url)
            }
            ToolButton {
                text: "✕"
                onClicked: root.close()
                ToolTip.visible: hovered; ToolTip.text: qsTr("Close (Esc)")
            }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.alpha(Theme.border, 0.6) }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Table of contents (or search results when searching)
            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 248
                color: Theme.surface

                ScrollView {
                    anchors.fill: parent
                    clip: true
                    contentWidth: availableWidth

                    ColumnLayout {
                        width: parent.parent.width
                        spacing: 2

                        // Search results
                        Repeater {
                            model: root.results
                            delegate: ItemDelegate {
                                required property var modelData
                                Layout.fillWidth: true
                                text: modelData.title
                                onClicked: { root.controller.navigate(modelData.path); searchField.text = "" }
                                contentItem: ColumnLayout {
                                    spacing: 2
                                    Label { text: modelData.title; color: Theme.text; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                                    Label { text: modelData.snippet; visible: text.length > 0; color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                                }
                            }
                        }

                        // Table of contents
                        Repeater {
                            model: (root.results.length === 0 && root.controller) ? root.controller.sections : []
                            delegate: ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 1

                                Label {
                                    visible: modelData.title.length > 0
                                    text: modelData.title
                                    color: Theme.muted
                                    font.pixelSize: 11
                                    font.bold: true
                                    leftPadding: 16
                                    topPadding: 12
                                    bottomPadding: 2
                                }
                                Repeater {
                                    model: modelData.pages
                                    delegate: ItemDelegate {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        height: 30
                                        text: modelData.title
                                        highlighted: root.controller && root.controller.path === modelData.path
                                        onClicked: root.controller.navigate(modelData.path)
                                        contentItem: Label {
                                            text: parent.text
                                            leftPadding: 22
                                            color: parent.highlighted ? Theme.accent : Theme.text
                                            font.pixelSize: 13
                                            elide: Text.ElideRight
                                            verticalAlignment: Text.AlignVCenter
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Rectangle { Layout.fillHeight: true; width: 1; color: Theme.alpha(Theme.border, 0.6) }

            // Page content: a sequence of text runs and images. Images are rendered as their own Image items so
            // they scale to fit the panel (Qt's Markdown renderer would otherwise draw them at native size).
            ScrollView {
                id: contentScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    width: contentScroll.availableWidth
                    spacing: 12

                    Item { Layout.fillWidth: true; Layout.preferredHeight: 8 }  // Top breathing room

                    Repeater {
                        model: root.controller ? root.controller.blocks : []
                        delegate: ColumnLayout {
                            id: block
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.leftMargin: 24
                            Layout.rightMargin: 24

                            Text {
                                visible: block.modelData.type === "text"
                                Layout.fillWidth: true
                                textFormat: Text.MarkdownText
                                text: block.modelData.type === "text" ? block.modelData.text : ""
                                color: Theme.text
                                wrapMode: Text.WordWrap
                                font.pixelSize: 14
                                linkColor: Theme.accent
                                onLinkActivated: link => root.follow(link)
                            }

                            Image {
                                id: shot
                                visible: block.modelData.type === "image"
                                source: block.modelData.type === "image" ? block.modelData.source : ""
                                fillMode: Image.PreserveAspectFit
                                mipmap: true
                                Layout.alignment: Qt.AlignHCenter
                                // Fit the panel's width, but never upscale beyond the image's native size.
                                Layout.preferredWidth: implicitWidth > 0 ? Math.min(implicitWidth, block.width) : block.width
                                Layout.preferredHeight: (implicitWidth > 0 && implicitHeight > 0)
                                    ? Layout.preferredWidth * (implicitHeight / implicitWidth) : implicitHeight
                            }
                        }
                    }

                    Item { Layout.fillWidth: true; Layout.preferredHeight: 12 }  // Bottom breathing room
                }
            }
        }
    }
}
