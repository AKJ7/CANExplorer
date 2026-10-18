import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0
import QtQml.Models 2.15


ColumnLayout {
    id: root
    required property var model
    // Width per column in pixels; 0 makes the column fill the remaining space
    required property var columnWidths
    property bool autoScroll: false
    readonly property alias selectionModel: selection

    function copySelection() {
        return root.model.copy_selection(selection)
    }

    function selectAll() {
        root.model.select_all(selection)
    }

    spacing: 0

    HorizontalHeaderView {
        id: header
        syncView: tableView
        clip: true
        Layout.fillWidth: true
        delegate: Rectangle {
            required property string display
            implicitHeight: 36
            implicitWidth: headerLabel.implicitWidth + 16
            color: Theme.cardAlt
            Label {
                id: headerLabel
                anchors.fill: parent
                leftPadding: 8
                text: parent.display
                color: Theme.muted
                font.bold: true
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: Theme.border
            }
        }
    }

    TableView {
        id: tableView
        model: root.model
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        selectionModel: ItemSelectionModel { id: selection }
        selectionBehavior: TableView.SelectRows
        selectionMode: TableView.ExtendedSelection
        Layout.fillWidth: true
        Layout.fillHeight: true
        ScrollBar.vertical: ScrollBar {}
        ScrollBar.horizontal: ScrollBar {}
        columnWidthProvider: function (column) {
            const width = root.columnWidths[column]
            if (width === 0) {
                const used = root.columnWidths.reduce((sum, value) => sum + value, 0)
                return Math.max(250, tableView.width - used)
            }
            return width
        }
        onWidthChanged: forceLayout()

        delegate: Rectangle {
            required property string display
            required property int textAlignment
            required property bool richText
            required property bool selected
            required property int row
            implicitHeight: label.implicitHeight + 4
            color: selected ? Theme.alpha(Theme.accent, 0.35) : row % 2 ? Theme.alpha(Theme.cardAlt, 0.6) : "transparent"

            Label {
                id: label
                anchors.fill: parent
                text: parent.display
                textFormat: parent.richText ? Text.StyledText : Text.PlainText
                color: Theme.text
                font.family: "monospace"
                elide: Text.ElideRight
                padding: 3
                leftPadding: 8
                horizontalAlignment: parent.textAlignment & Qt.AlignHorizontal_Mask
                verticalAlignment: Text.AlignVCenter
            }
        }
    }

    Connections {
        target: root.model
        function onRowsInserted() {
            if (root.autoScroll && !selection.hasSelection)
                tableView.positionViewAtRow(tableView.rows - 1, TableView.AlignBottom)
        }
    }
}
