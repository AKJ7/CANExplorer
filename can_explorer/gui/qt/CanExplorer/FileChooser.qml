import QtQuick 2.15

// Stand-in for QtQuick.Dialogs' FileDialog, shown by the `filePicker` context property (can_explorer/gui/file_picker.py)
// with QtWidgets' QFileDialog. Qt Quick's own non-native FileDialog crashes when its QML implementation cannot load,
// which is the case with the PyQt6 wheels on desktops without a native file dialog.
QtObject {
    id: chooser

    enum FileMode {
        OpenFile,
        SaveFile
    }

    property string title
    property int fileMode: FileChooser.OpenFile
    property var nameFilters: []
    // Appended to a typed name without suffix when the chosen filter has no extension of its own
    property string defaultSuffix
    property url selectedFile

    signal accepted()
    signal rejected()

    function open() {
        const filters = Array.from(nameFilters || [])
        const file = fileMode === FileChooser.SaveFile
            ? filePicker.save_file(title, filters, defaultSuffix)
            : filePicker.open_file(title, filters)
        if (file.toString() === "") {
            rejected()
            return
        }
        selectedFile = file
        accepted()
    }
}
