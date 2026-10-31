import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// In-app MCP server: expose this connection's live bus to an AI agent over localhost, token-gated.
Dialog {
    id: dialog
    required property var connection
    readonly property var server: connection ? connection.mcp_server : null

    title: qsTr("AI agent (MCP)")
    modal: true
    anchors.centerIn: Overlay.overlay
    standardButtons: Dialog.Close
    width: 560

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacing

        Label {
            text: qsTr("Let an AI agent inspect this connection — the bus you already have open — over a local MCP server. It listens on 127.0.0.1 only and requires the token below.")
            color: Theme.muted
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        Label {
            visible: !dialog.server.available
            text: qsTr("The MCP SDK is not installed. Install it with:  pip install 'can-explorer[mcp]'")
            color: Theme.warning
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        RowLayout {
            spacing: 8
            Layout.fillWidth: true
            enabled: dialog.server.available

            Label { text: qsTr("Access"); color: Theme.muted }
            ComboBox {
                id: tierBox
                model: dialog.server.tiers
                enabled: !dialog.server.running
                Layout.preferredWidth: 160
                Component.onCompleted: currentIndex = Math.max(0, dialog.server.tiers.indexOf(dialog.server.tier))
            }
            Item { Layout.fillWidth: true }
            Button {
                text: dialog.server.running ? qsTr("Stop") : qsTr("Start")
                highlighted: !dialog.server.running
                onClicked: {
                    if (dialog.server.running)
                        dialog.server.stop()
                    else {
                        const error = dialog.server.start(tierBox.currentText)
                        if (error)
                            statusLabel.text = error
                    }
                }
            }
        }

        Label {
            text: {
                switch (tierBox.currentText) {
                case "diagnostics": return qsTr("Read-only, plus OBD-II and UDS diagnostic reads (these send requests on the bus).")
                case "control": return qsTr("Plus active diagnostics: clear trouble codes, reset ECUs, run routines. Use with care.")
                case "send": return qsTr("Plus transmitting arbitrary frames and messages. The agent fully controls the bus.")
                default: return qsTr("Read-only: inspect frames, decode and reverse-engineer. Sends nothing.")
                }
            }
            color: tierBox.currentText === "read-only" ? Theme.muted : Theme.warning
            wrapMode: Text.Wrap
            font.pixelSize: 12
            Layout.fillWidth: true
        }

        Label {
            id: statusLabel
            text: dialog.server.error
            visible: text.length > 0
            color: Theme.danger
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        // Connection details, shown while running
        Card {
            visible: dialog.server.running
            Layout.fillWidth: true
            Layout.preferredHeight: detailsGrid.implicitHeight + 2 * Theme.spacing

            GridLayout {
                id: detailsGrid
                anchors.fill: parent
                columns: 3
                columnSpacing: 8
                rowSpacing: 6

                Label { text: qsTr("URL"); color: Theme.muted }
                TextField {
                    id: urlField
                    text: dialog.server.url
                    readOnly: true
                    font.family: "monospace"
                    Layout.fillWidth: true
                }
                Button { text: qsTr("Copy"); onClicked: { urlField.selectAll(); urlField.copy() } }

                Label { text: qsTr("Token"); color: Theme.muted }
                TextField {
                    id: tokenField
                    text: dialog.server.token
                    readOnly: true
                    font.family: "monospace"
                    echoMode: TextInput.Password
                    Layout.fillWidth: true
                }
                Button { text: qsTr("Copy"); onClicked: { tokenField.selectAll(); tokenField.copy() } }
            }
        }

        Label {
            visible: dialog.server.running
            text: qsTr("Point your MCP client at the URL using the streamable-HTTP transport, with the token as a Bearer header (Authorization: Bearer <token>) or appended as ?token=<token>.")
            color: Theme.muted
            wrapMode: Text.Wrap
            font.pixelSize: 12
            Layout.fillWidth: true
        }
    }
}
