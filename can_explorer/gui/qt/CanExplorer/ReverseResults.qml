import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Result table of the reverse engineering tools: one row per field with Use and Plot buttons.
ColumnLayout {
    id: root
    property var rows: []
    // { title, role, width (0: fills the rest), mono, optional (hidden while empty in every row) }
    property var columns: []
    readonly property var shownColumns: columns.filter(
        column => !column.optional || rows.some(row => row[column.role] !== "" && row[column.role] !== undefined))
    property string emptyText: ""
    // Text colour of a cell
    property var cellColor: (row, role) => Theme.text
    // Without a column filling the rest of the width, an empty item does (so the columns are not stretched)
    readonly property bool needsFiller: !shownColumns.some(column => !column.width)

    signal useRow(int index)
    signal plotRow(int index)

    spacing: 4

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Layout.leftMargin: 8
        Layout.rightMargin: 8
        Repeater {
            model: root.shownColumns
            Label {
                required property var modelData
                text: modelData.title
                color: Theme.muted
                font.bold: true
                font.pixelSize: 12
                elide: Text.ElideRight
                Layout.preferredWidth: modelData.width || -1
                Layout.fillWidth: !modelData.width
            }
        }
        Item { visible: root.needsFiller; Layout.fillWidth: true }
        Item { Layout.preferredWidth: actionsWidth.implicitWidth }
    }

    ListView {
        id: list
        clip: true
        model: root.rows
        Layout.fillWidth: true
        Layout.fillHeight: true
        ScrollBar.vertical: ScrollBar {}

        Label {
            visible: list.count === 0 && root.emptyText !== ""
            width: list.width
            text: root.emptyText
            color: Theme.muted
            wrapMode: Text.Wrap
            padding: 8
        }

        delegate: Rectangle {
            id: rowItem
            required property var modelData
            required property int index
            width: list.width
            height: 34
            color: index % 2 ? Theme.alpha(Theme.cardAlt, 0.5) : "transparent"
            radius: 4

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 8
                anchors.rightMargin: 8
                spacing: 8
                Repeater {
                    model: root.shownColumns
                    Label {
                        required property var modelData
                        text: String(rowItem.modelData[modelData.role] === undefined ? "" : rowItem.modelData[modelData.role])
                        color: root.cellColor(rowItem.modelData, modelData.role)
                        font.family: modelData.mono ? "monospace" : Qt.application.font.family
                        font.pixelSize: 12
                        elide: Text.ElideRight
                        Layout.preferredWidth: modelData.width || -1
                        Layout.fillWidth: !modelData.width
                        ToolTip.visible: truncated && cellArea.containsMouse
                        ToolTip.text: text
                        MouseArea {
                            id: cellArea
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.NoButton
                        }
                    }
                }
                Item { visible: root.needsFiller; Layout.fillWidth: true }
                RowLayout {
                    spacing: 0
                    opacity: rowItem.modelData.key !== "" ? 1 : 0
                    enabled: rowItem.modelData.key !== ""
                    ToolButton {
                        text: qsTr("Use")
                        font.pixelSize: 12
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Show the field in the bit grid and the bit field form")
                        onClicked: root.useRow(rowItem.modelData.index)
                    }
                    ToolButton {
                        text: qsTr("Plot")
                        font.pixelSize: 12
                        onClicked: root.plotRow(rowItem.modelData.index)
                    }
                }
            }
        }
    }

    // Width of the buttons, for the header
    RowLayout {
        id: actionsWidth
        visible: false
        spacing: 0
        ToolButton { text: qsTr("Use"); font.pixelSize: 12 }
        ToolButton { text: qsTr("Plot"); font.pixelSize: 12 }
    }
}
