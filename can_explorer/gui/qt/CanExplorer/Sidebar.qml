import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import CanExplorer 1.0

// Navigation: connections, the views of the selected connection, settings and application information.
Rectangle {
    id: root
    property var connections: []
    property int currentConnection: -1
    property var pages: []
    property string currentPage: ""
    property string currentTool: ""

    signal toolSelected(string tool)

    signal connectionSelected(int index)
    signal pageSelected(string page)

    color: Theme.surface
    implicitWidth: Theme.sidebarWidth

    Rectangle {
        anchors.right: parent.right
        width: 1
        height: parent.height
        color: Theme.alpha(Theme.border, 0.6)
    }

    component SectionLabel: Label {
        color: Theme.muted
        font.pixelSize: 11
        font.bold: true
        font.letterSpacing: 1.2
        leftPadding: 22
        topPadding: 14
        bottomPadding: 4
    }

    component NavItem: ItemDelegate {
        id: item
        property string glyph: ""
        property bool active: false
        property color dotColor: "transparent"
        Layout.fillWidth: true
        implicitHeight: 42
        hoverEnabled: true

        background: Rectangle {
            color: item.active ? Theme.alpha(Theme.accent, 0.14)
                : item.hovered ? Theme.alpha(Theme.text, 0.05) : "transparent"
            Rectangle {
                visible: item.active
                width: 3
                height: parent.height
                color: Theme.accent
            }
        }
        contentItem: RowLayout {
            spacing: 12
            Label {
                text: item.glyph
                color: item.active ? Theme.accent : Theme.muted
                font.pixelSize: 15
                horizontalAlignment: Text.AlignHCenter
                Layout.preferredWidth: 20
                Layout.leftMargin: 8
            }
            Label {
                text: item.text
                color: item.active ? Theme.accent : Theme.text
                font.bold: item.active
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            Rectangle {
                visible: item.dotColor !== Qt.color("transparent")
                width: 8
                height: 8
                radius: 4
                color: item.dotColor
                Layout.rightMargin: 8
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 2

        // Logo; also moves the window, which has no title bar
        RowLayout {
            DragHandler {
                target: null
                onActiveChanged: if (active) root.Window.window.startSystemMove()
            }
            Layout.fillWidth: true
            Layout.leftMargin: 24
            Layout.topMargin: 24
            Layout.bottomMargin: 12
            spacing: 12
            Logo {
                Layout.preferredWidth: 34
                Layout.preferredHeight: 34
            }
            Label {
                text: metadata.name
                color: Theme.text
                font.pixelSize: 19
                font.bold: true
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }

        // Everything between the title and the settings scrolls when it does not fit
        ScrollView {
            id: scroller
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth  // Only scroll vertically
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            ColumnLayout {
                width: scroller.availableWidth
                spacing: 2

                RowLayout {
                    Layout.fillWidth: true
                    Layout.rightMargin: 12
                    SectionLabel { text: qsTr("CONNECTIONS"); Layout.fillWidth: true }
                    ToolButton {
                        text: "+"
                        font.pixelSize: 18
                        Layout.topMargin: 10
                        ToolTip.visible: hovered
                        ToolTip.text: Actions.newConnection.text.replace("&", "")
                        onClicked: Actions.newConnection.trigger()
                    }
                }

                Repeater {
                    model: root.connections
                    NavItem {
                        required property var modelData
                        required property int index
                        text: modelData.name
                        glyph: modelData.is_lin ? "⇄" : modelData.is_replay ? "⟲" : "⌁"
                        active: index === root.currentConnection && root.currentTool === ""
                        dotColor: modelData.state === "open" ? Theme.success
                            : modelData.state === "error" ? Theme.danger : Theme.alpha(Theme.muted, 0.6)
                        onClicked: root.connectionSelected(index)
                        ToolTip.visible: hovered
                        ToolTip.text: modelData.description
                    }
                }

                NavItem {
                    visible: root.connections.length === 0
                    text: qsTr("Replay a log file…")
                    glyph: "⟲"
                    onClicked: Actions.openLogFile.trigger()
                }

                SectionLabel {
                    visible: root.pages.length > 0
                    text: qsTr("VIEWS")
                }

                Repeater {
                    model: root.pages
                    NavItem {
                        required property var modelData
                        text: modelData.title
                        glyph: modelData.icon
                        active: modelData.key === root.currentPage && root.currentTool === ""
                        onClicked: root.pageSelected(modelData.key)
                    }
                }

                SectionLabel { text: qsTr("TOOLS") }

                NavItem {
                    text: qsTr("Database editor")
                    glyph: "✎"
                    active: root.currentTool === "database-editor"
                    dotColor: connectionManager.database_editor.dirty ? Theme.warning : "transparent"
                    onClicked: root.toolSelected("database-editor")
                }

                NavItem {
                    text: qsTr("Multi-bus trace")
                    glyph: "≣"
                    active: root.currentTool === "multi-bus"
                    onClicked: root.toolSelected("multi-bus")
                }

                NavItem {
                    text: qsTr("Compare recordings")
                    glyph: "⇄"
                    active: root.currentTool === "compare"
                    onClicked: root.toolSelected("compare")
                }

                NavItem {
                    text: qsTr("EtherCAT (CoE)")
                    glyph: "⇶"
                    active: root.currentTool === "ethercat"
                    dotColor: connectionManager.ethercat.is_open ? Theme.success : "transparent"
                    onClicked: root.toolSelected("ethercat")
                }

                NavItem {
                    text: qsTr("Physical I/O")
                    glyph: "⇅"
                    active: root.currentTool === "physical-io"
                    dotColor: connectionManager.physical_io.is_open ? Theme.success : "transparent"
                    onClicked: root.toolSelected("physical-io")
                }

                NavItem {
                    text: qsTr("DoIP")
                    glyph: "⇌"
                    active: root.currentTool === "doip"
                    dotColor: connectionManager.doip.is_open ? Theme.success : "transparent"
                    onClicked: root.toolSelected("doip")
                }

                NavItem {
                    text: qsTr("Bus gateway")
                    glyph: "⇄"
                    active: root.currentTool === "gateway"
                    dotColor: connectionManager.gateway.running ? Theme.success : "transparent"
                    onClicked: root.toolSelected("gateway")
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: 16
            Layout.rightMargin: 16
            height: 1
            color: Theme.alpha(Theme.border, 0.8)
        }

        NavItem {
            text: qsTr("Settings")
            glyph: "⚙"
            onClicked: Actions.preferences.trigger()
        }

        ItemDelegate {
            Layout.fillWidth: true
            Layout.bottomMargin: 10
            onClicked: Actions.about.trigger()
            background: Item {}
            contentItem: RowLayout {
                spacing: 12
                Rectangle {
                    width: 38
                    height: 38
                    radius: 19
                    color: Theme.accent
                    Layout.leftMargin: 8
                    Label {
                        anchors.centerIn: parent
                        text: "CE"
                        color: "white"
                        font.bold: true
                    }
                }
                ColumnLayout {
                    spacing: 0
                    Label { text: metadata.name; color: Theme.text; font.bold: true }
                    Label { text: qsTr("Version %1").arg(metadata.version); color: Theme.muted; font.pixelSize: 12 }
                }
                Item { Layout.fillWidth: true }
            }
        }
    }
}
