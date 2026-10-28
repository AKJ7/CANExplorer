import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Related signals: fields that carry the same signal across messages are found and grouped, without any reference.
ColumnLayout {
    id: root
    required property var tools
    readonly property var related: tools.related

    signal fieldUsed()
    signal plotted()
    signal message(string text)

    spacing: 8

    RowLayout {
        spacing: 8
        Layout.fillWidth: true

        Label { text: qsTr("Minimum correlation") }
        SpinBox {
            id: correlationBox
            from: 90
            to: 100
            value: 99
            Layout.preferredWidth: 110
            textFromValue: function(value) { return (value / 100).toFixed(2) }
            valueFromText: function(text) { return Math.round(Number(text) * 100) }
            ToolTip.visible: hovered
            ToolTip.text: qsTr("How closely two fields must follow one another to be grouped as the same signal")
        }
        Label { text: qsTr("Max lag") }
        TextField {
            id: lagField
            text: "0.5"
            validator: DoubleValidator { bottom: 0; top: 10 }
            Layout.preferredWidth: 60
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Seconds one copy of a signal may lag behind another (e.g. a gateway re-broadcasting it)")
        }
        Label { text: qsTr("s") }
        Button {
            text: qsTr("Find related signals")
            highlighted: true
            enabled: !root.related.running
            onClicked: root.related.find(correlationBox.value / 100, Number(lagField.text) || 0.5)
        }
        BusyIndicator {
            running: root.related.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Item { Layout.fillWidth: true }
    }

    Label {
        text: root.related.status || qsTr("Finds fields that carry the same quantity on several messages — a signal rebroadcast by a gateway, or a scaled or lagged copy — and groups them. Use a found one to label the others.")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    ReverseResults {
        rows: root.related.results
        columns: [
            { title: qsTr("Group"), role: "group", width: 55, mono: true },
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Field"), role: "key", width: 150, mono: true },
            { title: qsTr("Correlation"), role: "correlation", width: 90, mono: true },
            { title: qsTr("Factor"), role: "scale", width: 80, mono: true },
            { title: qsTr("Offset"), role: "offset", width: 80, mono: true },
            { title: qsTr("Lag"), role: "lag", width: 70, mono: true },
            { title: qsTr("Condition"), role: "condition", width: 0, mono: true },
        ]
        emptyText: qsTr("Groups of fields carrying the same signal across messages are listed here, by group. The first field of each group is the reference the others are compared with.")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => {
            root.related.use(index)
            root.fieldUsed()
        }
        onPlotRow: index => {
            const error = root.related.plot(index)
            if (error)
                root.message(error)
            else
                root.plotted()
        }
    }
}
