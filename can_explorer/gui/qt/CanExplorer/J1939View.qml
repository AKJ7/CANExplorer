import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// J1939 network: the nodes and their address claims, their trouble codes, and sending parameter groups.
ColumnLayout {
    id: root
    required property var connection
    readonly property var j1939: connection.j1939
    readonly property bool canSend: connection.can_transmit && connection.connected
    property int selected: -1
    // Refreshed with the node list, as the details of the selected node change with it
    readonly property var details: { j1939.nodes; return selected >= 0 ? j1939.node_details(selected) : ({}) }

    function report(error) {
        errorLabel.text = error
    }

    function lampColor(state) {
        return state === "on" ? Theme.danger : state === "error" ? Theme.warning : Theme.muted
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label {
            text: qsTr("J1939 network: %1 nodes").arg(root.j1939.nodes.length)
            font.bold: true
        }
        Button {
            text: qsTr("Request address claims")
            enabled: root.canSend
            onClicked: root.report(root.j1939.request_address_claims())
        }
        Button {
            text: qsTr("Clear")
            flat: true
            onClicked: {
                root.selected = -1
                root.j1939.clear()
            }
        }
        Label { text: qsTr("Tool address"); visible: root.connection.can_transmit }
        TextField {
            text: root.j1939.tool_address
            visible: root.connection.can_transmit
            Layout.preferredWidth: 50
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Source address of requests and sent parameter groups (hex, F9 = service tool)")
            onEditingFinished: root.report(root.j1939.set_tool_address(text))
        }
        Item { Layout.fillWidth: true }
        Label {
            text: qsTr("%1 cannot claim an address").arg(root.j1939.cannot_claim.length)
            color: Theme.warning
            visible: root.j1939.cannot_claim.length > 0
            ToolTip.visible: cannotClaimArea.containsMouse
            ToolTip.text: root.j1939.cannot_claim.join("\n")
            MouseArea {
                id: cannotClaimArea
                anchors.fill: parent
                hoverEnabled: true
            }
        }
        Label {
            id: errorLabel
            color: Theme.danger
            visible: text.length > 0
            elide: Text.ElideRight
            Layout.maximumWidth: 360
        }
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Nodes
        ColumnLayout {
            SplitView.preferredWidth: 380
            SplitView.minimumWidth: 260
            spacing: 2

            Label {
                text: root.j1939.nodes.length ? qsTr("Nodes") : qsTr("No J1939 traffic yet")
                color: Theme.muted
                padding: 4
            }
            ListView {
                id: nodeList
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.j1939.nodes
                delegate: ItemDelegate {
                    id: nodeItem
                    required property var modelData
                    width: nodeList.width - 10
                    highlighted: root.selected === modelData.address
                    onClicked: root.selected = modelData.address

                    contentItem: RowLayout {
                        spacing: 8
                        Label {
                            text: nodeItem.modelData.address_text
                            font.family: "monospace"
                            font.bold: true
                        }
                        ColumnLayout {
                            spacing: 0
                            Layout.fillWidth: true
                            Label {
                                text: nodeItem.modelData.claimed ? nodeItem.modelData.function : qsTr("No address claim")
                                color: nodeItem.modelData.claimed ? Theme.text : Theme.muted
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            Label {
                                text: (nodeItem.modelData.claimed
                                       ? qsTr("manufacturer %1 · identity %2 · ").arg(nodeItem.modelData.manufacturer).arg(nodeItem.modelData.identity)
                                       : "") + (nodeItem.modelData.frames === 1 ? qsTr("1 frame") : qsTr("%1 frames").arg(nodeItem.modelData.frames))
                                color: Theme.muted
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                        Repeater {
                            model: nodeItem.modelData.lamps
                            Pill {
                                required property string modelData
                                text: modelData
                                tint: Theme.danger
                            }
                        }
                        Pill {
                            visible: nodeItem.modelData.active_dtcs > 0
                            text: qsTr("%1 DTC").arg(nodeItem.modelData.active_dtcs)
                            tint: Theme.warning
                        }
                    }
                }
            }
        }

        // Selected node
        ScrollView {
            id: detailsScroll
            SplitView.fillWidth: true
            SplitView.minimumWidth: 300
            clip: true
            contentWidth: availableWidth

            ColumnLayout {
                width: detailsScroll.availableWidth
                spacing: 6

                Label {
                    visible: root.selected < 0
                    text: qsTr("Select a node to see its NAME, trouble codes and parameter groups.")
                    color: Theme.muted
                    padding: 8
                }

                ColumnLayout {
                    visible: root.selected >= 0 && root.details.address !== undefined
                    Layout.fillWidth: true
                    Layout.margins: 4
                    spacing: 6

                    Label {
                        text: `${root.details.address_text || ""}  ${root.details.function || ""}`
                        font.bold: true
                        font.pixelSize: 15
                    }
                    Label {
                        text: root.details.description || ""
                        color: Theme.muted
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    GridLayout {
                        visible: !!root.details.claimed
                        columns: 4
                        columnSpacing: 12
                        rowSpacing: 2
                        Label { text: qsTr("NAME"); color: Theme.muted }
                        Label { text: root.details.name || ""; font.family: "monospace" }
                        Label { text: qsTr("Industry group"); color: Theme.muted }
                        Label { text: root.details.industry_group || "" }
                        Label { text: qsTr("ECU instance"); color: Theme.muted }
                        Label { text: String(root.details.ecu_instance) }
                        Label { text: qsTr("Function instance"); color: Theme.muted }
                        Label { text: String(root.details.function_instance) }
                        Label { text: qsTr("Vehicle system"); color: Theme.muted }
                        Label { text: String(root.details.vehicle_system) }
                        Label { text: qsTr("Arbitrary address"); color: Theme.muted }
                        Label { text: root.details.arbitrary_address ? qsTr("capable") : qsTr("no") }
                    }

                    // Trouble codes: DM1 (active) and DM2 (previously active)
                    Repeater {
                        model: [
                            { key: "dm1", title: qsTr("Active trouble codes (DM1)"), previous: false },
                            { key: "dm2", title: qsTr("Previously active trouble codes (DM2)"), previous: true },
                        ]
                        ColumnLayout {
                            id: dmSection
                            required property var modelData
                            readonly property var message: root.details[modelData.key] || ({ received: false, lamps: [], dtcs: [] })
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: dmSection.modelData.title; font.bold: true }
                                Repeater {
                                    model: dmSection.message.lamps
                                    Pill {
                                        required property var modelData
                                        text: `${modelData.lamp} ${modelData.state}`
                                        tint: root.lampColor(modelData.state)
                                    }
                                }
                                Item { Layout.fillWidth: true }
                                Button {
                                    text: qsTr("Request")
                                    visible: dmSection.modelData.previous
                                    enabled: root.canSend
                                    onClicked: root.report(root.j1939.request_dm2(root.selected))
                                }
                                Button {
                                    text: dmSection.modelData.previous ? qsTr("Clear (DM3)") : qsTr("Clear (DM11)")
                                    enabled: root.canSend
                                    onClicked: root.report(root.j1939.clear_dtcs(root.selected, dmSection.modelData.previous))
                                }
                            }
                            Label {
                                visible: dmSection.message.dtcs.length === 0
                                text: dmSection.message.received ? qsTr("No trouble codes")
                                      : dmSection.modelData.previous ? qsTr("Not requested yet") : qsTr("No DM1 received")
                                color: Theme.muted
                            }
                            Repeater {
                                model: dmSection.message.dtcs
                                Label {
                                    required property var modelData
                                    text: `SPN ${modelData.spn}${modelData.name ? " " + modelData.name : ""} · FMI ${modelData.fmi} ${modelData.fmi_text} · ${modelData.occurrences}×`
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }

                    Label { text: qsTr("Parameter groups sent"); font.bold: true }
                    Repeater {
                        model: root.details.pgns || []
                        RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Label { text: modelData.label; font.family: "monospace"; Layout.fillWidth: true; elide: Text.ElideRight }
                            Label { text: String(modelData.count); color: Theme.muted }
                        }
                    }
                }
            }
        }
    }

    // Sending parameter groups
    GridLayout {
        visible: root.connection.can_transmit
        columns: 9
        columnSpacing: 6
        rowSpacing: 2
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("PGN") }
        ComboBox {
            id: pgnBox
            editable: true
            model: root.j1939.pgn_names
            Layout.preferredWidth: 260
            ToolTip.visible: hovered
            ToolTip.text: qsTr("A name, a number such as 0xFEF1 or 65265, or a message of the database")
        }
        Label { text: qsTr("Priority") }
        SpinBox {
            id: priorityBox
            from: 0
            to: 7
            value: 6
        }
        Label { text: qsTr("To") }
        TextField {
            id: destinationField
            text: root.selected >= 0 ? root.selected.toString(16).toUpperCase().padStart(2, "0") : "FF"
            Layout.preferredWidth: 50
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Destination address (hex); FF sends to all nodes. Parameter groups 0xF000 and above are always broadcast")
        }
        TextField {
            id: dataField
            placeholderText: qsTr("data, hex, or signal values: EngineSpeed=1200, …")
            Layout.fillWidth: true
            onAccepted: sendButton.clicked()
        }
        Button {
            id: sendButton
            text: root.j1939.busy ? qsTr("Sending…") : qsTr("Send")
            enabled: root.canSend && !root.j1939.busy
            ToolTip.visible: hovered
            ToolTip.text: qsTr("More than 8 bytes are sent with the transport protocol: BAM to all nodes, RTS/CTS to one node")
            onClicked: root.report(root.j1939.send_pgn(pgnBox.editText, priorityBox.value, destinationField.text, dataField.text))
        }
        Button {
            text: qsTr("Request")
            enabled: root.canSend
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Ask the destination for this parameter group")
            onClicked: root.report(root.j1939.request_pgn(pgnBox.editText, destinationField.text))
        }
    }

    ListView {
        id: logList
        visible: root.connection.can_transmit && count > 0
        clip: true
        Layout.fillWidth: true
        Layout.preferredHeight: 70
        Layout.leftMargin: 4
        ScrollBar.vertical: ScrollBar {}
        model: root.j1939.log
        onCountChanged: positionViewAtEnd()
        delegate: Label {
            required property var modelData
            width: logList.width
            text: modelData.text
            font.family: "monospace"
            font.pixelSize: 12
            color: modelData.error ? Theme.danger : Theme.muted
            elide: Text.ElideRight
        }
    }
}
