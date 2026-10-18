import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15


ColumnLayout {
    id: root
    required property var connection
    spacing: 4

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
            text: root.connection.database_file ? qsTr("Unload database") : qsTr("Load database…")
            onClicked: root.connection.database_file ? root.connection.unload_database() : databaseDialog.open()
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
    }

    StackLayout {
        currentIndex: viewTabs.currentIndex
        Layout.fillWidth: true
        Layout.fillHeight: true

        FrameTable {
            model: root.connection.trace
            columnWidths: [110, 40, 70, 90, 40, 200, 0]
            autoScroll: autoScrollBox.checked
        }
        FrameTable {
            model: root.connection.overview
            columnWidths: [90, 40, 70, 80, 90, 40, 200, 0]
        }
    }

    TransmitPanel {
        connection: root.connection
        visible: root.connection.can_transmit
        Layout.fillWidth: true
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
        id: databaseDialog
        title: qsTr("Load signal database")
        fileMode: FileDialog.OpenFile
        nameFilters: connectionManager.database_file_filters
        onAccepted: root.connection.load_database(selectedFile)
    }
}
