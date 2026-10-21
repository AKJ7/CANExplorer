import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Automatic analysis: the fields of each identifier and what kind they are.
ColumnLayout {
    id: root
    required property var tools
    readonly property var analysis: tools.analysis

    signal fieldUsed()
    signal plotted()
    signal message(string text)

    spacing: 8

    function kindColor(kind) {
        return kind === "checksum" ? Theme.chart[4] : kind === "counter" ? Theme.chart[2]
             : kind === "multiplexer" ? Theme.chart[5] : kind === "physical" ? Theme.success
             : kind === "enum" || kind === "flag" ? Theme.chart[1] : Theme.muted
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Button {
            text: qsTr("Analyse all identifiers")
            enabled: !root.analysis.running
            highlighted: true
            onClicked: root.analysis.run(false)
        }
        Button {
            text: qsTr("Analyse %1").arg(root.tools.selected || qsTr("selected"))
            enabled: !root.analysis.running && root.tools.selected !== ""
            onClicked: root.analysis.run(true)
        }
        CheckBox {
            id: constantsBox
            text: qsTr("Constant bits")
        }
        BusyIndicator {
            running: root.analysis.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.analysis.status
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    Label {
        text: qsTr("Finds checksums (CRC-8, XOR, sums), multiplexers, counters, physical values, states and flags in the frames of the trace, in Intel and Motorola bit order. Let the bus run for a while with the values changing; bits that never change cannot be assigned.")
        color: Theme.muted
        font.pixelSize: 12
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    ReverseResults {
        rows: root.analysis.results.filter(row => constantsBox.checked || row.kind !== "constant")
        columns: [
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Field"), role: "key", width: 140, mono: true },
            { title: qsTr("Kind"), role: "kind_name", width: 85 },
            { title: qsTr("Confidence"), role: "confidence", width: 75 },
            { title: qsTr("Details"), role: "details", width: 0 },
            { title: qsTr("Condition"), role: "condition", width: 170, mono: true, optional: true },
        ]
        cellColor: (row, role) => role === "kind_name" ? root.kindColor(row.kind) : Theme.text
        emptyText: root.analysis.running ? "" : qsTr("No results yet")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => {
            root.analysis.use(index)
            root.fieldUsed()
        }
        onPlotRow: index => {
            const error = root.analysis.plot(index)
            if (error)
                root.message(error)
            else
                root.plotted()
        }
    }
}
