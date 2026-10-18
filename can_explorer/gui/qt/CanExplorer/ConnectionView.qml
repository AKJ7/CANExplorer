import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0


ColumnLayout {
    id: root
    required property var connection
    spacing: Theme.spacing

    // Views shown in the sidebar; `page` is the key of the current one
    readonly property var pages: [
        { key: "dashboard", title: qsTr("Dashboard"), icon: "▦" },
        { key: "trace", title: qsTr("Trace"), icon: "☰" },
        { key: "overview", title: qsTr("Overview"), icon: "▤" },
        { key: "signals", title: qsTr("Signals"), icon: "∿" },
    ].concat(connection.can_transmit ? [{ key: "diagnostics", title: qsTr("Diagnostics"), icon: "✚" }] : [])
     .concat(connection.is_lin ? [{ key: "lin", title: qsTr("LIN Schedule"), icon: "⏱" }] : [])
     .concat(connection.can_transmit ? [{ key: "simulation", title: qsTr("Simulation"), icon: "▶" }] : [])
    readonly property var pageKeys: ["dashboard", "trace", "overview", "signals", "diagnostics", "lin", "simulation"]
    property string page: "dashboard"
    readonly property string pageTitle: (pages.find(item => item.key === page) || pages[0]).title
    readonly property bool tablePage: page === "trace" || page === "overview"

    // Table shown on the current page, used by Copy / Select All
    // Bytes that differ from the previous frame of an identifier are shown in this colour in the overview
    Binding {
        target: root.connection.overview
        property: "highlight_color"
        value: String(Theme.warning)
    }

    readonly property var currentTable: page === "trace" ? traceTable : page === "overview" ? overviewTable : null

    function copySelection() {
        return currentTable ? currentTable.copySelection() : 0
    }

    function selectAll() {
        if (currentTable)
            currentTable.selectAll()
    }

    RowLayout {
        visible: root.tablePage || root.page === "signals"
        spacing: 8
        Layout.fillWidth: true

        Button {
            text: qsTr("Pause view")
            checkable: true
            checked: root.connection.paused
            onToggled: root.connection.set_paused(checked)
        }
        Button {
            text: qsTr("Clear")
            onClicked: root.connection.clear()
        }
        Button {
            text: root.connection.recording ? qsTr("● Stop recording") : qsTr("Record…")
            highlighted: root.connection.recording
            onClicked: root.connection.recording ? root.connection.stop_recording() : recordDialog.open()
        }
        Button {
            text: qsTr("Export…")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Save the selected trace rows, or all displayed rows, to a log file")
            onClicked: exportDialog.open()
        }
        Button {
            text: root.connection.database_file ? qsTr("Unload database") : qsTr("Load database…")
            onClicked: root.connection.database_file ? root.connection.unload_database() : databaseDialog.open()
        }
        ComboBox {
            id: busBox
            visible: root.connection.database_buses.length > 1
            model: [qsTr("All buses")].concat(root.connection.database_buses)
            currentIndex: Math.max(0, root.connection.database_buses.indexOf(root.connection.database_bus) + 1)
            onActivated: index => root.connection.set_database_bus(index === 0 ? "" : currentText)
            ToolTip.visible: hovered
            ToolTip.text: qsTr("The database describes several buses; decode with the messages of this one")
        }
        CheckBox {
            id: autoScrollBox
            visible: root.page === "trace"
            text: qsTr("Auto-scroll")
            checked: true
        }

        Item { Layout.fillWidth: true }

        Pill {
            visible: root.connection.database_file.length > 0
            text: root.connection.database_file.split(/[\\/]/).pop()
            tint: Theme.chart[4]
        }
        Pill {
            visible: root.connection.protocol !== "Raw"
            text: root.connection.protocol
            tint: Theme.chart[5]
        }
        Pill {
            text: qsTr("%1 frames").arg(root.connection.frame_count.toLocaleString(Qt.locale(), "f", 0))
            tint: Theme.accent
        }
    }

    StackLayout {
        currentIndex: root.pageKeys.indexOf(root.page)
        Layout.fillWidth: true
        Layout.fillHeight: true

        DashboardView {
            connection: root.connection
        }
        Card {
            padding: 1
            FrameTable {
                id: traceTable
                anchors.fill: parent
                model: root.connection.trace
                columnWidths: [110, 40, 70, 90, 40, 200, 0]
                autoScroll: autoScrollBox.checked
            }
        }
        Card {
            padding: 1
            FrameTable {
                id: overviewTable
                anchors.fill: parent
                model: root.connection.overview
                columnWidths: [90, 40, 70, 80, 90, 40, 200, 0]
                sortable: true
            }
        }
        Card {
            SignalsView {
                anchors.fill: parent
                connection: root.connection
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: DiagnosticsView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.is_lin
                sourceComponent: LinScheduleView {
                    connection: root.connection
                }
            }
        }
        Card {
            Loader {
                anchors.fill: parent
                active: root.connection.can_transmit
                sourceComponent: SimulationView {
                    connection: root.connection
                }
            }
        }
    }

    TransmitPanel {
        connection: root.connection
        visible: root.connection.can_transmit && root.tablePage
        Layout.fillWidth: true
        Layout.preferredHeight: implicitHeight
    }

    Label {
        id: statusLabel
        visible: text.length > 0
        color: Theme.muted
        Timer {
            id: statusTimer
            interval: 4000
            onTriggered: statusLabel.text = ""
        }
        function show(message) {
            text = message
            statusTimer.restart()
        }
    }

    FileDialog {
        id: recordDialog
        title: qsTr("Record to file")
        fileMode: FileDialog.SaveFile
        nameFilters: connectionManager.log_file_filters.slice(1)
        defaultSuffix: "asc"
        onAccepted: root.connection.start_recording(selectedFile)
    }

    FileDialog {
        id: exportDialog
        title: qsTr("Export trace")
        fileMode: FileDialog.SaveFile
        nameFilters: connectionManager.log_file_filters.slice(1)
        defaultSuffix: "asc"
        onAccepted: {
            const selection = traceTable.selectionModel.hasSelection ? traceTable.selectionModel : null
            statusLabel.show(root.connection.export_trace(selectedFile, selection))
        }
    }

    FileDialog {
        id: databaseDialog
        title: qsTr("Load signal database")
        fileMode: FileDialog.OpenFile
        nameFilters: connectionManager.database_file_filters
        onAccepted: root.connection.load_database(selectedFile)
    }
}
