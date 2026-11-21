import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Gateway security analysis: topology mapping, authentication patterns, access matrix,
// bypass detection, seed/key cracking and cross-ECU correlation.
ColumnLayout {
    id: root
    required property var tools
    readonly property var gw: tools.gateway_security

    signal message(string text)

    spacing: 8

    function severityColor(severity) {
        return severity === "finding" ? Theme.danger
             : severity === "warning" ? Theme.warning
             : Theme.muted
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Button {
            text: qsTr("Run analysis")
            enabled: !root.gw.running
            highlighted: true
            onClicked: root.gw.run()
        }
        Button {
            text: qsTr("Generate report")
            enabled: !root.gw.running && root.gw.results.length > 0
            onClicked: root.gw.generateReport()
        }
        Button {
            text: qsTr("Crack seed/key")
            enabled: !root.gw.running && root.gw.results.length > 0
            onClicked: root.gw.crackAll()
        }
        Button {
            id: exportBtn
            text: qsTr("Export…")
            enabled: root.gw.results.length > 0
            onClicked: exportMenu.open()

            Menu {
                id: exportMenu
                y: exportBtn.height
                MenuItem {
                    text: qsTr("Export as JSON")
                    onTriggered: {
                        let content = root.gw.exportReport("json")
                        if (content) root.message(qsTr("JSON report copied (%1 bytes)").arg(content.length))
                    }
                }
                MenuItem {
                    text: qsTr("Export as CSV")
                    onTriggered: {
                        let content = root.gw.exportReport("csv")
                        if (content) root.message(qsTr("CSV report copied (%1 bytes)").arg(content.length))
                    }
                }
                MenuSeparator {}
                MenuItem {
                    text: qsTr("Disclosure report (Markdown)")
                    onTriggered: {
                        let content = root.gw.disclosureReport("markdown")
                        if (content) root.message(qsTr("Disclosure report ready (%1 bytes)").arg(content.length))
                        else root.message(qsTr("Run the analysis first"))
                    }
                }
                MenuItem {
                    text: qsTr("Discovered DBC scaffold")
                    onTriggered: {
                        let content = root.gw.exportDiscoveredDbc()
                        if (content) root.message(qsTr("DBC scaffold ready (%1 bytes)").arg(content.length))
                        else root.message(qsTr("Run the analysis first"))
                    }
                }
            }
        }
        BusyIndicator {
            running: root.gw.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.gw.status
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    Label {
        text: qsTr("Maps gateway topology, builds an access matrix, analyses authentication patterns and timing, NRC fingerprints ECUs, plans probing steps, detects bypass paths, assesses seed entropy and cracks seed/key algorithms. Passive — reads captured frames only; cracking runs offline on extracted pairs.")
        color: Theme.muted
        font.pixelSize: 12
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Category filter
    RowLayout {
        spacing: 4
        Layout.fillWidth: true
        Label { text: qsTr("Filter:"); color: Theme.muted }
        Repeater {
            model: ["All", "NRC fingerprint", "Auth timing", "Probe plan", "Filtering", "Topology", "Seed/key crack"]
            Button {
                text: modelData
                flat: true
                highlighted: root.categoryFilter === modelData
                font.pixelSize: 12
                onClicked: root.categoryFilter = (root.categoryFilter === modelData ? "All" : modelData)
            }
        }
    }

    property string categoryFilter: "All"
    property var filteredResults: {
        if (categoryFilter === "All") return root.gw.results
        let out = []
        for (let i = 0; i < root.gw.results.length; i++) {
            if (root.gw.results[i].category === categoryFilter)
                out.push(root.gw.results[i])
        }
        return out
    }

    ReverseResults {
        rows: root.filteredResults
        columns: [
            { title: qsTr("Category"), role: "category", width: 120 },
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Finding"), role: "finding", width: 160 },
            { title: qsTr("Severity"), role: "severity", width: 80 },
            { title: qsTr("Details"), role: "details", width: 0 },
        ]
        cellColor: (row, role) => role === "severity" ? root.severityColor(row.severity) : Theme.text
        emptyText: root.gw.running ? "" : qsTr("No results yet — run the analysis on captured UDS traffic to find gateway patterns")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => root.gw.use(index)
        onPlotRow: index => root.gw.plot(index)
    }
}
