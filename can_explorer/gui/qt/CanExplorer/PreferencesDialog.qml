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

    // The themes offered for the light and the dark mode of "System": those with a light or dark background
    readonly property var lightThemes: appState.themes.filter(name => name !== "System" && !Theme.isDark(name))
    readonly property var darkThemes: appState.themes.filter(name => name !== "System" && Theme.isDark(name))

    function clearPreview() {
        Theme.preview = ""
        Theme.previewLight = ""
        Theme.previewDark = ""
    }

    onAboutToShow: {
        clearPreview()
        rowsBox.value = appState.max_trace_rows
        refreshBox.value = appState.refresh_interval_ms
        themeBox.select(appState.theme)
        systemLightBox.select(appState.system_light_theme)
        systemDarkBox.select(appState.system_dark_theme)
        languageBox.currentIndex = Math.max(0, languageBox.model.findIndex(item => item.code === appState.language))
        historyBox.value = appState.trace_history
        directoryField.text = appState.trace_directory
        mapTilesBox.checked = appState.map_tiles
        tileUrlField.text = appState.map_tile_url
    }
    onAccepted: {
        appState.set_preferences(rowsBox.value, refreshBox.value)
        appState.set_trace_history(historyBox.value, directoryField.text)
        appState.set_map_tiles(mapTilesBox.checked, tileUrlField.text)
        appState.set_system_themes(systemLightBox.currentText, systemDarkBox.currentText)
        appState.set_theme(themeBox.currentText)
        appState.set_language(languageBox.currentValue)
    }
    // The themes picked in the lists are only previewed: OK keeps them (onAccepted), Cancel or Escape goes back
    onClosed: clearPreview()

    GridLayout {
        columns: 2

        Label { text: qsTr("Theme") }
        RowLayout {
            spacing: 8
            ThemeComboBox {
                id: themeBox
                objectName: "themeBox"
                model: dialog.appState.themes
                onActivated: Theme.preview = currentText
                ToolTip.visible: hovered && !popup.visible
                ToolTip.text: qsTr("Previewed at once; OK keeps it, Cancel goes back. System follows the light or dark mode of the operating system")
            }
            ThemeSwatches { themeName: themeBox.currentText }
        }

        // The themes of System, for the light and the dark mode of the operating system
        Label {
            visible: themeBox.currentText === "System"
            text: qsTr("In light mode")
            Layout.leftMargin: 16
        }
        ThemeComboBox {
            id: systemLightBox
            objectName: "systemLightBox"
            visible: themeBox.currentText === "System"
            model: dialog.lightThemes
            onActivated: Theme.previewLight = currentText
            ToolTip.visible: hovered && !popup.visible
            ToolTip.text: qsTr("The theme shown while the operating system is in light mode")
        }
        Label {
            visible: themeBox.currentText === "System"
            text: qsTr("In dark mode")
            Layout.leftMargin: 16
        }
        ThemeComboBox {
            id: systemDarkBox
            objectName: "systemDarkBox"
            visible: themeBox.currentText === "System"
            model: dialog.darkThemes
            onActivated: Theme.previewDark = currentText
            ToolTip.visible: hovered && !popup.visible
            ToolTip.text: qsTr("The theme shown while the operating system is in dark mode")
        }

        Label { text: qsTr("Language") }
        ComboBox {
            id: languageBox
            objectName: "languageBox"
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

        Label { text: qsTr("GPS map") }
        CheckBox {
            id: mapTilesBox
            text: qsTr("Show map tiles")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Download map tiles for the GPS track in Video & map. The tile server sees the map area shown.")
        }
        Label { text: qsTr("Map tile URL") }
        RowLayout {
            enabled: mapTilesBox.checked
            TextField {
                id: tileUrlField
                Layout.preferredWidth: 320
                ToolTip.visible: hovered
                ToolTip.text: qsTr("A tile server's URL with {z}, {x} and {y}; follow its usage policy")
            }
            Button {
                text: qsTr("Default")
                onClicked: tileUrlField.text = dialog.appState.default_map_tile_url
            }
        }

        Label { text: qsTr("Updates") }
        RowLayout {
            CheckBox {
                text: qsTr("Check at start")
                checked: connectionManager.updates.check_at_start
                onToggled: connectionManager.updates.set_check_at_start(checked)
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Look for new releases on GitHub each time CANExplorer starts")
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
