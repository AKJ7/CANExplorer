import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0
import CanExplorer.Plots 1.0


SplitView {
    id: root
    required property var connection
    readonly property var view: connection.plot_view
    readonly property var colors: Theme.chart
    property string status: ""

    function format(value) {
        return value === "" || value === undefined ? "–" : Number(value).toPrecision(6).replace(/\.?0+$/, "")
    }

    // Signal list
    ColumnLayout {
        SplitView.preferredWidth: 280
        SplitView.minimumWidth: 180

        Label {
            text: root.connection.database_file
                ? qsTr("Signals — check to plot")
                : qsTr("Load a signal database to plot signals")
            padding: 4
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
        TextField {
            id: signalFilter
            placeholderText: qsTr("Search signals")
            enabled: root.connection.database_file !== ""
            Layout.fillWidth: true
        }
        ListView {
            id: signalList
            clip: true
            Layout.fillWidth: true
            Layout.fillHeight: true
            ScrollBar.vertical: ScrollBar {}
            model: root.connection.database_signals.filter(
                info => info.key.toLowerCase().includes(signalFilter.text.toLowerCase()))
            delegate: CheckDelegate {
                required property var modelData
                width: signalList.width
                text: modelData.key + (modelData.unit ? ` [${modelData.unit}]` : "")
                checked: root.connection.tracked_signals.includes(modelData.key)
                onToggled: checked
                    ? root.connection.track_signal(modelData.message, modelData.signal, modelData.unit)
                    : root.connection.untrack_signal(modelData.message, modelData.signal)
            }
        }
    }

    ColumnLayout {
        SplitView.fillWidth: true
        spacing: 8
        clip: true

        // Time axis and tools
        RowLayout {
            spacing: 8
            Layout.leftMargin: 4
            Layout.rightMargin: 4
            Label { text: qsTr("Live window") }
            ComboBox {
                id: windowBox
                model: root.view.windows
                currentIndex: root.view.windows.indexOf(root.view.window)
                displayText: qsTr("%1 s").arg(currentValue)
                onActivated: root.view.set_window(currentValue)
            }
            Button {
                text: root.view.follow ? qsTr("● Live") : qsTr("Live")
                highlighted: root.view.follow
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Follow the newest values (double click a chart)")
                onClicked: root.view.follow_live()
            }
            Button {
                text: qsTr("Fit all")
                enabled: root.connection.tracked_signals.length > 0
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Show everything recorded for the plotted signals")
                onClicked: root.view.fit_all()
            }
            Label {
                text: root.view.range_text
                color: Theme.muted
                font.pixelSize: 12
                elide: Text.ElideRight
                Layout.fillWidth: true
                Layout.minimumWidth: 0
            }
            Button {
                text: qsTr("Clear cursors")
                visible: root.view.has_cursors
                onClicked: root.view.clear_cursors()
            }
            Button {
                text: qsTr("Export CSV…")
                enabled: root.connection.tracked_signals.length > 0
                onClicked: exportDialog.open()
            }
        }

        Label {
            visible: root.status.length > 0
            text: root.status
            color: Theme.muted
            Layout.leftMargin: 4
        }

        ScrollView {
            id: plotScroll
            clip: true
            Layout.fillWidth: true
            Layout.fillHeight: true

            Column {
                width: plotScroll.availableWidth
                spacing: 8

                Label {
                    visible: root.connection.tracked_signals.length === 0
                    text: qsTr("No signals selected")
                    padding: 8
                }

                Repeater {
                    model: root.view.charts
                    Rectangle {
                        id: chart
                        required property var modelData
                        required property int index
                        width: parent.width
                        height: 230
                        radius: Theme.smallRadius
                        color: Theme.alpha(Theme.cardAlt, 0.5)
                        border.color: Theme.alpha(Theme.border, 0.6)

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 6
                            spacing: 2

                            // Legend: one chip per signal, its menu moves it to another chart
                            RowLayout {
                                spacing: 6
                                Layout.fillWidth: true
                                Repeater {
                                    model: chart.modelData
                                    Button {
                                        id: chip
                                        required property string modelData
                                        flat: true
                                        padding: 4
                                        horizontalPadding: 8
                                        contentItem: RowLayout {
                                            spacing: 6
                                            Rectangle {
                                                width: 10
                                                height: 10
                                                radius: 5
                                                color: root.colors[root.view.color_index(chip.modelData) % root.colors.length]
                                            }
                                            Label {
                                                text: chip.modelData
                                                color: Theme.text
                                                font.pixelSize: 12
                                                elide: Text.ElideRight
                                                Layout.maximumWidth: 260
                                            }
                                            Label { text: "▾"; color: Theme.muted; font.pixelSize: 10 }
                                        }
                                        onClicked: chipMenu.popup()
                                        Menu {
                                            id: chipMenu
                                            MenuItem {
                                                text: qsTr("Show in its own chart")
                                                enabled: chart.modelData.length > 1
                                                onTriggered: root.view.move_to_chart(chip.modelData, -1)
                                            }
                                            Instantiator {
                                                model: root.view.charts.length
                                                delegate: MenuItem {
                                                    required property int index
                                                    text: qsTr("Move to chart %1 (%2)").arg(index + 1)
                                                        .arg(root.view.charts[index] ? root.view.charts[index].join(", ") : "")
                                                    visible: index !== chart.index
                                                    height: visible ? implicitHeight : 0
                                                    onTriggered: root.view.move_to_chart(chip.modelData, index)
                                                }
                                                onObjectAdded: (index, object) => chipMenu.insertItem(1 + index, object)
                                                onObjectRemoved: (index, object) => chipMenu.removeItem(object)
                                            }
                                            MenuSeparator {}
                                            MenuItem {
                                                text: qsTr("Stop plotting")
                                                onTriggered: {
                                                    const [message, signal] = chip.modelData.split(".")
                                                    root.connection.untrack_signal(message, signal)
                                                }
                                            }
                                        }
                                    }
                                }
                                Item { Layout.fillWidth: true }
                                CheckBox {
                                    visible: chart.modelData.length > 1
                                    text: qsTr("Separate scales")
                                    checked: plot.separate_scales
                                    onToggled: root.view.set_separate_scales(chart.index, checked)
                                }
                            }

                            SignalPlot {
                                id: plot
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                view: root.view
                                keys: chart.modelData
                                separate_scales: root.view.charts && root.view.separate_scales(chart.index)
                                palette: root.colors
                                text_color: Theme.muted
                                grid_color: Theme.border
                                cursor_color: Theme.warning

                                // Wheel: zoom around the mouse; drag: pan; click: cursor 1, Shift+click or right
                                // click: cursor 2; double click: back to live
                                WheelHandler {
                                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                                    onWheel: event => root.view.zoom(event.angleDelta.y > 0 ? 0.8 : 1.25, plot.time_at(point.position.x))
                                }
                                DragHandler {
                                    id: panHandler
                                    target: null
                                    property real lastX: 0
                                    onActiveChanged: lastX = centroid.position.x
                                    onCentroidChanged: {
                                        if (!active)
                                            return
                                        const dx = centroid.position.x - lastX
                                        lastX = centroid.position.x
                                        root.view.pan(-dx * plot.seconds_per_pixel())
                                    }
                                }
                                TapHandler {
                                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                                    onTapped: (eventPoint, button) => root.view.set_cursor(
                                        button === Qt.RightButton || (point.modifiers & Qt.ShiftModifier) ? 1 : 0,
                                        plot.time_at(eventPoint.position.x))
                                    onDoubleTapped: root.view.follow_live()
                                }
                                HoverHandler {
                                    cursorShape: panHandler.active ? Qt.ClosedHandCursor : Qt.CrossCursor
                                }
                            }
                        }
                    }
                }
            }
        }

        // Values at the cursors and statistics between them
        Card {
            visible: root.connection.tracked_signals.length > 0
            padding: 10
            Layout.fillWidth: true
            Layout.preferredHeight: 76 + root.view.readout.length * 24

            ColumnLayout {
            anchors.fill: parent
            spacing: 6

            Label {
                text: root.view.has_cursors
                    ? [root.view.cursor_times.c1 ? `C1 ${root.view.cursor_times.c1} s` : "",
                       root.view.cursor_times.c2 ? `C2 ${root.view.cursor_times.c2} s` : "",
                       root.view.cursor_times.delta ? `Δt ${root.view.cursor_times.delta}` : ""].filter(t => t).join("     ")
                    : qsTr("Click a chart to set cursor C1, Shift+click or right click for C2; wheel zooms, dragging pans")
                color: root.view.has_cursors ? Theme.warning : Theme.muted
                font.pixelSize: 12
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 8
                columnSpacing: 16
                rowSpacing: 2

                Repeater {
                    model: [qsTr("Signal"), "C1", "C2", qsTr("Δ (C2 − C1)"),
                            root.view.cursor_times.delta ? qsTr("Min (C1–C2)") : qsTr("Min (visible)"),
                            qsTr("Max"), qsTr("Mean"), qsTr("Values")]
                    Label {
                        required property string modelData
                        text: modelData
                        color: Theme.muted
                        font.bold: true
                        font.pixelSize: 12
                    }
                }
                Repeater {
                    model: root.view.readout
                    delegate: Repeater {
                        id: readoutRow
                        required property var modelData
                        model: [
                            modelData.key + (modelData.unit ? ` [${modelData.unit}]` : ""),
                            root.format(modelData.c1), root.format(modelData.c2), root.format(modelData.delta),
                            root.format(modelData.minimum), root.format(modelData.maximum), root.format(modelData.mean),
                            String(modelData.count)
                        ]
                        Label {
                            required property string modelData
                            required property int index
                            text: modelData
                            color: index === 0 ? root.colors[readoutRow.modelData.color % root.colors.length] : Theme.text
                            font.family: index === 0 ? Qt.application.font.family : "monospace"
                            font.pixelSize: 12
                            horizontalAlignment: index === 0 ? Text.AlignLeft : Text.AlignRight
                            elide: index === 0 ? Text.ElideRight : Text.ElideNone
                            Layout.fillWidth: index === 0
                            Layout.minimumWidth: index === 0 ? 60 : implicitWidth
                        }
                    }
                }
            }
            }
        }
    }

    FileDialog {
        id: exportDialog
        title: qsTr("Export plotted signals")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTr("CSV files (*.csv)")]
        defaultSuffix: "csv"
        onAccepted: root.status = root.view.export_csv(selectedFile, !root.view.follow)
    }
}
