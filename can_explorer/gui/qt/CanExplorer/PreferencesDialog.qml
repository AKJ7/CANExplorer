import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


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
        themeBox.currentIndex = appState.themes.indexOf(appState.theme)
        languageBox.currentIndex = Math.max(0, languageBox.model.findIndex(item => item.code === appState.language))
        historyBox.value = appState.trace_history
        directoryField.text = appState.trace_directory
    }
    onAccepted: {
        appState.set_preferences(rowsBox.value, refreshBox.value)
        appState.set_trace_history(historyBox.value, directoryField.text)
        appState.set_theme(themeBox.currentText)
        appState.set_language(languageBox.currentValue)
    }

    GridLayout {
        columns: 2

        Label { text: qsTr("Theme") }
        ComboBox {
            id: themeBox
            model: dialog.appState.themes
            Layout.preferredWidth: 180
        }

        Label { text: qsTr("Language") }
        ComboBox {
            id: languageBox
            model: [{ code: "", name: qsTr("System language") }].concat(dialog.appState.languages)
            textRole: "name"
            valueRole: "code"
            Layout.preferredWidth: 180
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Applies at once to the whole interface; texts without a translation stay in English")
        }

        Label { text: qsTr("Trace in memory (frames)") }
        SpinBox {
            id: rowsBox
            from: dialog.appState.min_trace_rows
            to: dialog.appState.max_trace_rows_limit
            stepSize: 10000
            editable: true
            Layout.preferredWidth: 180
        }
        Label { text: qsTr("Trace history (frames)") }
        SpinBox {
            id: historyBox
            from: 0
            to: dialog.appState.max_trace_history
            stepSize: 1000000
            editable: true
            Layout.preferredWidth: 180
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Frames kept in total; those beyond the memory limit go to temporary files on disk. 0: memory only.")
        }
        Label { text: qsTr("Trace history directory") }
        TextField {
            id: directoryField
            Layout.preferredWidth: 320
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Where the temporary files go; a directory on disk, not in memory (often /tmp)")
        }
        Label {
            text: qsTr("About %1 GB of disk at most. Trace settings apply to connections opened afterwards.")
                .arg((Math.max(0, historyBox.value - rowsBox.value) * 160 / 1e9).toFixed(1))
            font.italic: true
            wrapMode: Text.Wrap
            Layout.columnSpan: 2
            Layout.preferredWidth: 480  // Wrapped, so the dialog's width does not depend on the font
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

        Label { text: qsTr("Updates") }
        RowLayout {
            CheckBox {
                text: qsTr("Check at start")
                checked: connectionManager.updates.check_at_start
                onToggled: connectionManager.updates.set_check_at_start(checked)
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Look for new releases on GitHub at most once a day")
            }
            CheckBox {
                text: qsTr("Pre-releases")
                checked: connectionManager.updates.include_prereleases
                onToggled: connectionManager.updates.set_include_prereleases(checked)
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Also offer release candidates and other pre-releases")
            }
            Button {
                text: qsTr("Check now")
                onClicked: {
                    dialog.close()
                    Actions.checkForUpdates.trigger()
                }
            }
        }

        Button {
            text: qsTr("Clear recent connections and files")
            Layout.columnSpan: 2
            onClicked: dialog.appState.clear_recent()
        }
    }
}
