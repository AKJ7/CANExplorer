import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Automotive Ethernet capture: open a pcap file (or capture live on Linux) and decode the packets — Ethernet,
// VLAN, IPv4/IPv6, ARP, UDP/TCP, and SOME/IP, DoIP, gPTP and AVTP on top.
ColumnLayout {
    id: root
    readonly property var eth: connectionManager.ethernet
    readonly property string pageTitle: eth.capturing ? qsTr("capturing") : (eth.file_name || qsTr("no capture"))
    property int selectedNumber: 0  // Packet selected for the details pane
    readonly property var detailRows: selectedNumber > 0 && eth.count > 0 ? eth.dissect(selectedNumber) : []
    spacing: Theme.spacing

    RowLayout {
        spacing: 8
        Layout.fillWidth: true

        Button {
            text: qsTr("Open pcap…")
            enabled: !eth.busy && !eth.capturing
            onClicked: openDialog.open()
        }
        ComboBox {
            id: ifaceBox
            model: eth.interfaces
            enabled: !eth.capturing && eth.interfaces.length > 0
            visible: eth.interfaces.length > 0
            Layout.preferredWidth: 140
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Interface to capture live from (needs libpcap/Npcap and capture privileges)")
        }
        Button {
            text: eth.capturing ? qsTr("Stop") : qsTr("Capture")
            highlighted: eth.capturing
            visible: eth.interfaces.length > 0
            enabled: eth.capturing || ifaceBox.currentText.length > 0
            onClicked: {
                if (eth.capturing)
                    eth.stop_capture()
                else {
                    const error = eth.start_capture(ifaceBox.currentText)
                    if (error)
                        statusLabel.show(error)
                }
            }
        }
        Button {
            text: qsTr("Export…")
            enabled: eth.count > 0 && !eth.capturing
            onClicked: saveDialog.open()
        }
        Button {
            text: qsTr("Export CAN log…")
            enabled: eth.can_frame_count > 0 && !eth.capturing
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Save the CAN frames tunnelled in AVTP ACF (TSCF/NTSCF) as a CAN log to replay in a connection")
            onClicked: saveCanDialog.open()
        }
        Button {
            text: qsTr("Clear")
            enabled: eth.count > 0 && !eth.capturing
            onClicked: eth.clear()
        }
        TextField {
            id: filterField
            placeholderText: qsTr("filter: SOME/IP, 192.168.0.10, 30509…")
            text: eth.filter
            onTextEdited: eth.set_filter(text)
            Layout.preferredWidth: 220
        }
        TextField {
            id: nmPortField
            placeholderText: qsTr("UDP-NM port")
            text: eth.nm_port > 0 ? eth.nm_port : ""
            inputMethodHints: Qt.ImhDigitsOnly
            validator: IntValidator { bottom: 0; top: 65535 }
            onEditingFinished: eth.set_nm_port(parseInt(text) || 0)
            Layout.preferredWidth: 110
            ToolTip.visible: hovered
            ToolTip.text: qsTr("UDP port carrying AUTOSAR UDP-NM (it has no standard port); set it to decode NM frames")
        }
        Item { Layout.fillWidth: true }
        BusyIndicator { running: eth.busy; visible: running; Layout.preferredHeight: 28; Layout.preferredWidth: 28 }
        Pill {
            visible: eth.file_name.length > 0
            text: eth.file_name
            tint: Theme.chart[4]
        }
        Pill {
            visible: eth.count > 0
            text: qsTr("%1 packets").arg(eth.count.toLocaleString(Qt.locale(), "f", 0))
            tint: Theme.accent
        }
        Pill {
            visible: eth.can_frame_count > 0
            text: qsTr("%1 ACF-CAN").arg(eth.can_frame_count.toLocaleString(Qt.locale(), "f", 0))
            tint: Theme.chart[2]
        }
    }

    Label {
        id: statusLabel
        visible: text.length > 0
        text: eth.error || eth.status
        color: eth.error.length > 0 ? Theme.danger : Theme.muted
        Layout.fillWidth: true
        Timer { id: statusTimer; interval: 5000; onTriggered: statusLabel.text = Qt.binding(() => eth.error || eth.status) }
        function show(message) { text = message; statusTimer.restart() }
    }

    Label {
        visible: eth.count === 0 && !eth.busy
        text: qsTr("Open a pcap capture of automotive Ethernet traffic (classic pcap, Ethernet link type), or capture live on an interface. Frames are decoded down the stack and the automotive protocols — SOME/IP, DoIP, gPTP, AVTP — are recognised. A full capture needs a network tap or a mirror/SPAN port.")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Protocol breakdown
    Flow {
        visible: eth.protocols.length > 0
        spacing: 6
        Layout.fillWidth: true
        Repeater {
            model: eth.protocols
            Pill {
                required property var modelData
                text: modelData.protocol + " · " + modelData.count
                tint: Theme.chart[["SOME/IP", "DoIP", "gPTP", "AVTP", "ARP"].indexOf(modelData.protocol) % Theme.chart.length]
            }
        }
    }

    // Time sync (gPTP / IEEE 802.1AS)
    Card {
        id: gptpCard
        title: qsTr("Time sync (gPTP)")
        visible: eth.has_gptp
        Layout.fillWidth: true
        Layout.preferredHeight: gptpGrid.implicitHeight + 2 * Theme.spacing
        readonly property var g: eth.gptp

        GridLayout {
            id: gptpGrid
            anchors.fill: parent
            columns: 4
            columnSpacing: 24
            rowSpacing: 6

            Label { text: qsTr("Grandmaster"); color: Theme.muted }
            Label {
                text: gptpCard.g.grandmaster
                    ? gptpCard.g.grandmaster + qsTr("  (priority %1, %2)").arg(gptpCard.g.gm_priority1)
                        .arg(gptpCard.g.gm_clock_class_name || qsTr("class %1").arg(gptpCard.g.gm_clock_class))
                    : qsTr("none announced")
                color: Theme.text
                font.family: "monospace"
                font.pixelSize: 12
                Layout.columnSpan: 3
                Layout.fillWidth: true
                elide: Text.ElideRight
            }

            Label { text: qsTr("Steps removed"); color: Theme.muted }
            Label { text: String(gptpCard.g.steps_removed); color: Theme.text; font.pixelSize: 12 }
            Label { text: qsTr("Time source"); color: Theme.muted }
            Label { text: gptpCard.g.gm_time_source || "—"; color: Theme.text; font.pixelSize: 12 }

            Label { text: qsTr("Sync interval"); color: Theme.muted }
            Label {
                text: gptpCard.g.sync_interval_ms != null
                    ? qsTr("%1 ms").arg(gptpCard.g.sync_interval_ms)
                        + (gptpCard.g.sync_rate_hz != null ? qsTr(" (observed %1 Hz)").arg(gptpCard.g.sync_rate_hz) : "")
                    : "—"
                color: Theme.text
                font.pixelSize: 12
            }
            Label { text: qsTr("Announce interval"); color: Theme.muted }
            Label { text: gptpCard.g.announce_interval_ms != null ? qsTr("%1 ms").arg(gptpCard.g.announce_interval_ms) : "—"; color: Theme.text; font.pixelSize: 12 }

            Label { text: qsTr("Clocks / domains"); color: Theme.muted }
            Label {
                text: qsTr("%1 clock(s) on domain(s) %2").arg((gptpCard.g.clocks || []).length).arg((gptpCard.g.domains || []).join(", "))
                color: Theme.text
                font.pixelSize: 12
                Layout.columnSpan: 3
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
        }
    }

    // SOME/IP services (from Service Discovery)
    Card {
        id: someipCard
        title: qsTr("SOME/IP services")
        visible: eth.has_someip
        Layout.fillWidth: true
        Layout.preferredHeight: someipColumn.implicitHeight + 2 * Theme.spacing

        ColumnLayout {
            id: someipColumn
            anchors.fill: parent
            spacing: 4

            RowLayout {
                Layout.fillWidth: true
                spacing: 16
                Repeater {
                    model: [qsTr("Service"), qsTr("Instance"), qsTr("Version"), qsTr("Endpoints"), qsTr("Offers")]
                    Label {
                        required property int index
                        required property var modelData
                        text: modelData
                        color: Theme.muted
                        font.pixelSize: 11
                        Layout.preferredWidth: index === 3 ? -1 : 70
                        Layout.fillWidth: index === 3
                    }
                }
            }
            Repeater {
                model: eth.someip_services
                RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 16
                    Label { text: modelData.service; color: Theme.text; font.family: "monospace"; font.pixelSize: 12; Layout.preferredWidth: 70 }
                    Label { text: modelData.instance; color: Theme.text; font.family: "monospace"; font.pixelSize: 12; Layout.preferredWidth: 70 }
                    Label { text: modelData.version; color: Theme.text; font.pixelSize: 12; Layout.preferredWidth: 70 }
                    Label { text: modelData.endpoints || "—"; color: Theme.text; font.family: "monospace"; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                    Label { text: String(modelData.offers); color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 70 }
                }
            }
        }
    }

    // AVTP stream health (per-stream sequence loss)
    Card {
        id: avtpCard
        title: qsTr("AVTP streams")
        visible: eth.has_avtp_streams
        Layout.fillWidth: true
        Layout.preferredHeight: avtpColumn.implicitHeight + 2 * Theme.spacing

        ColumnLayout {
            id: avtpColumn
            anchors.fill: parent
            spacing: 4

            RowLayout {
                Layout.fillWidth: true
                spacing: 16
                Repeater {
                    model: [qsTr("Stream"), qsTr("Format"), qsTr("Packets"), qsTr("Lost"), qsTr("Loss"), qsTr("Rate")]
                    Label {
                        required property int index
                        required property var modelData
                        text: modelData
                        color: Theme.muted
                        font.pixelSize: 11
                        Layout.preferredWidth: index === 0 ? 170 : 80
                        Layout.fillWidth: index === 1
                    }
                }
            }
            Repeater {
                model: eth.avtp_streams
                RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 16
                    Label { text: modelData.stream; color: Theme.text; font.family: "monospace"; font.pixelSize: 12; Layout.preferredWidth: 170; elide: Text.ElideRight }
                    Label { text: modelData.format; color: Theme.accent; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                    Label { text: modelData.packets; color: Theme.text; font.family: "monospace"; font.pixelSize: 12; Layout.preferredWidth: 80 }
                    Label { text: modelData.lost; color: modelData.lost > 0 ? Theme.danger : Theme.text; font.family: "monospace"; font.pixelSize: 12; Layout.preferredWidth: 80 }
                    Label { text: modelData.loss; color: modelData.lost > 0 ? Theme.danger : Theme.muted; font.family: "monospace"; font.pixelSize: 12; Layout.preferredWidth: 80 }
                    Label { text: modelData.rate; color: Theme.muted; font.family: "monospace"; font.pixelSize: 12; Layout.preferredWidth: 80 }
                }
            }
        }
    }

    // Packets
    Card {
        title: qsTr("Packets")
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 2
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Repeater {
                    model: [qsTr("Time"), qsTr("Source"), qsTr("Destination"), qsTr("Protocol"), qsTr("Len"), qsTr("Info")]
                    Label {
                        required property int index
                        required property string modelData
                        text: modelData
                        color: Theme.muted
                        font.bold: true
                        font.pixelSize: 12
                        Layout.preferredWidth: [80, 170, 170, 90, 50, 0][index] || -1
                        Layout.fillWidth: index === 5
                    }
                }
            }
            ListView {
                id: packetList
                clip: true
                model: eth.packets
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                onCountChanged: if (atYEnd || count <= 1) positionViewAtEnd()
                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: packetList.width
                    height: 24
                    color: modelData.number === root.selectedNumber
                        ? Theme.alpha(Theme.accent, 0.25)
                        : (index % 2 ? Theme.alpha(Theme.cardAlt, 0.5) : "transparent")
                    MouseArea { anchors.fill: parent; onClicked: root.selectedNumber = modelData.number }
                    RowLayout {
                        anchors.fill: parent
                        spacing: 12
                        Label { text: modelData.time; font.family: "monospace"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 80 }
                        Label { text: modelData.source; font.family: "monospace"; font.pixelSize: 12; color: Theme.text; Layout.preferredWidth: 170; elide: Text.ElideRight }
                        Label { text: modelData.destination; font.family: "monospace"; font.pixelSize: 12; color: Theme.text; Layout.preferredWidth: 170; elide: Text.ElideRight }
                        Label { text: modelData.protocol; font.pixelSize: 12; color: Theme.accent; Layout.preferredWidth: 90; elide: Text.ElideRight }
                        Label { text: modelData.length; font.family: "monospace"; font.pixelSize: 12; color: Theme.muted; Layout.preferredWidth: 50 }
                        Label { text: modelData.info; font.pixelSize: 12; color: Theme.text; elide: Text.ElideRight; Layout.fillWidth: true }
                    }
                }
            }

            // Packet details — the field-by-field dissection of the selected packet
            Rectangle {
                visible: root.detailRows.length > 0
                Layout.fillWidth: true
                Layout.preferredHeight: 200
                color: Theme.alpha(Theme.cardAlt, 0.4)
                radius: Theme.radius
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 6
                    spacing: 2
                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: qsTr("Packet %1 details").arg(root.selectedNumber)
                            color: Theme.muted; font.bold: true; font.pixelSize: 12
                            Layout.fillWidth: true
                        }
                        Button { text: qsTr("Close"); flat: true; onClicked: root.selectedNumber = 0 }
                    }
                    ListView {
                        id: detailList
                        clip: true
                        model: root.detailRows
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ScrollBar.vertical: ScrollBar {}
                        delegate: RowLayout {
                            required property var modelData
                            width: detailList.width
                            spacing: 8
                            Item { Layout.preferredWidth: modelData.depth * 16 }
                            Label { text: modelData.label; color: Theme.accent; font.family: "monospace"; font.pixelSize: 12 }
                            Label { text: modelData.value; color: Theme.text; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                        }
                    }
                }
            }
        }
    }

    FileChooser {
        id: openDialog
        title: qsTr("Open a pcap capture")
        nameFilters: [qsTr("pcap files (*.pcap *.cap)"), qsTr("All files (*)")]
        onAccepted: {
            const error = eth.open_pcap(selectedFile)
            if (error)
                statusLabel.show(error)
        }
    }

    FileChooser {
        id: saveDialog
        title: qsTr("Save the packets")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "pcap"
        nameFilters: [qsTr("pcap files (*.pcap)")]
        onAccepted: statusLabel.show(eth.export_pcap(selectedFile))
    }

    FileChooser {
        id: saveCanDialog
        title: qsTr("Save the tunnelled CAN frames as a log")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "asc"
        nameFilters: [qsTr("CAN log files (*.asc *.blf *.log *.csv *.trc)")]
        onAccepted: statusLabel.show(eth.export_can_log(selectedFile))
    }
}
