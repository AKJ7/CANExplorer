import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Event markers: mark the moments something happens, then find the bits changing at them.
ColumnLayout {
    id: root
    required property var tools
    readonly property var markers: tools.markers

    signal fieldUsed()
    signal plotted()
    signal message(string text)

    spacing: 8

    function mark() {
        const error = root.markers.add(labelField.text)
        if (error)
            root.message(error)
    }

    Shortcut {
        sequence: "Ctrl+M"
        enabled: root.visible
        onActivated: root.mark()
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        TextField {
            id: labelField
            placeholderText: qsTr("Label, e.g. door opened")
            Layout.preferredWidth: 220
            onAccepted: root.mark()
        }
        Button {
            text: qsTr("Mark now")
            highlighted: true
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Mark this moment (Ctrl+M) each time you do the action, e.g. five times opening the door")
            onClicked: root.mark()
        }
        Button {
            text: qsTr("Clear markers")
            enabled: root.markers.markers.length > 0
            onClicked: root.markers.clear()
        }
        Item { Layout.fillWidth: true }
    }

    // Markers set so far
    Flow {
        spacing: 6
        visible: root.markers.markers.length > 0
        Layout.fillWidth: true
        Repeater {
            model: root.markers.markers
            Rectangle {
                required property var modelData
                implicitWidth: markerRow.implicitWidth + 12
                implicitHeight: 28
                radius: 14
                color: Theme.alpha(Theme.accent, 0.14)
                RowLayout {
                    id: markerRow
                    anchors.centerIn: parent
                    spacing: 4
                    Label {
                        text: modelData.label ? `${modelData.time} · ${modelData.label}` : modelData.time
                        color: Theme.text
                        font.pixelSize: 12
                    }
                    ToolButton {
                        text: "×"
                        implicitWidth: 22
                        implicitHeight: 22
                        onClicked: root.markers.remove(modelData.index)
                    }
                }
            }
        }
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Label { text: qsTr("Markers") }
        ComboBox {
            id: labelBox
            model: [qsTr("All")].concat(root.markers.labels)
        }
        Label { text: qsTr("Seconds before") }
        TextField {
            id: beforeField
            text: "0.5"
            validator: DoubleValidator { bottom: 0; top: 60 }
            Layout.preferredWidth: 60
        }
        Label { text: qsTr("after") }
        TextField {
            id: afterField
            text: "1.5"
            validator: DoubleValidator { bottom: 0; top: 60 }
            Layout.preferredWidth: 60
        }
        Button {
            text: qsTr("Find changing bits")
            highlighted: true
            enabled: !root.markers.running && root.markers.markers.length > 0
            onClicked: root.markers.find(labelBox.currentIndex === 0 ? "" : labelBox.currentText,
                                         Number(beforeField.text) || 0, Number(afterField.text) || 1.5)
        }
        BusyIndicator {
            running: root.markers.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.markers.status
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    ReverseResults {
        rows: root.markers.results
        columns: [
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Field"), role: "key", width: 140, mono: true },
            { title: qsTr("Markers hit"), role: "hits", width: 90, mono: true },
            { title: qsTr("Otherwise"), role: "background", width: 90, mono: true },
            { title: qsTr("Score"), role: "score", width: 60, mono: true },
            { title: qsTr("Changes"), role: "transitions", width: 0, mono: true },
        ]
        emptyText: qsTr("Bits changing within the window around most markers, but rarely otherwise, are listed here")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => {
            root.markers.use(index)
            root.fieldUsed()
        }
        onPlotRow: index => {
            const error = root.markers.plot(index)
            if (error)
                root.message(error)
            else
                root.plotted()
        }
    }
}
