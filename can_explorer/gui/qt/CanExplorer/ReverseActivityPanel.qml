import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0
import CanExplorer.Plots 1.0

// Bit-transition raster: the value of every bit of the selected identifier across the whole capture, so counters,
// constants, toggles and multiplexed regions stand out at a glance. Frames across, bits down (byte 0 at the top).
ColumnLayout {
    id: root
    required property var tools
    readonly property var raster: tools.raster

    signal fieldUsed()  // Part of the common panel interface; the activity map does not use them
    signal plotted()
    signal message(string text)

    spacing: 8

    onVisibleChanged: if (visible && tools.selected !== "") raster.refresh()

    Connections {
        target: root.tools
        function onSelection_changed() { if (root.visible && root.tools.selected !== "") root.raster.refresh() }
    }

    RowLayout {
        spacing: 10
        Layout.fillWidth: true

        Button {
            text: qsTr("Refresh")
            enabled: !root.raster.running && root.tools.selected !== ""
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Rebuild the activity map from the frames captured so far")
            onClicked: root.raster.refresh()
        }
        BusyIndicator {
            running: root.raster.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        // Legend
        Repeater {
            model: [
                { c: Theme.cardAlt, t: qsTr("0") },
                { c: Theme.accent, t: qsTr("1") },
                { c: Theme.warning, t: qsTr("changed") }
            ]
            RowLayout {
                required property var modelData
                spacing: 4
                Rectangle { width: 12; height: 12; radius: 2; color: modelData.c }
                Label { text: modelData.t; color: Theme.muted; font.pixelSize: 12 }
            }
        }
        Item { Layout.fillWidth: true }
        Label {
            text: root.raster.has_data ? qsTr("%1 over %2").arg(root.raster.status).arg(root.raster.span) : root.raster.status
            color: Theme.muted
            font.pixelSize: 12
            elide: Text.ElideRight
        }
    }

    Card {
        Layout.fillWidth: true
        Layout.fillHeight: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 6

            Label {
                visible: root.tools.selected === ""
                text: qsTr("Select an identifier to see its bits over the whole capture")
                color: Theme.muted
                Layout.fillWidth: true
            }
            BitRaster {
                id: rasterItem
                visible: root.tools.selected !== ""
                controller: root.raster
                colors: [Theme.cardAlt, Theme.accent, Qt.darker(Theme.warning, 2.4), Theme.warning]
                gridColor: Theme.alpha(Theme.border, 0.8)
                textColor: Theme.muted
                Layout.fillWidth: true
                Layout.fillHeight: true
            }
            Label {
                text: qsTr("Each row is a bit (byte 0 at the top, most significant bit first), each column a frame over time. Solid blue is a steady 1, dark a steady 0; orange marks a bit that changed. Counters ramp as diagonal stripes, constants are flat, multiplexers switch.")
                color: Theme.muted
                font.pixelSize: 11
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
    }
}
