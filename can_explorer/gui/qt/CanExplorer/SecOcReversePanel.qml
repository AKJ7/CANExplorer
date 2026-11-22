import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// SecOC reverse engineering: layout detection, freshness reconstruction, MAC strength,
// key recovery status and bypass detection for every identifier.
ColumnLayout {
    id: root
    required property var tools
    readonly property var secoc: tools.secoc_reverse

    signal message(string text)

    spacing: 8

    function severityColor(severity) {
        return severity === "finding" ? Theme.danger
             : severity === "warning" ? Theme.warning
             : severity === "random" ? Theme.success
             : Theme.muted
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Button {
            text: qsTr("Analyse all identifiers")
            enabled: !root.secoc.running
            highlighted: true
            onClicked: root.secoc.run(false)
        }
        Button {
            text: qsTr("Analyse %1").arg(root.tools.selected || qsTr("selected"))
            enabled: !root.secoc.running && root.tools.selected !== ""
            onClicked: root.secoc.run(true)
        }
        BusyIndicator {
            running: root.secoc.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.secoc.status
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    Label {
        text: qsTr("Detects SecOC-protected frames, reconstructs freshness counters, assesses MAC truncation strength, attempts key recovery from common/weak keys and detects SecOC bypass paths. Passive — reads captured frames only.")
        color: Theme.muted
        font.pixelSize: 12
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    ReverseResults {
        rows: root.secoc.results
        columns: [
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Category"), role: "category", width: 100 },
            { title: qsTr("Finding"), role: "finding", width: 200 },
            { title: qsTr("Severity"), role: "severity", width: 80 },
            { title: qsTr("Confidence"), role: "confidence", width: 75 },
            { title: qsTr("Details"), role: "details", width: 0 }
        ]
        cellColor: (row, role) => role === "severity" ? root.severityColor(row.severity) : Theme.text
        emptyText: root.secoc.running ? "" : qsTr("No results yet — run the analysis to assess SecOC protection of the captured frames")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => root.secoc.use(index)
        onPlotRow: index => root.secoc.plot(index)
    }
}
