import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// SOME/IP over Ethernet: listen on UDP, discover services and show decoded messages.
ColumnLayout {
    id: root
    readonly property var someip: connectionManager.someip
    readonly property string pageTitle: someip.is_open ? qsTr("listening") : qsTr("not listening")
    spacing: Theme.spacing

    // Controls
    RowLayout {
        spacing: 8
        Layout.fillWidth: true

        Label { text: qsTr("UDP ports"); color: Theme.muted }
        TextField {
            id: portsField
            text: root.someip.ports || root.someip.default_ports
            enabled: !root.someip.is_open
            Layout.preferredWidth: 160
            ToolTip.visible: hovered
            ToolTip.text: qsTr("UDP ports to listen on, comma separated (30490 is the usual Service Discovery port)")
        }
        Label { text: qsTr("SD multicast"); color: Theme.muted }
        TextField {
            id: multicastField
            text: root.someip.multicast || root.someip.default_multicast
            enabled: !root.someip.is_open
            Layout.preferredWidth: 160
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Service Discovery multicast group to join (leave empty to listen on unicast only)")
        }
        Button {
            text: root.someip.is_open ? qsTr("Stop") : qsTr("Listen")
            highlighted: !root.someip.is_open
            onClicked: {
                if (root.someip.is_open) {
                    root.someip.close()
                } else {
                    const error = root.someip.open(portsField.text, multicastField.text)
                    if (error)
                        statusLabel.show(error)
                }
            }
        }
        Button {
            text: qsTr("Clear")
            enabled: root.someip.message_count > 0
            onClicked: root.someip.clear()
        }
        Button {
            text: root.someip.description_file ? qsTr("Unload FIBEX") : qsTr("FIBEX…")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Load a FIBEX file to decode the message payloads into named signals")
            onClicked: root.someip.description_file ? root.someip.unload_description() : fibexDialog.open()
        }
        Item { Layout.fillWidth: true }
        Pill {
            visible: root.someip.description_file.length > 0
            text: root.someip.description_file
            tint: Theme.chart[4]
        }
        Pill {
            visible: root.someip.is_open
            text: qsTr("%1 messages").arg(root.someip.message_count.toLocaleString(Qt.locale(), "f", 0))
            tint: Theme.accent
        }
    }

    Label {
        id: statusLabel
        visible: text.length > 0
        text: root.someip.error
        color: Theme.danger
        Layout.fillWidth: true
        Timer { id: statusTimer; interval: 5000; onTriggered: statusLabel.text = Qt.binding(() => root.someip.error) }
        function show(message) { text = message; statusTimer.restart() }
    }

    Label {
        visible: !root.someip.is_open && root.someip.message_count === 0 && root.someip.error === ""
        text: qsTr("SOME/IP is the service-oriented middleware of automotive Ethernet. Listen on the Service Discovery port to see the services offered on the network and the messages exchanged. A plain UDP socket sees the multicast it joins and traffic addressed to this host; full capture needs a network tap or mirror port.")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Discovered services
    Card {
        title: qsTr("Services (%1)").arg(root.someip.services.length)
        visible: root.someip.services.length > 0
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(220, 60 + root.someip.services.length * 30)

        ColumnLayout {
            anchors.fill: parent
            spacing: 2
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Repeater {
                    model: [qsTr("Service"), qsTr("Instance"), qsTr("Version"), qsTr("Endpoints"), qsTr("Offers")]
                    Label {
                        required property int index
                        required property string modelData
                        text: modelData
                        color: Theme.muted
                        font.bold: true
                        font.pixelSize: 12
                        Layout.preferredWidth: [80, 80, 80, 0, 60][index] || -1
                        Layout.fillWidth: index === 3
                    }
                }
            }
            ListView {
                clip: true
                model: root.someip.services
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                delegate: RowLayout {
                    required property var modelData
                    width: ListView.view.width
                    spacing: 12
                    Label { text: modelData.service; font.family: "monospace"; color: Theme.text; Layout.preferredWidth: 80 }
                    Label { text: modelData.instance; font.family: "monospace"; color: Theme.text; Layout.preferredWidth: 80 }
                    Label { text: modelData.version; font.family: "monospace"; color: Theme.muted; Layout.preferredWidth: 80 }
                    Label { text: modelData.endpoints; font.family: "monospace"; color: Theme.text; elide: Text.ElideRight; Layout.fillWidth: true }
                    Label { text: modelData.offers; color: Theme.muted; Layout.preferredWidth: 60 }
                }
            }
        }
    }

    // Message stream
    Card {
        title: qsTr("Messages")
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 2
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Repeater {
                    model: [qsTr("Time"), qsTr("Source"), qsTr("Service.Method"), qsTr("Type"), qsTr("Len"), qsTr("Description")]
                    Label {
                        required property int index
                        required property string modelData
                        text: modelData
                        color: Theme.muted
                        font.bold: true
                        font.pixelSize: 12
                        Layout.preferredWidth: [70, 140, 120, 150, 50, 0][index] || -1
                        Layout.fillWidth: index === 5
                    }
                }
            }
            ListView {
                id: messageList
                clip: true
                model: root.someip.messages
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                onCountChanged: if (atYEnd || count <= 1) positionViewAtEnd()
                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: messageList.width
                    height: 26
                    color: index % 2 ? Theme.alpha(Theme.cardAlt, 0.5) : "transparent"
                    RowLayout {
                        anchors.fill: parent
                        spacing: 12
                        Label { text: modelData.time; font.family: "monospace"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 70 }
                        Label { text: modelData.source; font.family: "monospace"; font.pixelSize: 12; color: Theme.text; Layout.preferredWidth: 140; elide: Text.ElideRight }
                        Label { text: modelData.id; font.family: "monospace"; font.pixelSize: 12; color: modelData.sd ? Theme.chart[5] : Theme.text; Layout.preferredWidth: 120 }
                        Label { text: modelData.type; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 150; elide: Text.ElideRight }
                        Label { text: modelData.length; font.family: "monospace"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 50 }
                        Label { text: modelData.description; font.pixelSize: 12; color: Theme.text; elide: Text.ElideRight; Layout.fillWidth: true }
                    }
                }
            }
        }
    }

    FileDialog {
        id: fibexDialog
        title: qsTr("Load SOME/IP FIBEX description")
        nameFilters: [qsTr("FIBEX files (*.fibex *.xml)"), qsTr("All files (*)")]
        onAccepted: {
            const error = root.someip.load_description(selectedFile)
            if (error)
                statusLabel.show(error)
        }
    }
}
