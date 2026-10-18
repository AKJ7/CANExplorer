import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15


ColumnLayout {
    id: root
    required property var connection
    spacing: 4

    // Table shown in the current tab, used by Edit > Copy / Select All
    readonly property var currentTable: viewTabs.currentIndex === 0 ? traceTable
        : viewTabs.currentIndex === 1 ? overviewTable : null

    function copySelection() {
        return currentTable ? currentTable.copySelection() : 0
    }

    function selectAll() {
        if (currentTable)
            currentTable.selectAll()
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.leftMargin: 4
        Layout.rightMargin: 4

        Button {
            text: root.connection.connected
                ? (root.connection.is_replay ? qsTr("Stop") : qsTr("Disconnect"))
                : (root.connection.is_replay ? qsTr("Replay") : qsTr("Connect"))
            highlighted: !root.connection.connected
            onClicked: root.connection.connected ? root.connection.close() : root.connection.open()
        }
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
            text: root.connection.recording ? qsTr("Stop recording") : qsTr("Record…")
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
            text: qsTr("Auto-scroll")
            checked: true
        }

        Item { Layout.fillWidth: true }

        TextField {
            id: filterField
            placeholderText: qsTr("Filter: 100-1FF !123 ext rx fd")
            Layout.preferredWidth: 260
            onTextChanged: root.connection.set_filter(text)
            color: root.connection.filter_error ? "tomato" : palette.text
            ToolTip.visible: hovered && root.connection.filter_error.length > 0
            ToolTip.text: root.connection.filter_error
        }
    }

    TabBar {
        id: viewTabs
        Layout.fillWidth: true
        TabButton { text: qsTr("Trace"); width: implicitWidth }
        TabButton { text: qsTr("Overview"); width: implicitWidth }
        TabButton { text: qsTr("Signals"); width: implicitWidth }
        TabButton {
            text: qsTr("Diagnostics")
            width: root.connection.can_transmit ? implicitWidth : 0
            visible: root.connection.can_transmit
        }
        TabButton {
            text: qsTr("LIN Schedule")
            width: root.connection.is_lin ? implicitWidth : 0
            visible: root.connection.is_lin
        }
        TabButton {
            text: qsTr("Simulation")
            width: root.connection.can_transmit ? implicitWidth : 0
            visible: root.connection.can_transmit
        }
    }

    StackLayout {
        currentIndex: viewTabs.currentIndex
        Layout.fillWidth: true
        Layout.fillHeight: true

        FrameTable {
            id: traceTable
            model: root.connection.trace
            columnWidths: [110, 40, 70, 90, 40, 200, 0]
            autoScroll: autoScrollBox.checked
        }
        FrameTable {
            id: overviewTable
            model: root.connection.overview
            columnWidths: [90, 40, 70, 80, 90, 40, 200, 0]
        }
        SignalsView {
            connection: root.connection
        }
        Loader {
            active: root.connection.can_transmit
            sourceComponent: DiagnosticsView {
                connection: root.connection
            }
        }
        Loader {
            active: root.connection.is_lin
            sourceComponent: LinScheduleView {
                connection: root.connection
            }
        }
        Loader {
            active: root.connection.can_transmit
            sourceComponent: SimulationView {
                connection: root.connection
            }
        }
    }

    TransmitPanel {
        connection: root.connection
        visible: root.connection.can_transmit && viewTabs.currentIndex < 2
        Layout.fillWidth: true
    }

    Label {
        id: statusLabel
        visible: text.length > 0
        padding: 2
        Layout.leftMargin: 4
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
