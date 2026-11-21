import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Cryptanalysis: randomness tests, freshness analysis, linear relations and keystream reuse for every identifier.
ColumnLayout {
    id: root
    required property var tools
    readonly property var crypto: tools.crypto

    signal fieldUsed()
    signal plotted()
    signal message(string text)

    spacing: 8

    function verdictColor(verdict) {
        return verdict === "random" ? Theme.success
             : verdict === "structured" ? Theme.chart[1]
             : verdict === "finding" ? Theme.danger
             : verdict === "inconclusive" ? Theme.muted : Theme.text
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Button {
            text: qsTr("Analyse all identifiers")
            enabled: !root.crypto.running
            highlighted: true
            onClicked: root.crypto.run(false)
        }
        Button {
            text: qsTr("Analyse %1").arg(root.tools.selected || qsTr("selected"))
            enabled: !root.crypto.running && root.tools.selected !== ""
            onClicked: root.crypto.run(true)
        }
        BusyIndicator {
            running: root.crypto.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.crypto.status
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    Label {
        text: qsTr("Runs statistical randomness tests (monobit, runs, chi-square, serial correlation) on trailing bytes, searches for linear dependencies (CRC, XOR masks) via GF(2) Gaussian elimination, and detects keystream reuse in suspected encrypted bytes. Passive — reads captured frames only.")
        color: Theme.muted
        font.pixelSize: 12
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    ReverseResults {
        rows: root.crypto.results
        columns: [
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Test"), role: "test", width: 120 },
            { title: qsTr("Verdict"), role: "verdict", width: 100 },
            { title: qsTr("Confidence"), role: "confidence", width: 75 },
            { title: qsTr("Details"), role: "details", width: 0 },
        ]
        cellColor: (row, role) => role === "verdict" ? root.verdictColor(row.verdict) : Theme.text
        emptyText: root.crypto.running ? "" : qsTr("No results yet — run the analysis to assess the security of the captured frames")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => { root.crypto.use(index); root.fieldUsed() }
        onPlotRow: index => { root.crypto.plot(index); root.plotted() }
    }
}
