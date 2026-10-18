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
    // Clicking a column header calls model.sort_by(column)
    property bool sortable: false
    readonly property alias selectionModel: selection
    readonly property int currentRow: selection.hasSelection ? selection.currentIndex.row : -1

    // Right click on a row (selected first, when it was not)
    signal contextMenuRequested(int row)

    // Select a row and scroll to it, e.g. for a search result or a bookmark
    function showRow(row) {
        if (row < 0 || row >= tableView.rows)
            return
        const index = root.model.index(row, 0)
        selection.setCurrentIndex(index, ItemSelectionModel.ClearAndSelect | ItemSelectionModel.Rows)
        tableView.positionViewAtRow(row, TableView.Contain)
    }

    function copySelection() {
        return root.model.copy_selection(selection)
    }

    function selectAll() {
        root.model.select_all(selection)
    }

    readonly property int minimumColumnWidth: 30

    // Width of a column: as dragged by the user, else as configured; the filling column takes the rest
    function columnWidth(column) {
        const explicit = tableView.explicitColumnWidth(column)
        if (explicit >= 0)
            return explicit
        const width = root.columnWidths[column]
        if (width !== 0)
            return width
        let used = 0
        for (let other = 0; other < root.columnWidths.length; ++other) {
            if (other !== column) {
                const dragged = tableView.explicitColumnWidth(other)
                used += dragged >= 0 ? dragged : root.columnWidths[other]
            }
        }
        return Math.max(250, tableView.width - used)
    }

    function setColumnWidth(column, width) {
        tableView.setColumnWidth(column, Math.max(root.minimumColumnWidth, Math.round(width)))
    }

    // Back to the configured widths
    function resetColumnWidths() {
        tableView.clearColumnWidths()
    }

    spacing: 0

    HorizontalHeaderView {
        id: header
        syncView: tableView
        clip: true
        Layout.fillWidth: true
        delegate: Rectangle {
            id: headerCell
            required property string display
            required property int column
            readonly property bool sorted: /[▲▼]$/.test(display)
            implicitHeight: 36
            implicitWidth: headerLabel.implicitWidth + 16
            color: root.sortable && headerHover.hovered ? Qt.lighter(Theme.cardAlt, Theme.dark ? 1.25 : 0.97) : Theme.cardAlt
            Label {
                id: headerLabel
                anchors.fill: parent
                leftPadding: 8
                text: parent.display
                color: headerCell.sorted ? Theme.accent : Theme.muted
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
            HoverHandler {
                id: headerHover
                enabled: root.sortable
                cursorShape: Qt.PointingHandCursor
            }
            TapHandler {
                enabled: root.sortable
                onTapped: point => {
                    if (point.position.x < headerCell.width - resizeHandle.width)  // Not on the resize handle
                        root.model.sort_by(headerCell.column)
                }
            }

            // Drag the right edge to resize the column; a double click restores its width
            Item {
                id: resizeHandle
                objectName: "columnResizeHandle"
                anchors.right: parent.right
                width: 8
                height: parent.height
                z: 1
                property real startWidth: 0
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.verticalCenter: parent.verticalCenter
                    width: 1
                    height: parent.height * 0.5
                    color: resizeHover.hovered || resizeDrag.active ? Theme.accent : Theme.border
                }
                HoverHandler {
                    id: resizeHover
                    cursorShape: Qt.SplitHCursor
                }
                DragHandler {
                    id: resizeDrag
                    target: null
                    xAxis.enabled: true
                    yAxis.enabled: false
                    cursorShape: Qt.SplitHCursor
                    onActiveChanged: {
                        if (active)
                            resizeHandle.startWidth = root.columnWidth(headerCell.column)  // Also without rows
                    }
                    onTranslationChanged: {
                        if (active)
                            root.setColumnWidth(headerCell.column, resizeHandle.startWidth + translation.x)
                    }
                }
                TapHandler {
                    onDoubleTapped: tableView.setColumnWidth(headerCell.column, -1)
                }
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
        columnWidthProvider: column => root.columnWidth(column)
        onWidthChanged: forceLayout()

        delegate: Rectangle {
            required property string display
            required property int textAlignment
            required property bool richText
            required property bool selected
            required property bool bookmarked
            required property int row
            required property int column
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

            // Bookmark marker at the start of the row
            Rectangle {
                visible: parent.bookmarked && parent.column === 0
                width: 4
                height: parent.height
                color: Theme.warning
            }

            TapHandler {
                acceptedButtons: Qt.RightButton
                onTapped: {
                    if (!parent.selected)
                        root.showRow(parent.row)
                    root.contextMenuRequested(parent.row)
                }
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
