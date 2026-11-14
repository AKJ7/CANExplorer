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
    property var pageActivity: ({})  // { page key: { status, text } } of the current connection's views
    property string currentPage: ""
    property string currentTool: ""
    property bool collapsed: false  // Icon-only rail: labels hidden, glyphs and the logo shown

    // What each tool is doing, like the views' pageActivity: { tool: { status, text } }, status "ok" (running),
    // "idle" (set up but not running), "warning" or "error"; something to look at outranks running
    readonly property var toolActivity: {
        const activity = {}
        const mark = (key, status, text) => { activity[key] = { status: status, text: text } }
        const editor = connectionManager.database_editor
        const database = editor.file_name  // "Untitled" for a new one
        if (editor.dirty)
            mark("database-editor", "warning", qsTr("Unsaved changes in %1").arg(database))
        else if (editor.open)
            mark("database-editor", "ok", qsTr("Editing %1").arg(database))
        const multiBus = connectionManager.multi_bus
        if (multiBus.selected.length > 0) {
            const names = multiBus.selected.join(", ")
            const open = root.connections.some(connection => multiBus.selected.includes(connection.name) && connection.connected)
            if (multiBus.paused)
                mark("multi-bus", "idle", qsTr("Paused: %1").arg(names))
            else if (open)
                mark("multi-bus", "ok", qsTr("Merging %1").arg(names))
            else
                mark("multi-bus", "idle", qsTr("Waiting for %1 to connect").arg(names))
        }
        if (connectionManager.compare.busy)
            mark("compare", "ok", qsTr("Comparing"))
        const ethercat = connectionManager.ethercat
        if (ethercat.is_open)
            mark("ethercat", "ok", qsTr("Open on %1").arg(ethercat.adapter_description || ethercat.adapter))
        const io = connectionManager.physical_io
        if (io.is_open)
            mark("physical-io", "ok", qsTr("Open: %1").arg(io.device_name))
        const doip = connectionManager.doip
        if (doip.is_open)
            mark("doip", "ok", qsTr("Connected: %1").arg(doip.connection_text))
        else if (doip.discovering)
            mark("doip", "ok", qsTr("Discovering entities"))
        const someip = connectionManager.someip
        if (someip.error)
            mark("someip", "error", someip.error)
        else if (someip.is_open)
            mark("someip", "ok", qsTr("Listening · %1 messages").arg(someip.message_count))
        const ethernet = connectionManager.ethernet
        const events = ethernet.security_event_counts || {}
        if (ethernet.error)
            mark("ethernet", "error", ethernet.error)
        else if ((events.high || 0) > 0)
            mark("ethernet", "error", qsTr("%1 high-severity security events").arg(events.high))
        else if ((events.events || 0) > 0)
            mark("ethernet", "warning", qsTr("%1 security events").arg(events.events))
        else if (ethernet.capturing)
            mark("ethernet", "ok", qsTr("Capturing · %1 packets").arg(ethernet.count))
        const gateway = connectionManager.gateway
        if (gateway.running) {
            const rules = gateway.rules.filter(rule => rule.enabled)
            const waiting = rules.filter(rule => !rule.active).length
            if (waiting > 0)
                mark("gateway", "warning", qsTr("%1 of %2 rules waiting for their connections").arg(waiting).arg(rules.length))
            else
                mark("gateway", "ok", qsTr("Forwarding, rules: %1").arg(rules.length))
        }
        return activity
    }

    signal toolSelected(string tool)

    signal connectionSelected(int index)
    signal pageSelected(string page)
    signal homeSelected()  // The logo/title clicked: show the welcome page

    color: Theme.surface
    implicitWidth: Theme.sidebarWidth

    Rectangle {
        anchors.right: parent.right
        width: 1
        height: parent.height
        color: Theme.alpha(Theme.border, 0.6)
    }

    component SectionLabel: Label {
        visible: !root.collapsed
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
        // What the view or tool is doing ({ status, text }, or null): its mark, explained in the tooltip
        property var activity: null
        // State shown as a StatusDot at the end of the row ("", "ok", "idle", "warning", "error")
        property string status: activity ? activity.status : ""
        Layout.fillWidth: true
        implicitHeight: 42
        hoverEnabled: true
        // When collapsed, the label is hidden, so name the item in a tooltip (instances may override this)
        ToolTip.text: activity ? `${item.text}: ${activity.text}` : item.text
        ToolTip.visible: item.hovered && (root.collapsed || activity !== null)

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
                Layout.preferredWidth: root.collapsed ? -1 : 20
                Layout.fillWidth: root.collapsed
                Layout.leftMargin: root.collapsed ? 0 : 8
            }
            Label {
                visible: !root.collapsed
                text: item.text
                color: item.active ? Theme.accent : Theme.text
                font.bold: item.active
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            StatusDot {
                visible: !root.collapsed && item.status !== ""
                status: item.status
                size: 9
                Layout.rightMargin: 8
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 2

        // Logo and title; clicking shows the welcome page, dragging moves the window (which has no title bar)
        RowLayout {
            DragHandler {
                target: null
                onActiveChanged: if (active) root.Window.window.startSystemMove()
            }
            TapHandler {
                onTapped: root.homeSelected()
            }
            HoverHandler {
                cursorShape: Qt.PointingHandCursor
            }
            Layout.fillWidth: true
            Layout.leftMargin: root.collapsed ? Math.max(0, Math.round((root.width - 34) / 2)) : 24
            Layout.topMargin: 24
            Layout.bottomMargin: 12
            spacing: 12
            Logo {
                Layout.preferredWidth: 34
                Layout.preferredHeight: 34
            }
            Label {
                visible: !root.collapsed
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
                    Layout.rightMargin: root.collapsed ? 0 : 12
                    SectionLabel { text: qsTr("CONNECTIONS"); Layout.fillWidth: true }
                    Item { Layout.fillWidth: root.collapsed }  // Centre the button on the collapsed rail
                    ToolButton {
                        text: "+"
                        font.pixelSize: 18
                        Layout.topMargin: 10
                        ToolTip.visible: hovered
                        ToolTip.text: Actions.newConnection.text.replace("&", "")
                        onClicked: Actions.newConnection.trigger()
                    }
                    Item { Layout.fillWidth: root.collapsed }
                }

                Repeater {
                    model: root.connections
                    NavItem {
                        required property var modelData
                        required property int index
                        text: modelData.name
                        glyph: modelData.is_lin ? "⇄" : modelData.is_replay ? "⟲" : "⌁"
                        active: index === root.currentConnection && root.currentTool === ""
                        status: modelData.state === "open" ? "ok" : modelData.state === "error" ? "error" : "idle"
                        onClicked: root.connectionSelected(index)
                        ToolTip.visible: hovered
                        ToolTip.text: modelData.description + "\n" + (modelData.state === "open" ? qsTr("Connected")
                            : modelData.state === "error" ? qsTr("Error")
                            : modelData.state === "finished" ? qsTr("Replay finished") : qsTr("Closed"))
                    }
                }

                NavItem {
                    visible: root.connections.length === 0
                    text: qsTr("Replay a log file…")
                    glyph: "⟲"
                    onClicked: Actions.openLogFile.trigger()
                }

                SectionLabel {
                    visible: !root.collapsed && root.pages.length > 0
                    text: qsTr("VIEWS")
                }

                Repeater {
                    model: root.pages
                    NavItem {
                        required property var modelData
                        text: modelData.title
                        glyph: modelData.icon
                        active: modelData.key === root.currentPage && root.currentTool === ""
                        activity: root.pageActivity[modelData.key] || null
                        onClicked: root.pageSelected(modelData.key)
                    }
                }

                SectionLabel { text: qsTr("TOOLS") }

                NavItem {
                    text: qsTr("Database editor")
                    glyph: "✎"
                    active: root.currentTool === "database-editor"
                    activity: root.toolActivity["database-editor"] || null
                    onClicked: root.toolSelected("database-editor")
                }

                NavItem {
                    text: qsTr("Multi-bus trace")
                    glyph: "≣"
                    active: root.currentTool === "multi-bus"
                    activity: root.toolActivity["multi-bus"] || null
                    onClicked: root.toolSelected("multi-bus")
                }

                NavItem {
                    text: qsTr("Compare recordings")
                    glyph: "⇄"
                    active: root.currentTool === "compare"
                    activity: root.toolActivity["compare"] || null
                    onClicked: root.toolSelected("compare")
                }

                NavItem {
                    text: qsTr("EtherCAT (CoE)")
                    glyph: "⇶"
                    active: root.currentTool === "ethercat"
                    activity: root.toolActivity["ethercat"] || null
                    onClicked: root.toolSelected("ethercat")
                }

                NavItem {
                    text: qsTr("Physical I/O")
                    glyph: "⇅"
                    active: root.currentTool === "physical-io"
                    activity: root.toolActivity["physical-io"] || null
                    onClicked: root.toolSelected("physical-io")
                }

                NavItem {
                    text: qsTr("DoIP")
                    glyph: "⇌"
                    active: root.currentTool === "doip"
                    activity: root.toolActivity["doip"] || null
                    onClicked: root.toolSelected("doip")
                }

                NavItem {
                    text: qsTr("SOME/IP")
                    glyph: "⿻"
                    active: root.currentTool === "someip"
                    activity: root.toolActivity["someip"] || null
                    onClicked: root.toolSelected("someip")
                }

                NavItem {
                    text: qsTr("Ethernet capture")
                    glyph: "🖧"
                    active: root.currentTool === "ethernet"
                    activity: root.toolActivity["ethernet"] || null
                    onClicked: root.toolSelected("ethernet")
                }

                NavItem {
                    text: qsTr("Bus gateway")
                    glyph: "⇄"
                    active: root.currentTool === "gateway"
                    activity: root.toolActivity["gateway"] || null
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
                spacing: root.collapsed ? 0 : 12  // No gaps on the narrow rail, so the avatar stays centred
                Item { Layout.fillWidth: root.collapsed }  // Centre the avatar on the collapsed rail (equal padding)
                Rectangle {
                    width: 38
                    height: 38
                    radius: 19
                    // A ring, so the initials sit on the sidebar and read in every theme, coloured like the name
                    color: "transparent"
                    border.color: Theme.accent
                    border.width: 2
                    Layout.leftMargin: root.collapsed ? 0 : 8
                    Label {
                        anchors.centerIn: parent
                        text: "CE"
                        color: Theme.text  // As the application name beside it
                        font.bold: true
                    }
                }
                ColumnLayout {
                    visible: !root.collapsed
                    spacing: 0
                    Label { text: metadata.name; color: Theme.text; font.bold: true }
                    Label { text: qsTr("Version %1").arg(metadata.version); color: Theme.muted; font.pixelSize: 12 }
                }
                Item { Layout.fillWidth: true }
            }
        }
    }
}
