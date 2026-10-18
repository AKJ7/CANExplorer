import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


ColumnLayout {
    id: root
    required property var connection

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label {
            text: root.connection.schedule_tables.length
                ? qsTr("Schedule table")
                : qsTr("Load an LDF file (Load database…) to run schedule tables as LIN master")
        }
        ComboBox {
            id: tableBox
            visible: root.connection.schedule_tables.length > 0
            model: root.connection.schedule_tables
            Layout.preferredWidth: 200
        }
        Button {
            visible: root.connection.schedule_tables.length > 0
            text: root.connection.schedule_running ? qsTr("Stop") : qsTr("Start")
            highlighted: !root.connection.schedule_running
            enabled: root.connection.connected
            onClicked: errorLabel.text = root.connection.schedule_running
                ? (root.connection.stop_schedule(), "")
                : root.connection.start_schedule(tableBox.currentText)
        }
        Label {
            visible: root.connection.schedule_running
            text: qsTr("running %1").arg(root.connection.schedule_table)
            color: Theme.success
        }
        Item { Layout.fillWidth: true }
        Label {
            id: errorLabel
            color: Theme.danger
            visible: text.length > 0
        }
    }

    Label {
        visible: root.connection.master_signals.length > 0
        text: qsTr("Signals published by the master (number, 0x hex or logical value name):")
        padding: 4
    }

    ListView {
        id: signalList
        clip: true
        Layout.fillWidth: true
        Layout.fillHeight: true
        ScrollBar.vertical: ScrollBar {}
        model: root.connection.master_signals
        delegate: RowLayout {
            required property var modelData
            width: signalList.width
            spacing: 8

            Label {
                text: `${modelData.frame}.${modelData.signal}`
                Layout.preferredWidth: 260
                leftPadding: 8
            }
            TextField {
                text: modelData.value
                Layout.preferredWidth: 140
                onEditingFinished: errorLabel.text = root.connection.set_master_signal(modelData.signal, text)
            }
            Label { text: modelData.unit }
            Item { Layout.fillWidth: true }
        }
    }
}
