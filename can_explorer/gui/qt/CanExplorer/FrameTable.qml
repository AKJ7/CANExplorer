import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


ColumnLayout {
    id: root
    required property var model
    // Width per column in pixels; 0 makes the column fill the remaining space
    required property var columnWidths
    property bool autoScroll: false

    spacing: 0

    HorizontalHeaderView {
        id: header
        syncView: tableView
        clip: true
        Layout.fillWidth: true
    }

    TableView {
        id: tableView
        model: root.model
        clip: true
        boundsBehavior: Flickable.StopAtBounds
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

        delegate: Label {
            required property string display
            required property int textAlignment
            required property bool richText
            text: display
            textFormat: richText ? Text.StyledText : Text.PlainText
            font.family: "monospace"
            elide: Text.ElideRight
            padding: 3
            horizontalAlignment: textAlignment & Qt.AlignHorizontal_Mask
            verticalAlignment: Text.AlignVCenter
        }
    }

    Connections {
        target: root.model
        function onRowsInserted() {
            if (root.autoScroll)
                tableView.positionViewAtRow(tableView.rows - 1, TableView.AlignBottom)
        }
    }
}
