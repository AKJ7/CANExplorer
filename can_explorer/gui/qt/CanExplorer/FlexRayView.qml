import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0
import CanExplorer.Plots 1.0


// FlexRay: frames from a simulated cluster, a log file or a Vector or Intrepid interface, decoded with the cluster
// description, checked against its schedule, plotted and sent
ColumnLayout {
    id: root
    readonly property var fr: connectionManager.flexray
    readonly property string pageTitle: fr.source_name
    readonly property var stats: fr.statistics
    readonly property var report: fr.analysis
    property var sendValues: ({})
    property url vectorConfigUrl
    property string vectorConfigName: ""

    function report_(error) {
        errorLabel.text = error || ""
    }

    spacing: 8

    // The cluster description and the sources
    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Button {
            text: root.fr.database_name ? qsTr("Cluster: %1").arg(root.fr.database_name) : qsTr("Load Cluster…")
            onClicked: databaseChooser.open()
            ToolTip.visible: hovered
            ToolTip.text: qsTr("A FIBEX or AUTOSAR ARXML description of the FlexRay cluster: names the frames of each slot and decodes their signals")
        }
        ComboBox {
            id: clusterBox
            visible: root.fr.clusters.length > 1
            model: root.fr.clusters
            currentIndex: Math.max(0, root.fr.clusters.indexOf(root.fr.cluster))
            onActivated: root.report_(root.fr.choose_cluster(currentText))
            Layout.preferredWidth: 150
            ToolTip.visible: hovered
            ToolTip.text: qsTr("The file describes several FlexRay clusters: the one to use")
        }
        ToolSeparator {}
        Button {
            text: qsTr("Simulate")
            enabled: root.fr.database_name !== ""
            onClicked: root.report_(root.fr.start_simulation(cycleBox.value))
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Send the cluster's frames in their slots and cycles, with changing signal values, to try the tool without hardware")
        }
        SpinBox {
            id: cycleBox
            from: 0; to: 64; value: 0
            editable: true
            textFromValue: (value, locale) => value === 0 ? qsTr("auto") : value + " ms"
            valueFromText: (text, locale) => text === qsTr("auto") ? 0 : parseInt(text)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Cycle time of the simulated cluster (auto: the description's, else 5 ms)")
        }
        Button {
            text: qsTr("Open Log…")
            onClicked: captureChooser.open()
            ToolTip.visible: hovered
            ToolTip.text: qsTr("A pcap or pcapng capture (FlexRay or IEEE 1722), or a Vector BLF or ASC log")
        }
        Button { text: qsTr("Stop"); enabled: root.fr.running; onClicked: root.fr.stop() }
        Item { Layout.fillWidth: true }
    }

    // Interfaces
    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Button {
            text: qsTr("Find Interfaces")
            onClicked: root.report_(root.fr.refresh_interfaces())
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Look for the FlexRay channels of Vector (XL Driver Library) and Intrepid (icsneopy) interfaces")
        }
        ComboBox {
            id: interfaceBox
            model: root.fr.interfaces.length > 0 ? root.fr.interfaces.map(c => c.name) : [qsTr("No interface")]
            enabled: root.fr.interfaces.length > 0
            Layout.preferredWidth: 200
        }
        readonly property string kind: root.fr.interfaces.length > 0 && interfaceBox.currentIndex >= 0
                                       ? root.fr.interfaces[interfaceBox.currentIndex].kind : ""
        ComboBox {
            id: modeBox
            enabled: root.fr.interfaces.length > 0
            model: [qsTr("Watch the bus"), qsTr("Take part (send)")]
            Layout.preferredWidth: 150
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Watch: receive without joining the cluster. Take part: the controller joins the cluster and can send frames")
        }
        ComboBox {
            id: ecuBox
            visible: modeBox.currentIndex === 1 && parent.kind === "intrepid"
            model: root.fr.controllers.length > 0 ? root.fr.controllers : [qsTr("No ECU in the description")]
            enabled: root.fr.controllers.length > 0
            Layout.preferredWidth: 160
            ToolTip.visible: hovered
            ToolTip.text: qsTr("The ECU of the description to be: its FlexRay controller's parameters configure the interface")
        }
        Button {
            visible: modeBox.currentIndex === 1 && parent.kind === "vector"
            text: root.vectorConfigName ? qsTr("Configuration: %1").arg(root.vectorConfigName) : qsTr("Configuration…")
            onClicked: configChooser.open()
            ToolTip.visible: hovered
            ToolTip.text: qsTr("XLfrClusterConfig values (JSON or name = value lines), as Vector's tools give them; without one, the channel's own configuration is used")
        }
        CheckBox {
            id: coldstartBox
            visible: modeBox.currentIndex === 1
            text: qsTr("Coldstart")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Start the cluster as a coldstart node when no other node does")
        }
        Button {
            text: qsTr("Start")
            enabled: root.fr.interfaces.length > 0
            onClicked: root.report_(root.fr.start_interface(interfaceBox.currentIndex, modeBox.currentIndex === 1 ? "node" : "watch",
                                                            root.fr.controllers.length > 0 ? ecuBox.currentText : "",
                                                            coldstartBox.checked, root.vectorConfigUrl))
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Experimental: written from the vendors' published interfaces, not yet tested on a device")
        }
        Item { Layout.fillWidth: true }
        Label {
            visible: root.fr.mode !== ""
            text: root.fr.mode === "node" ? qsTr("Taking part in the cluster") : qsTr("Watching the bus")
            color: Theme.muted
        }
    }

    Label {
        id: errorLabel
        text: root.fr.error
        visible: text.length > 0
        color: Theme.danger
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Statistics
    RowLayout {
        spacing: 24
        Layout.fillWidth: true
        Repeater {
            model: [
                { label: qsTr("Frames"), value: root.stats.frames },
                { label: qsTr("Slots"), value: root.stats.slots },
                { label: qsTr("Cycle"), value: root.stats.cycle >= 0 ? root.stats.cycle : "–" },
                { label: qsTr("Null frames"), value: root.stats.null },
                { label: qsTr("With errors"), value: root.stats.errors },
                { label: qsTr("Events"), value: root.stats.events },
                { label: qsTr("Issues"), value: root.report.issues.length },
            ]
            delegate: ColumnLayout {
                required property var modelData
                spacing: 0
                Label { text: modelData.label; color: Theme.muted; font.pixelSize: 12 }
                Label { text: String(modelData.value); font.pixelSize: Theme.valueSize; font.bold: true }
            }
        }
        Item { Layout.fillWidth: true }
        Label {
            visible: root.fr.recording
            text: qsTr("Recording to %1").arg(root.fr.recording_file)
            color: Theme.danger
        }
        TextField {
            placeholderText: qsTr("Slots, e.g. 12 40-50")
            Layout.preferredWidth: 180
            onEditingFinished: root.report_(root.fr.set_filter(text))
        }
        Button {
            text: root.fr.recording ? qsTr("Stop Recording") : qsTr("Record…")
            enabled: root.fr.running || root.fr.recording
            onClicked: root.fr.recording ? root.fr.stop_recording() : recordChooser.open()
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Write the frames that arrive to a pcap file (LINKTYPE_FLEXRAY), which Wireshark opens too")
        }
        Button { text: qsTr("Save…"); enabled: root.stats.frames > 0; onClicked: saveChooser.open() }
        Button { text: qsTr("Clear"); onClicked: root.fr.clear() }
    }

    TabBar {
        id: tabs
        objectName: "flexrayTabs"
        Layout.fillWidth: true
        TabButton { text: qsTr("Trace"); width: implicitWidth }
        TabButton { text: qsTr("Slots"); width: implicitWidth }
        TabButton { text: qsTr("Analysis"); width: implicitWidth }
        TabButton { text: qsTr("Events"); width: implicitWidth }
        TabButton { text: qsTr("Signals"); width: implicitWidth }
        TabButton { text: qsTr("Send"); width: implicitWidth }
        TabButton { text: qsTr("Cluster"); width: implicitWidth }
    }

    StackLayout {
        currentIndex: tabs.currentIndex
        Layout.fillWidth: true
        Layout.fillHeight: true

        // The frames, newest last
        ColumnLayout {
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 8
                Repeater {
                    model: [[qsTr("Time [s]"), 100], [qsTr("Ch"), 30], [qsTr("Slot"), 50], [qsTr("Cycle"), 50], [qsTr("Len"), 40], [qsTr("Flags"), 50], [qsTr("Data [hex]"), 340]]
                    delegate: Label {
                        required property var modelData
                        text: modelData[0]; font.bold: true; Layout.preferredWidth: modelData[1]
                    }
                }
                Label { text: qsTr("Info"); font.bold: true; Layout.fillWidth: true }
            }
            ListView {
                id: traceList
                clip: true
                model: root.fr.frames
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                onCountChanged: if (!root.fr.paused) positionViewAtEnd()
                delegate: RowLayout {
                    required property var modelData
                    width: traceList.width
                    x: 8
                    Label { text: modelData.time; font.family: "monospace"; Layout.preferredWidth: 100 }
                    Label { text: modelData.channel; font.family: "monospace"; Layout.preferredWidth: 30 }
                    Label { text: modelData.slot; font.family: "monospace"; Layout.preferredWidth: 50 }
                    Label { text: modelData.cycle; font.family: "monospace"; Layout.preferredWidth: 50 }
                    Label { text: modelData.length; font.family: "monospace"; Layout.preferredWidth: 40 }
                    Label { text: modelData.flags; font.family: "monospace"; color: Theme.muted; Layout.preferredWidth: 50 }
                    Label { text: modelData.data; font.family: "monospace"; elide: Text.ElideRight; Layout.preferredWidth: 340 }
                    Label {
                        text: modelData.info
                        color: modelData.error ? Theme.danger : Theme.text
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }
            }
        }

        // One row per slot and channel
        ColumnLayout {
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 8
                Repeater {
                    model: [[qsTr("Slot"), 50], [qsTr("Ch"), 30], [qsTr("Segment"), 70], [qsTr("Frames"), 70], [qsTr("Cycles"), 60], [qsTr("Null"), 50], [qsTr("Last data [hex]"), 320]]
                    delegate: Label {
                        required property var modelData
                        text: modelData[0]; font.bold: true; Layout.preferredWidth: modelData[1]
                    }
                }
                Label { text: qsTr("Last value"); font.bold: true; Layout.fillWidth: true }
            }
            ListView {
                id: slotList
                clip: true
                model: root.fr.slots
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                delegate: RowLayout {
                    required property var modelData
                    width: slotList.width
                    x: 8
                    Label { text: modelData.slot; font.family: "monospace"; Layout.preferredWidth: 50 }
                    Label { text: modelData.channel; font.family: "monospace"; Layout.preferredWidth: 30 }
                    Label { text: modelData.segment; color: Theme.muted; Layout.preferredWidth: 70 }
                    Label { text: modelData.count; font.family: "monospace"; Layout.preferredWidth: 70 }
                    Label { text: modelData.cycles; font.family: "monospace"; Layout.preferredWidth: 60 }
                    Label { text: modelData.null; font.family: "monospace"; Layout.preferredWidth: 50 }
                    Label { text: modelData.data; font.family: "monospace"; elide: Text.ElideRight; Layout.preferredWidth: 320 }
                    Label {
                        text: modelData.info + (modelData.errors > 0 ? qsTr(" (%1 with errors)").arg(modelData.errors) : "")
                        color: modelData.errors > 0 ? Theme.warning : Theme.text
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }
            }
        }

        // Schedule, timing and bus health
        ScrollView {
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 8
                Label {
                    visible: !root.report.described
                    text: qsTr("Load the cluster description to check the frames against the schedule; without it only the bus health and the channels are compared.")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Label { text: qsTr("Channels"); font.bold: true }
                GridLayout {
                    columns: 9
                    columnSpacing: 16
                    rowSpacing: 2
                    Repeater {
                        model: [qsTr("Channel"), qsTr("Frames"), qsTr("Null"), qsTr("With errors"), qsTr("Cycles"),
                                qsTr("Cycle jumps"), qsTr("Sync frames"), qsTr("Cycles < 2 sync"), qsTr("Cycle time")]
                        Label { required property string modelData; text: modelData; color: Theme.muted; font.pixelSize: 12 }
                    }
                    Repeater {
                        model: root.report.channels
                        delegate: Repeater {
                            required property var modelData
                            model: [modelData.channel, modelData.frames, modelData.null, modelData.with_errors, modelData.cycles,
                                    modelData.cycle_jumps, modelData.sync, modelData.few_sync_cycles, modelData.cycle_time]
                            Label { required property var modelData; text: String(modelData); font.family: "monospace" }
                        }
                    }
                }
                Repeater {
                    model: root.report.channels.filter(c => c.errors)
                    Label {
                        required property var modelData
                        text: qsTr("Errors on %1: %2").arg(modelData.channel).arg(modelData.errors)
                        color: Theme.warning
                    }
                }
                Label { visible: root.report.timing !== ""; text: root.report.timing; color: Theme.muted }
                Label {
                    visible: root.report.described && !root.report.timed
                    text: qsTr("The description gives no static slot timing (macrotick, static slot length): the frames' timing is not checked.")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Label { text: root.report.comparison; color: Theme.muted }
                Repeater {
                    model: root.report.events
                    Label { required property string modelData; text: modelData; color: Theme.muted }
                }
                Label {
                    text: root.report.issues.length > 0 ? qsTr("Issues") : qsTr("No issues")
                    font.bold: true
                }
                RowLayout {
                    visible: root.report.issues.length > 0
                    Repeater {
                        model: [[qsTr("Slot"), 50], [qsTr("Ch"), 40], [qsTr("Issue"), 150], [qsTr("Frame"), 150], [qsTr("Count"), 60], [qsTr("Last cycle"), 70]]
                        Label { required property var modelData; text: modelData[0]; font.bold: true; Layout.preferredWidth: modelData[1] }
                    }
                    Label { text: qsTr("Detail"); font.bold: true }
                }
                Repeater {
                    model: root.report.issues
                    delegate: RowLayout {
                        required property var modelData
                        Label { text: modelData.slot; font.family: "monospace"; Layout.preferredWidth: 50 }
                        Label { text: modelData.channel; font.family: "monospace"; Layout.preferredWidth: 40 }
                        Label { text: modelData.kind; color: Theme.warning; Layout.preferredWidth: 150 }
                        Label { text: modelData.name; elide: Text.ElideRight; Layout.preferredWidth: 150 }
                        Label { text: modelData.count; font.family: "monospace"; Layout.preferredWidth: 60 }
                        Label { text: modelData.cycle; font.family: "monospace"; Layout.preferredWidth: 70 }
                        Label { text: modelData.detail; color: Theme.muted }
                    }
                }
            }
        }

        // Symbols, controller states and errors
        ColumnLayout {
            spacing: 0
            Label {
                visible: root.fr.events.length === 0
                text: qsTr("No symbols, controller states or errors were reported.")
                color: Theme.muted
                padding: 8
            }
            ListView {
                id: eventList
                clip: true
                model: root.fr.events
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                delegate: RowLayout {
                    required property var modelData
                    width: eventList.width
                    x: 8
                    Label { text: modelData.time; font.family: "monospace"; Layout.preferredWidth: 100 }
                    Label { text: modelData.kind; color: modelData.kind === "error" ? Theme.danger : Theme.muted; Layout.preferredWidth: 60 }
                    Label { text: modelData.channel; font.family: "monospace"; Layout.preferredWidth: 40 }
                    Label { text: modelData.cycle; font.family: "monospace"; Layout.preferredWidth: 40 }
                    Label { text: modelData.text; elide: Text.ElideRight; Layout.fillWidth: true }
                }
            }
        }

        // Plotting signals
        SplitView {
            ListView {
                id: signalList
                SplitView.preferredWidth: 300
                clip: true
                model: root.fr.signals
                ScrollBar.vertical: ScrollBar {}
                header: Label {
                    text: root.fr.database_name ? qsTr("Click a signal to plot it") : qsTr("Load the cluster description to plot its signals.")
                    color: Theme.muted
                    padding: 8
                }
                delegate: ItemDelegate {
                    required property var modelData
                    width: signalList.width
                    text: modelData.frame + "." + modelData.signal + (modelData.unit ? " [" + modelData.unit + "]" : "")
                    highlighted: root.fr.tracked_signals.indexOf(modelData.frame + "." + modelData.signal) >= 0
                    onClicked: highlighted ? root.fr.untrack_key(modelData.frame + "." + modelData.signal)
                                           : root.fr.plot_signal(modelData.frame, modelData.signal)
                }
            }
            ColumnLayout {
                spacing: 4
                Label {
                    visible: root.fr.tracked_signals.length === 0
                    text: qsTr("No signal plotted.")
                    color: Theme.muted
                    padding: 8
                }
                RowLayout {
                    visible: root.fr.tracked_signals.length > 0
                    Label { text: root.fr.plot_view.range_text; color: Theme.muted }
                    Item { Layout.fillWidth: true }
                    Button { text: qsTr("Fit"); onClicked: root.fr.plot_view.fit_all() }
                    Button { text: qsTr("Live"); onClicked: root.fr.plot_view.follow_live() }
                }
                Repeater {
                    model: root.fr.plot_view.charts
                    delegate: ColumnLayout {
                        id: chart
                        required property var modelData
                        required property int index
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Label { text: chart.modelData.join(", "); color: Theme.muted; font.pixelSize: 12 }
                        SignalPlot {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 80
                            view: root.fr.plot_view
                            keys: chart.modelData
                            palette: Theme.chart
                            text_color: Theme.muted
                            grid_color: Theme.border
                            cursor_color: Theme.warning
                            WheelHandler {
                                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                                onWheel: event => root.fr.plot_view.zoom(event.angleDelta.y > 0 ? 0.8 : 1.25, parent.time_at(point.position.x))
                            }
                            TapHandler { onDoubleTapped: root.fr.plot_view.follow_live() }
                        }
                    }
                }
            }
        }

        // Sending frames
        ScrollView {
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 8
                Label {
                    visible: !root.fr.can_transmit
                    text: qsTr("Sending needs the simulation, or an interface taking part in the cluster (Take part).")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Label { text: qsTr("A frame of the description"); font.bold: true }
                RowLayout {
                    ComboBox {
                        id: sendFrameBox
                        model: root.fr.definitions.map(d => d.name + " (" + qsTr("slot %1, %2").arg(d.slot).arg(d.cycles) + ")")
                        Layout.preferredWidth: 300
                        onActivated: root.sendValues = ({})
                    }
                    ComboBox { id: sendChannelBox; model: [qsTr("Its channels"), "A", "B", "A+B"]; Layout.preferredWidth: 120 }
                    Button {
                        text: qsTr("Send")
                        enabled: root.fr.can_transmit && root.fr.definitions.length > 0
                        onClicked: root.report_(root.fr.transmit_definition(sendFrameBox.currentIndex, root.sendValues,
                                                                           sendChannelBox.currentIndex === 0 ? "" : sendChannelBox.currentText))
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Send the frame in its slot and cycles with these physical values (empty: 0) until stopped; the PDUs' update bits are set")
                    }
                }
                Repeater {
                    model: root.fr.signals.filter(s => s.definition === sendFrameBox.currentIndex)
                    delegate: RowLayout {
                        required property var modelData
                        spacing: 12
                        Label {
                            text: modelData.signal + (modelData.unit ? " [" + modelData.unit + "]" : "")
                            Layout.preferredWidth: 220
                            elide: Text.ElideRight
                        }
                        TextField {
                            placeholderText: "0"
                            Layout.preferredWidth: 140
                            onEditingFinished: {
                                const values = Object.assign({}, root.sendValues)
                                if (text === "")
                                    delete values[modelData.signal]
                                else
                                    values[modelData.signal] = text
                                root.sendValues = values
                            }
                        }
                        Label { text: modelData.choices; color: Theme.muted; elide: Text.ElideRight; Layout.fillWidth: true }
                    }
                }
                Label { text: qsTr("Raw bytes in a slot"); font.bold: true }
                RowLayout {
                    SpinBox { id: rawSlot; from: 1; to: 2047; value: 1; editable: true }
                    Label { text: qsTr("from cycle") }
                    SpinBox { id: rawBase; from: 0; to: 63; value: 0; editable: true }
                    Label { text: qsTr("every") }
                    ComboBox { id: rawRepetition; model: [1, 2, 4, 5, 8, 10, 16, 20, 32, 40, 50, 64]; Layout.preferredWidth: 80 }
                    ComboBox { id: rawChannel; model: ["A", "B", "A+B"]; Layout.preferredWidth: 80 }
                    TextField { id: rawData; placeholderText: qsTr("Bytes (hex), an even number"); Layout.preferredWidth: 260 }
                    Button {
                        text: qsTr("Send")
                        enabled: root.fr.can_transmit
                        onClicked: root.report_(root.fr.transmit_raw(rawSlot.value, rawBase.value, rawRepetition.currentValue,
                                                                    rawChannel.currentText, rawData.text))
                    }
                }
                Label { text: root.fr.transmissions.length > 0 ? qsTr("Sending") : ""; font.bold: true }
                Repeater {
                    model: root.fr.transmissions
                    delegate: RowLayout {
                        required property var modelData
                        Label { text: qsTr("Slot %1").arg(modelData.slot); font.family: "monospace"; Layout.preferredWidth: 80 }
                        Label { text: modelData.name; Layout.preferredWidth: 150; elide: Text.ElideRight }
                        Label { text: modelData.channels; Layout.preferredWidth: 40 }
                        Label { text: modelData.cycles; Layout.preferredWidth: 140 }
                        Label { text: modelData.data; font.family: "monospace"; Layout.preferredWidth: 300; elide: Text.ElideRight }
                        Button { text: qsTr("Stop"); onClicked: root.report_(root.fr.stop_transmit(modelData.slot)) }
                    }
                }
            }
        }

        // The cluster's parameters, frames and PDUs
        ScrollView {
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 8
                Label {
                    visible: root.fr.database_name === ""
                    text: qsTr("Load the cluster description (FIBEX or ARXML) to name the frames and decode their signals.")
                    color: Theme.muted
                    padding: 8
                }
                Flow {
                    visible: root.fr.parameters.length > 0
                    spacing: 24
                    Layout.fillWidth: true
                    Repeater {
                        model: root.fr.parameters
                        delegate: ColumnLayout {
                            required property var modelData
                            spacing: 0
                            Label { text: modelData.label; color: Theme.muted; font.pixelSize: 12 }
                            Label { text: modelData.value; font.bold: true }
                        }
                    }
                }
                Label {
                    visible: root.fr.controllers.length > 0
                    text: qsTr("ECUs with a FlexRay controller: %1").arg(root.fr.controllers.join(", "))
                    color: Theme.muted
                }
                Label { visible: root.fr.definitions.length > 0; text: qsTr("Frames"); font.bold: true }
                Repeater {
                    model: root.fr.definitions
                    delegate: RowLayout {
                        required property var modelData
                        Label { text: modelData.slot; font.family: "monospace"; Layout.preferredWidth: 50 }
                        Label { text: modelData.name; font.bold: true; Layout.preferredWidth: 180; elide: Text.ElideRight }
                        Label { text: modelData.channels; Layout.preferredWidth: 40 }
                        Label { text: modelData.cycles; Layout.preferredWidth: 160 }
                        Label { text: qsTr("%1 bytes").arg(modelData.length); Layout.preferredWidth: 80 }
                        Label { text: modelData.signals; color: Theme.muted; elide: Text.ElideRight; Layout.fillWidth: true }
                    }
                }
                Label { visible: root.fr.pdus.length > 0; text: qsTr("PDUs"); font.bold: true }
                RowLayout {
                    visible: root.fr.pdus.length > 0
                    Repeater {
                        model: [[qsTr("Slot"), 50], [qsTr("Frame"), 160], [qsTr("PDU"), 160], [qsTr("Byte"), 50], [qsTr("Length"), 60], [qsTr("Update bit"), 80]]
                        Label { required property var modelData; text: modelData[0]; color: Theme.muted; Layout.preferredWidth: modelData[1] }
                    }
                    Label { text: qsTr("Signals"); color: Theme.muted }
                }
                Repeater {
                    model: root.fr.pdus
                    delegate: RowLayout {
                        required property var modelData
                        Label { text: modelData.slot; font.family: "monospace"; Layout.preferredWidth: 50 }
                        Label { text: modelData.frame; Layout.preferredWidth: 160; elide: Text.ElideRight }
                        Label { text: modelData.pdu; Layout.preferredWidth: 160; elide: Text.ElideRight }
                        Label { text: modelData.offset; font.family: "monospace"; Layout.preferredWidth: 50 }
                        Label { text: modelData.length; font.family: "monospace"; Layout.preferredWidth: 60 }
                        Label { text: modelData.update_bit || "–"; font.family: "monospace"; Layout.preferredWidth: 80 }
                        Label { text: modelData.signals; color: Theme.muted; elide: Text.ElideRight; Layout.fillWidth: true }
                    }
                }
            }
        }
    }

    FileChooser {
        id: databaseChooser
        title: qsTr("FlexRay cluster description")
        nameFilters: [qsTr("FIBEX and ARXML (*.xml *.fibex *.arxml)"), qsTr("All files (*)")]
        onAccepted: root.report_(root.fr.load_database(selectedFile))
    }
    FileChooser {
        id: captureChooser
        title: qsTr("FlexRay capture or log")
        nameFilters: [qsTr("Captures and logs (*.pcap *.pcapng *.blf *.asc)"), qsTr("All files (*)")]
        onAccepted: root.report_(root.fr.open_capture(selectedFile))
    }
    FileChooser {
        id: configChooser
        title: qsTr("Vector FlexRay configuration (XLfrClusterConfig)")
        nameFilters: [qsTr("Configurations (*.json *.txt *.cfg)"), qsTr("All files (*)")]
        onAccepted: {
            root.vectorConfigUrl = selectedFile
            root.vectorConfigName = String(selectedFile).split("/").pop()
        }
    }
    FileChooser {
        id: recordChooser
        title: qsTr("Record FlexRay frames")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "pcap"
        nameFilters: [qsTr("pcap (*.pcap)")]
        onAccepted: root.report_(root.fr.start_recording(selectedFile))
    }
    FileChooser {
        id: saveChooser
        title: qsTr("Save FlexRay frames")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "pcap"
        nameFilters: [qsTr("pcap (*.pcap)")]
        onAccepted: root.report_(root.fr.save_frames(selectedFile))
    }
}
