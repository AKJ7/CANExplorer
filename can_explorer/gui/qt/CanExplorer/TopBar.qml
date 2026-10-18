import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import CanExplorer 1.0

// Breadcrumb, frame filter, connect button, notifications and the application menu.
Rectangle {
    id: root
    property var connection: null
    property string pageTitle: ""
    property var notifications: []

    signal clearNotifications()

    color: Theme.background
    implicitHeight: 72

    onConnectionChanged: filterField.text = connection ? connection.filter_text : ""

    // The window has no title bar: the top bar moves it, and a double click maximizes or restores it
    DragHandler {
        target: null
        grabPermissions: PointerHandler.CanTakeOverFromAnything
        onActiveChanged: if (active) root.Window.window.startSystemMove()
    }
    TapHandler {
        onDoubleTapped: root.toggleMaximized()
    }

    function toggleMaximized() {
        const window = root.Window.window
        if (window.visibility === Window.Maximized)
            window.showNormal()
        else
            window.showMaximized()
    }

    component WindowButton: ToolButton {
        id: windowButton
        property color hoverColor: Theme.alpha(Theme.text, 0.12)
        implicitWidth: 40
        implicitHeight: 32
        font.pixelSize: 15
        contentItem: Label {
            text: windowButton.text
            font: windowButton.font
            color: windowButton.hovered && windowButton.hoverColor === Theme.danger ? "white" : Theme.text
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: Theme.smallRadius
            color: windowButton.hovered ? windowButton.hoverColor : "transparent"
        }
    }

    Rectangle {
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: Theme.alpha(Theme.border, 0.6)
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 28
        anchors.rightMargin: 20
        spacing: 14

        Label {
            text: root.connection ? root.connection.name : qsTr("Welcome")
            color: Theme.text
            font.pixelSize: 16
        }
        Label {
            visible: root.connection !== null
            text: "/"
            color: Theme.muted
            font.pixelSize: 16
        }
        Label {
            visible: root.connection !== null
            text: root.pageTitle
            color: Theme.muted
            font.pixelSize: 16
        }

        Item { Layout.fillWidth: true }

        TextField {
            id: filterField
            visible: root.connection !== null
            placeholderText: qsTr("Filter: 100-1FF !123 ext rx fd")
            Layout.preferredWidth: 300
            Layout.preferredHeight: 40
            leftPadding: 18
            color: root.connection && root.connection.filter_error ? Theme.danger : Theme.text
            placeholderTextColor: Theme.muted
            onTextEdited: root.connection.set_filter(text)
            ToolTip.visible: hovered && root.connection !== null && root.connection.filter_error.length > 0
            ToolTip.text: root.connection ? root.connection.filter_error : ""
            background: Rectangle {
                radius: height / 2
                color: Theme.card
                border.color: filterField.activeFocus ? Theme.accent : Theme.alpha(Theme.border, 0.8)
            }
        }

        Button {
            id: connectButton
            visible: root.connection !== null
            text: !root.connection ? "" : root.connection.connected
                ? (root.connection.is_replay ? qsTr("Stop") : qsTr("Disconnect"))
                : (root.connection.is_replay ? qsTr("Replay") : qsTr("Connect"))
            Layout.preferredHeight: 40
            onClicked: root.connection.connected ? root.connection.close() : root.connection.open()
            highlighted: root.connection !== null && !root.connection.connected
            font.bold: true
            Layout.preferredWidth: Math.max(120, implicitWidth)
        }

        // Notifications
        ToolButton {
            id: bell
            text: "🔔"
            font.pixelSize: 18
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Notifications")
            onClicked: notificationPopup.open()

            Rectangle {
                visible: root.notifications.length > 0
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 2
                width: Math.max(18, badge.implicitWidth + 8)
                height: 18
                radius: 9
                color: Theme.danger
                Label {
                    id: badge
                    anchors.centerIn: parent
                    text: root.notifications.length > 99 ? "99+" : root.notifications.length
                    color: "white"
                    font.pixelSize: 11
                    font.bold: true
                }
            }

            Popup {
                id: notificationPopup
                y: bell.height + 6
                x: bell.width - width
                width: 420
                padding: 12
                background: Rectangle {
                    color: Theme.card
                    radius: Theme.radius
                    border.color: Theme.border
                }
                contentItem: ColumnLayout {
                    spacing: 8
                    RowLayout {
                        Label {
                            text: qsTr("Notifications")
                            color: Theme.text
                            font.bold: true
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Clear")
                            flat: true
                            enabled: root.notifications.length > 0
                            onClicked: root.clearNotifications()
                        }
                    }
                    Label {
                        visible: root.notifications.length === 0
                        text: qsTr("No notifications")
                        color: Theme.muted
                    }
                    ListView {
                        clip: true
                        spacing: 6
                        model: root.notifications.slice().reverse()
                        Layout.fillWidth: true
                        Layout.preferredHeight: Math.min(contentHeight, 360)
                        delegate: Rectangle {
                            required property var modelData
                            width: ListView.view.width
                            height: message.implicitHeight + 16
                            radius: Theme.smallRadius
                            color: Theme.cardAlt
                            Label {
                                id: message
                                anchors.fill: parent
                                anchors.margins: 8
                                text: `${modelData.time}  ${modelData.text}`
                                color: Theme.text
                                wrapMode: Text.Wrap
                            }
                        }
                    }
                }
            }
        }

        // Application menu
        ToolButton {
            id: menuButton
            text: "⋮"
            font.pixelSize: 20
            onClicked: appMenu.popup(menuButton, 0, menuButton.height)
            AppMenu { id: appMenu }
        }

        Rectangle {
            width: 1
            height: 28
            color: Theme.alpha(Theme.border, 0.8)
        }

        // Window controls
        RowLayout {
            spacing: 2
            WindowButton {
                text: "—"
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Minimize")
                onClicked: root.Window.window.showMinimized()
            }
            WindowButton {
                text: root.Window.window && root.Window.window.visibility === Window.Maximized ? "❐" : "☐"
                ToolTip.visible: hovered
                ToolTip.text: text === "❐" ? qsTr("Restore") : qsTr("Maximize")
                onClicked: root.toggleMaximized()
            }
            WindowButton {
                text: "✕"
                hoverColor: Theme.danger
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Close")
                onClicked: Actions.exit.trigger()
            }
        }
    }
}
