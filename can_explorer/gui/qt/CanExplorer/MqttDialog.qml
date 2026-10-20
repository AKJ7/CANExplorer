import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Publish the connection to an MQTT broker: broker, topics, what to publish, and optionally sending.
Dialog {
    id: dialog
    required property var connection
    readonly property var mqtt: connection ? connection.mqtt : null
    property string error: ""

    title: qsTr("MQTT")
    modal: true
    anchors.centerIn: Overlay.overlay
    standardButtons: Dialog.Close

    function load() {
        const options = dialog.mqtt.options
        brokerField.text = options.broker
        userField.text = options.username
        passwordField.text = ""
        prefixField.text = options.prefix
        signalsBox.checked = options.signals
        framesBox.checked = options.frames
        retainBox.checked = options.retain
        intervalField.text = String(options.interval)
        sendBox.checked = options.allow_send
        allowField.text = options.allowlist
    }

    function start() {
        error = dialog.mqtt.start({
            broker: brokerField.text,
            username: userField.text,
            password: passwordField.text,
            prefix: prefixField.text,
            signals: signalsBox.checked,
            frames: framesBox.checked,
            retain: retainBox.checked,
            interval: Number(intervalField.text || 0.1),
            allow_send: sendBox.checked,
            allowlist: allowField.text,
        })
    }

    // The options in use while publishing, otherwise the last used ones
    onAboutToShow: {
        error = ""
        load()
    }

    ColumnLayout {
        spacing: 12
        implicitWidth: 560
        enabled: dialog.mqtt !== null

        GridLayout {
            columns: 2
            columnSpacing: 12
            rowSpacing: 8
            Layout.fillWidth: true
            enabled: !dialog.mqtt.enabled

            Label { text: qsTr("Broker") }
            TextField {
                id: brokerField
                placeholderText: "mqtt://localhost:1883"
                Layout.fillWidth: true
                ToolTip.visible: hovered
                ToolTip.text: qsTr("mqtt://HOST[:PORT], or mqtts:// for TLS (port 8883)")
            }
            Label { text: qsTr("User") }
            RowLayout {
                Layout.fillWidth: true
                TextField {
                    id: userField
                    placeholderText: qsTr("optional")
                    Layout.fillWidth: true
                }
                TextField {
                    id: passwordField
                    placeholderText: qsTr("password")
                    echoMode: TextInput.Password
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Not stored: enter it again after restarting CANExplorer")
                }
            }
            Label { text: qsTr("Topic prefix") }
            TextField {
                id: prefixField
                placeholderText: "canexplorer"
                Layout.fillWidth: true
            }
        }

        Label {
            text: qsTr("Topics: %1/%2/…").arg(prefixField.text || "canexplorer").arg(dialog.connection ? dialog.connection.name : "")
            color: Theme.muted
            font.family: "monospace"
        }

        GroupBox {
            title: qsTr("Publish")
            Layout.fillWidth: true
            enabled: !dialog.mqtt.enabled
            ColumnLayout {
                anchors.fill: parent
                CheckBox {
                    id: signalsBox
                    text: qsTr("Signals (…/signals/MESSAGE/SIGNAL), when they change; needs a signal database")
                }
                CheckBox {
                    id: framesBox
                    text: qsTr("Frames (…/frames, a JSON list per interval)")
                }
                CheckBox {
                    id: retainBox
                    text: qsTr("Retain signal values on the broker")
                }
                RowLayout {
                    Label { text: qsTr("At most every") }
                    TextField {
                        id: intervalField
                        validator: DoubleValidator { bottom: 0.01; decimals: 3; notation: DoubleValidator.StandardNotation }
                        horizontalAlignment: Text.AlignRight
                        Layout.preferredWidth: 70
                    }
                    Label { text: qsTr("s per signal") }
                }
            }
        }

        GroupBox {
            title: qsTr("Send")
            Layout.fillWidth: true
            enabled: !dialog.mqtt.enabled && dialog.connection && dialog.connection.can_transmit
            ColumnLayout {
                anchors.fill: parent
                CheckBox {
                    id: sendBox
                    text: qsTr("Send frames published to …/send")
                }
                TextField {
                    id: allowField
                    visible: sendBox.checked
                    placeholderText: qsTr("Allowed messages or hex identifiers, e.g. EngineControl 7DF (* for all)")
                    Layout.fillWidth: true
                }
                Label {
                    visible: sendBox.checked
                    text: qsTr("Anyone who can publish to the broker can then send these frames on the bus.")
                    color: Theme.warning
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Button {
                text: dialog.mqtt.enabled ? qsTr("Stop") : qsTr("Start")
                highlighted: !dialog.mqtt.enabled
                onClicked: dialog.mqtt.enabled ? dialog.mqtt.stop() : dialog.start()
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Label {
                    text: dialog.error || (dialog.mqtt.enabled ? dialog.mqtt.state : qsTr("Not publishing"))
                    color: dialog.error ? Theme.danger : dialog.mqtt.state === "connected" ? Theme.success : Theme.text
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Label {
                    visible: text.length > 0
                    text: dialog.mqtt.statistics
                    color: Theme.muted
                }
            }
        }
    }
}
