import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Security dashboard: a summary of the combined security posture.
ColumnLayout {
    id: root
    required property var tools
    readonly property var dashboard: tools.security_dashboard

    signal message(string text)

    spacing: 8

    function severityColor(severity) {
        return severity === "critical" || severity === "finding" ? Theme.danger
             : severity === "high" || severity === "warning" ? Theme.warning
             : severity === "medium" ? Theme.chart[2]
             : Theme.muted
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true
        Button {
            text: qsTr("Refresh dashboard")
            enabled: !root.dashboard.running
            highlighted: true
            onClicked: root.dashboard.run()
        }
        BusyIndicator {
            running: root.dashboard.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.dashboard.status
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    Label {
        text: qsTr("Combined security posture: ECU count, bypass findings by severity, SecOC coverage, seed quality grades and top findings from all analyses.")
        color: Theme.muted
        font.pixelSize: 12
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Summary stat cards
    Flow {
        spacing: 12
        Layout.fillWidth: true
        visible: Object.keys(root.dashboard.stats).length > 0

        Card {
            width: 160
            height: 80
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                Label { text: qsTr("ECUs"); font.pixelSize: 11; color: Theme.muted }
                Label {
                    text: (root.dashboard.stats.ecu_count || 0).toString()
                    font.pixelSize: 28; font.bold: true
                }
            }
        }
        Card {
            width: 160
            height: 80
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                Label { text: qsTr("Findings"); font.pixelSize: 11; color: Theme.muted }
                Label {
                    readonly property int count: root.dashboard.stats.total_findings || 0
                    text: count.toString()
                    font.pixelSize: 28; font.bold: true
                    color: count > 0 ? Theme.danger : Theme.text
                }
            }
        }
        Card {
            width: 160
            height: 80
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                Label { text: qsTr("Overall score"); font.pixelSize: 11; color: Theme.muted }
                Label {
                    text: (root.dashboard.stats.overall_score || 0) + "/10"
                    font.pixelSize: 28; font.bold: true
                }
            }
        }
        Card {
            width: 160
            height: 80
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                Label { text: qsTr("Grade"); font.pixelSize: 11; color: Theme.muted }
                Label {
                    text: root.dashboard.stats.overall_grade || "—"
                    font.pixelSize: 28; font.bold: true
                }
            }
        }
    }

    // Summary lines
    Repeater {
        model: root.dashboard.stats.summary || []
        Label {
            text: "• " + modelData
            color: Theme.text
            font.pixelSize: 12
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
    }

    // Top findings table
    Label {
        text: qsTr("Top findings")
        font.pixelSize: 14
        font.bold: true
        visible: root.dashboard.results.length > 0
    }

    ReverseResults {
        rows: root.dashboard.results
        columns: [
            { title: qsTr("ECU"), role: "ecu_id", width: 70, mono: true },
            { title: qsTr("Category"), role: "category", width: 120 },
            { title: qsTr("Finding"), role: "finding", width: 200 },
            { title: qsTr("Severity"), role: "severity", width: 80 },
            { title: qsTr("Details"), role: "details", width: 0 }
        ]
        cellColor: (row, role) => role === "severity" ? root.severityColor(row.severity) : Theme.text
        emptyText: root.dashboard.running ? "" : qsTr("No data yet — refresh the dashboard to run all analyses")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => root.dashboard.use(index)
    }
}
