import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Reverse engineering: the bits and bytes of each identifier, a notch to find bits that change on an action,
// and bit fields that can be plotted and added to a database.
SplitView {
    id: root
    required property var connection
    readonly property var tools: connection.reverse
    property int anchorBit: -1  // First bit of a selection in the grid; Shift+click selects up to another bit
    // A placeholder for a bit cell whose data is momentarily out of range (while the grid shrinks for a smaller
    // identifier), so its bindings never dereference undefined
    readonly property var emptyBit: ({ bit: 0, value: 0, masked: true, flips: 0, ones: 0, field: false, hit: false, recent: 0, age: -1 })
    property string status: ""

    signal showPage(string page)

    onVisibleChanged: tools.set_visible(visible)
    Component.onCompleted: tools.set_visible(visible)

    function showStatus(text) {
        status = text
        statusTimer.restart()
    }

    function percent(share) {
        return (share * 100).toFixed(share > 0 && share < 0.01 ? 2 : 0) + " %"
    }

    // Changing bits are tinted by how often they change, on a logarithmic scale (every 1000th frame: faint)
    function heat(flips) {
        return flips <= 0 ? 0 : Math.max(0.15, Math.min(1, (Math.log10(flips) + 3) / 3))
    }

    Timer {
        id: statusTimer
        interval: 4000
        onTriggered: root.status = ""
    }

    // Identifiers
    Card {
        SplitView.preferredWidth: 290
        SplitView.minimumWidth: 200
        padding: 10

        ColumnLayout {
            anchors.fill: parent
            spacing: 6

            TextField {
                id: idFilter
                placeholderText: qsTr("Search ID or name")
                Layout.fillWidth: true
                onTextChanged: root.tools.set_id_search(text)
            }
            CheckBox {
                id: hitsOnly
                visible: root.tools.notched
                text: qsTr("Only changed since the notch")
                onToggled: root.tools.set_hits_only(checked)
            }
            ListView {
                id: idList
                objectName: "reverseIdList"
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                // A real model updated in place: the list is not rebuilt on every refresh, so clicking an entry
                // while traffic flows always selects it and never destroys the delegate mid-click
                model: root.tools.identifier_model
                delegate: ItemDelegate {
                    id: idItem
                    required property var model
                    width: idList.width
                    highlighted: idItem.model.id === root.tools.selected
                    onClicked: root.tools.select(idItem.model.id)
                    contentItem: RowLayout {
                        spacing: 8
                        ColumnLayout {
                            spacing: 0
                            Layout.fillWidth: true
                            Label {
                                text: idItem.model.id
                                color: Theme.text
                                font.family: "monospace"
                                font.bold: true
                            }
                            Label {
                                text: [idItem.model.name, qsTr("%1 frames").arg(idItem.model.count),
                                       idItem.model.cycle].filter(t => t).join(" · ")
                                color: Theme.muted
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                        Pill {
                            visible: idItem.model.new
                            text: qsTr("new")
                            tint: Theme.success
                        }
                        Pill {
                            visible: idItem.model.hits > 0
                            text: idItem.model.hits === 1 ? qsTr("1 bit") : qsTr("%1 bits").arg(idItem.model.hits)
                            tint: Theme.warning
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Bits that changed since the notch")
                        }
                    }
                }
            }
            Label {
                visible: idList.count === 0
                text: root.tools.identifiers.length === 0 ? qsTr("No frames yet") : qsTr("No matching identifiers")
                color: Theme.muted
                padding: 8
            }
        }
    }

    ColumnLayout {
        SplitView.fillWidth: true
        spacing: 8

        TabBar {
            id: tabBar
            objectName: "reverseTabs"
            Layout.fillWidth: true
            TabButton { text: qsTr("Bits") }
            TabButton { text: qsTr("Automatic analysis") }
            TabButton { text: qsTr("Reference signals") }
            TabButton { text: qsTr("Markers") }
            TabButton { text: qsTr("Related signals") }
        }

        StackLayout {
            currentIndex: tabBar.currentIndex
            Layout.fillWidth: true
            Layout.fillHeight: true

            ScrollView {
                id: detailScroll
                contentWidth: availableWidth
                clip: true

                ColumnLayout {
                    width: detailScroll.availableWidth
                    spacing: Theme.spacing

                    RowLayout {
                        spacing: 8
                        Layout.fillWidth: true
                        Button {
                            text: root.tools.notched ? qsTr("Notch again") : qsTr("Notch")
                            highlighted: root.tools.notched
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Hide every bit that changed so far; bits that change afterwards, e.g. when you press a button, stand out")
                            onClicked: root.tools.notch()
                        }
                        Button {
                            visible: root.tools.notched
                            text: qsTr("Clear notch")
                            onClicked: root.tools.clear_notch()
                        }
                        Button {
                            text: qsTr("Reset")
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Forget the statistics and start counting again")
                            onClicked: root.tools.reset()
                        }
                        Label {
                            text: root.status
                            color: Theme.muted
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Label {
                            text: root.tools.last_data
                            color: Theme.muted
                            font.family: "monospace"
                        }
                    }

                    Label {
                        visible: !root.tools.selected
                        text: qsTr("Select an identifier to see its bits")
                        color: Theme.muted
                    }

                    // Bits: byte 0 at the top, bit 7 on the left
                    Card {
                        visible: root.tools.selected !== ""
                        title: root.tools.selected_name
                            ? qsTr("Bits of %1 (%2)").arg(root.tools.selected).arg(root.tools.selected_name)
                            : qsTr("Bits of %1").arg(root.tools.selected)
                        Layout.fillWidth: true
                        Layout.preferredHeight: bitGrid.implicitHeight + legend.implicitHeight + 80

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8

                            Column {
                                id: bitGrid
                                spacing: 3

                                Row {
                                    spacing: 3
                                    Item { width: 56; height: 1 }
                                    Repeater {
                                        model: [7, 6, 5, 4, 3, 2, 1, 0]
                                        Label {
                                            required property int modelData
                                            width: 38
                                            text: modelData
                                            color: Theme.muted
                                            font.pixelSize: 11
                                            horizontalAlignment: Text.AlignHCenter
                                        }
                                    }
                                }

                                Repeater {
                                    model: Math.floor(root.tools.bits.length / 8)
                                    Row {
                                        id: byteRow
                                        required property int index
                                        spacing: 3

                                        // Clicking the byte label selects the whole byte
                                        Label {
                                            width: 56
                                            height: 28
                                            verticalAlignment: Text.AlignVCenter
                                            text: qsTr("Byte %1").arg(byteRow.index)
                                            color: Theme.muted
                                            font.pixelSize: 12
                                            MouseArea {
                                                anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: {
                                                    root.anchorBit = byteRow.index * 8 + 7
                                                    root.tools.select_bits(byteRow.index * 8 + 7, byteRow.index * 8)
                                                }
                                            }
                                        }
                                        Repeater {
                                            // A fixed count with indexed access into the cached bits list. Slicing
                                            // returned a new array on every refresh, recreating the cells — so a
                                            // cell clicked while a refresh was due (selecting a bit itself triggers
                                            // one) was destroyed mid-click and crashed. A constant model keeps the
                                            // cells; only their bindings re-evaluate.
                                            model: 8
                                            Rectangle {
                                                id: cell
                                                required property int index
                                                readonly property var bit: root.tools.bits[byteRow.index * 8 + cell.index] || root.emptyBit
                                                width: 38
                                                height: 28
                                                radius: 5
                                                color: bit.masked ? Theme.alpha(Theme.muted, 0.06)
                                                    : bit.flips > 0 ? Theme.alpha(Theme.chart[0], 0.12 + 0.6 * root.heat(bit.flips))
                                                    : Theme.cardAlt
                                                border.width: bit.field || bit.hit ? 2 : 1
                                                border.color: bit.field ? Theme.accent : bit.hit ? Theme.warning : Theme.alpha(Theme.border, 0.6)

                                                // Fades out after a change
                                                Rectangle {
                                                    anchors.fill: parent
                                                    radius: parent.radius
                                                    visible: !cell.bit.masked && cell.bit.recent > 0
                                                    color: Theme.alpha(Theme.warning, 0.65 * cell.bit.recent)
                                                }
                                                Label {
                                                    anchors.centerIn: parent
                                                    text: cell.bit.value
                                                    color: cell.bit.masked ? Theme.muted : Theme.text
                                                    font.family: "monospace"
                                                    font.bold: cell.bit.value === 1
                                                }
                                                MouseArea {
                                                    id: bitArea
                                                    anchors.fill: parent
                                                    hoverEnabled: true
                                                    onClicked: mouse => {
                                                        if ((mouse.modifiers & Qt.ShiftModifier) && root.anchorBit >= 0) {
                                                            root.tools.select_bits(root.anchorBit, cell.bit.bit)
                                                        } else {
                                                            root.anchorBit = cell.bit.bit
                                                            root.tools.select_bits(cell.bit.bit, cell.bit.bit)
                                                        }
                                                    }
                                                }
                                                ToolTip.visible: bitArea.containsMouse
                                                ToolTip.text: qsTr("Bit %1: set in %2 of the frames, changed in %3")
                                                    .arg(bit.bit).arg(root.percent(bit.ones)).arg(root.percent(bit.flips))
                                                    + (bit.age >= 0 ? qsTr(", last %1 s ago").arg(bit.age.toFixed(1)) : qsTr(", never changed"))
                                                    + (bit.masked ? qsTr(" (hidden by the notch)") : "")
                                            }
                                        }
                                    }
                                }
                            }

                            Label {
                                id: legend
                                text: qsTr("Click a bit to start a field, Shift+click another bit to extend it, click a byte label to select the byte. Blue: how often a bit changes; orange: changed just now; outlined orange: changed since the notch.")
                                color: Theme.muted
                                font.pixelSize: 12
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                        }
                    }

                    // Bit field
                    Card {
                        visible: root.tools.selected !== ""
                        title: qsTr("Bit field %1").arg(root.tools.field_key)
                        Layout.fillWidth: true
                        Layout.preferredHeight: fieldGrid.implicitHeight + fieldButtons.implicitHeight + 90

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8

                            GridLayout {
                                id: fieldGrid
                                columns: 8
                                columnSpacing: 10
                                rowSpacing: 6
                                Layout.fillWidth: true

                                component FieldText: TextField {
                                    property string field
                                    text: String(root.tools.field[field])
                                    Layout.fillWidth: true
                                    onEditingFinished: if (text !== String(root.tools.field[field])) {
                                        const change = {}
                                        change[field] = text
                                        root.tools.set_field(change)
                                        text = Qt.binding(() => String(root.tools.field[field]))
                                    }
                                }

                                Label { text: qsTr("Start bit"); color: Theme.muted }
                                FieldText { field: "start" }
                                Label { text: qsTr("Length"); color: Theme.muted }
                                FieldText { field: "length" }
                                Label { text: qsTr("Byte order"); color: Theme.muted }
                                ComboBox {
                                    model: [qsTr("Intel (little endian)"), qsTr("Motorola (big endian)")]
                                    currentIndex: root.tools.byte_orders.indexOf(root.tools.field.byte_order)
                                    onActivated: index => root.tools.set_field({ byte_order: root.tools.byte_orders[index] })
                                    Layout.fillWidth: true
                                }
                                CheckBox {
                                    text: qsTr("Signed")
                                    checked: root.tools.field.signed
                                    onToggled: root.tools.set_field({ signed: checked })
                                    Layout.columnSpan: 2
                                }

                                Label { text: qsTr("Factor"); color: Theme.muted }
                                FieldText { field: "scale" }
                                Label { text: qsTr("Offset"); color: Theme.muted }
                                FieldText { field: "offset" }
                                Label { text: qsTr("Unit"); color: Theme.muted }
                                FieldText { field: "unit" }
                                Label { text: qsTr("Name"); color: Theme.muted }
                                FieldText { field: "name"; placeholderText: qsTr("Signal_%1").arg(root.tools.field.start) }
                            }

                            RowLayout {
                                id: fieldButtons
                                spacing: 8
                                Layout.fillWidth: true
                                Pill {
                                    visible: root.tools.field_multiplexer !== ""
                                    text: qsTr("only if %1").arg(root.tools.field_multiplexer)
                                    tint: Theme.chart[5]
                                    ToolTip.visible: pillArea.containsMouse
                                    ToolTip.text: qsTr("The field is multiplexed: only frames with this multiplexer value carry it. Click to use all frames.")
                                    MouseArea {
                                        id: pillArea
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: root.tools.clear_multiplexer()
                                    }
                                }
                                Label {
                                    text: root.tools.field_error ? root.tools.field_error
                                        : qsTr("Value: %1").arg(root.tools.field_value || "–")
                                    color: root.tools.field_error ? Theme.danger : Theme.text
                                    font.family: root.tools.field_error ? Qt.application.font.family : "monospace"
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Button {
                                    text: qsTr("Plot")
                                    ToolTip.visible: hovered
                                    ToolTip.text: qsTr("Plot the field on the Signals page, from the first frame in the trace on")
                                    onClicked: {
                                        const error = root.tools.plot_field()
                                        if (error)
                                            root.showStatus(error)
                                        else
                                            root.showPage("signals")
                                    }
                                }
                                Button {
                                    text: qsTr("Add to database")
                                    ToolTip.visible: hovered
                                    ToolTip.text: qsTr("Add the field as a signal in the database editor (and its message if needed)")
                                    onClicked: {
                                        const error = connectionManager.database_editor.add_found_signal(
                                            root.tools.signal_definition, root.connection.database_file)
                                        if (error)
                                            root.showStatus(error)
                                        else
                                            Actions.showTool("database-editor")
                                    }
                                }
                            }
                        }
                    }

                    // Bytes
                    Card {
                        visible: root.tools.selected !== ""
                        title: qsTr("Bytes")
                        Layout.fillWidth: true
                        Layout.preferredHeight: byteGrid.implicitHeight + 70

                        GridLayout {
                            id: byteGrid
                            anchors.fill: parent
                            columns: 7
                            columnSpacing: 16
                            rowSpacing: 4

                            Repeater {
                                model: [qsTr("Byte"), qsTr("Value"), qsTr("Distinct"), qsTr("Min"), qsTr("Max"),
                                        qsTr("Entropy"), qsTr("Values 00–FF")]
                                Label {
                                    required property string modelData
                                    text: modelData
                                    color: Theme.muted
                                    font.bold: true
                                    font.pixelSize: 12
                                }
                            }
                            Repeater {
                                model: root.tools.bytes
                                delegate: Repeater {
                                    id: byteStats
                                    required property var modelData
                                    model: 7
                                    Item {
                                        required property int index
                                        implicitWidth: index === 6 ? histogram.implicitWidth : cellText.implicitWidth
                                        implicitHeight: 22
                                        Layout.fillWidth: index === 6
                                        Label {
                                            id: cellText
                                            visible: parent.index < 6
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: parent.index < 6
                                                ? String([byteStats.modelData.index, byteStats.modelData.value, byteStats.modelData.distinct,
                                                          byteStats.modelData.minimum, byteStats.modelData.maximum,
                                                          byteStats.modelData.entropy.toFixed(2)][parent.index]) : ""
                                            color: Theme.text
                                            font.family: "monospace"
                                            font.pixelSize: 12
                                        }
                                        Row {
                                            id: histogram
                                            visible: parent.index === 6
                                            height: 20
                                            anchors.verticalCenter: parent.verticalCenter
                                            spacing: 1
                                            Repeater {
                                                model: byteStats.modelData.histogram
                                                Rectangle {
                                                    required property real modelData
                                                    width: 3
                                                    height: Math.max(1, modelData * 20)
                                                    y: 20 - height
                                                    color: modelData > 0 ? Theme.chart[1] : Theme.alpha(Theme.border, 0.5)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            ReverseAnalysisPanel {
                tools: root.tools
                connection: root.connection
                onFieldUsed: tabBar.currentIndex = 0
                onPlotted: root.showPage("signals")
                onMessage: text => root.showStatus(text)
            }
            ReverseReferencePanel {
                tools: root.tools
                connection: root.connection
                onFieldUsed: tabBar.currentIndex = 0
                onPlotted: root.showPage("signals")
                onMessage: text => root.showStatus(text)
            }
            ReverseMarkersPanel {
                tools: root.tools
                onFieldUsed: tabBar.currentIndex = 0
                onPlotted: root.showPage("signals")
                onMessage: text => root.showStatus(text)
            }
            ReverseRelatedPanel {
                tools: root.tools
                onFieldUsed: tabBar.currentIndex = 0
                onPlotted: root.showPage("signals")
                onMessage: text => root.showStatus(text)
            }
        }

        Label {
            visible: tabBar.currentIndex > 0 && root.status !== ""
            text: root.status
            color: Theme.warning
            Layout.fillWidth: true
        }
    }
}
