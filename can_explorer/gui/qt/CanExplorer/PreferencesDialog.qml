import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Dialog {
    id: dialog
    title: qsTr("Preferences")
    modal: true
    anchors.centerIn: parent
    standardButtons: Dialog.Ok | Dialog.Cancel
    readonly property var appState: connectionManager.app_state

    onAboutToShow: {
        rowsBox.value = appState.max_trace_rows
        refreshBox.value = appState.refresh_interval_ms
    }
    onAccepted: appState.set_preferences(rowsBox.value, refreshBox.value)

    GridLayout {
        columns: 2

        Label { text: qsTr("Trace buffer (frames)") }
        SpinBox {
            id: rowsBox
            from: dialog.appState.min_trace_rows
            to: dialog.appState.max_trace_rows_limit
            stepSize: 10000
            editable: true
            Layout.preferredWidth: 180
        }
        Label {
            text: qsTr("Applies to connections opened afterwards.")
            font.italic: true
            Layout.columnSpan: 2
        }

        Label { text: qsTr("View refresh interval (ms)") }
        SpinBox {
            id: refreshBox
            from: 16
            to: 1000
            stepSize: 10
            editable: true
            Layout.preferredWidth: 180
        }

        Button {
            text: qsTr("Clear recent connections and files")
            Layout.columnSpan: 2
            onClicked: dialog.appState.clear_recent()
        }
    }
}
