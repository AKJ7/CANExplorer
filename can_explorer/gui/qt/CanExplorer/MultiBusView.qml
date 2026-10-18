import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Several connections in one trace, ordered by time: e.g. the CAN and LIN side of a gateway.
ColumnLayout {
    id: root
    readonly property var multiBus: connectionManager.multi_bus
    readonly property string pageTitle: multiBus.selected.join(" + ")

    spacing: Theme.spacing

    function copySelection() {
        return table.copySelection()
    }

    function selectAll() {
        table.selectAll()
    }

    Card {
        Layout.fillWidth: true
        Layout.preferredHeight: busFlow.implicitHeight + 28
        padding: 14

        Flow {
            id: busFlow
            anchors.fill: parent
            spacing: 8

            Label {
                text: root.multiBus.connections.length ? qsTr("Buses") : qsTr("Open or create connections to show them together here.")
                color: Theme.muted
                height: 32
                verticalAlignment: Text.AlignVCenter
            }
            Repeater {
                model: root.multiBus.connections
                CheckBox {
                    required property var modelData
                    text: modelData.name
                    checked: modelData.selected
                    ToolTip.visible: hovered
                    ToolTip.text: modelData.description
                    onToggled: root.multiBus.set_selected(modelData.name, checked)
                }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Button {
            text: root.multiBus.paused ? qsTr("Resume") : qsTr("Pause")
            onClicked: root.multiBus.set_paused(!root.multiBus.paused)
        }
        Button {
            text: qsTr("Clear")
            onClicked: root.multiBus.clear()
        }
        CheckBox {
            id: autoScrollBox
            text: qsTr("Auto-scroll")
            checked: true
        }
        TextField {
            placeholderText: qsTr("Filter, e.g. 100-1FF !123 ext rx")
            text: root.multiBus.filter_text
            Layout.preferredWidth: 280
            onAccepted: root.multiBus.set_filter(text)
            onEditingFinished: root.multiBus.set_filter(text)
        }
        Label {
            text: root.multiBus.filter_error
            color: Theme.danger
            visible: text.length > 0
            elide: Text.ElideRight
            Layout.maximumWidth: 300
        }
        Item { Layout.fillWidth: true }
        Label {
            text: qsTr("%1 frames").arg(root.multiBus.frame_count)
            color: Theme.muted
        }
    }

    Card {
        padding: 1
        Layout.fillWidth: true
        Layout.fillHeight: true

        FrameTable {
            id: table
            anchors.fill: parent
            model: root.multiBus.trace
            columnWidths: [110, 110, 40, 70, 90, 40, 200, 0]
            autoScroll: autoScrollBox.checked && !root.multiBus.paused
        }
    }
}
