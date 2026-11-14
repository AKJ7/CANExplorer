import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Security events of a connection: SecOC and E2E failures, unknown identifiers, frames faster than their cycle
// time, refused security access or authentication — aggregated per kind and identifier.
Card {
    id: root
    required property var connection
    readonly property var events: connection.security_events
    readonly property var counts: connection.security_event_counts
    title: qsTr("Security events")

    function severityColor(severity) {
        return severity === "high" ? Theme.danger : severity === "medium" ? Theme.warning : Theme.muted
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Pill {
                text: qsTr("%1 events").arg(root.counts.events || 0)
                tint: Theme.accent
            }
            Pill {
                visible: (root.counts.high || 0) > 0
                text: qsTr("%1 high").arg(root.counts.high)
                tint: Theme.danger
            }
            Pill {
                visible: (root.counts.frames || 0) > 0
                text: qsTr("%1 frames").arg(root.counts.frames)
                tint: Theme.chart[4]
            }
            Item { Layout.fillWidth: true }
            Label { id: statusLabel; color: Theme.muted }
            Button {
                text: qsTr("Export…")
                enabled: root.events.length > 0
                onClicked: exportDialog.open()
            }
            Button {
                text: qsTr("Clear")
                enabled: root.events.length > 0
                onClicked: root.connection.clear_security_events()
            }
        }

        Label {
            visible: root.events.length === 0
            text: qsTr("No security events. Watched as frames arrive: SecOC and E2E failures of the database's secured and protected PDUs, identifiers the database does not define, frames arriving much sooner than their cycle time (injected frames competing with the sender), and refused security access or authentication. An event repeating 3 times within a second becomes high severity.")
            color: Theme.muted
            wrapMode: Text.Wrap
            verticalAlignment: Text.AlignTop
            Layout.fillWidth: true
            Layout.fillHeight: true  // Takes the list's place, so the rows stay at the top
        }

        ListView {
            id: list
            visible: root.events.length > 0
            model: root.events
            clip: true
            spacing: 2
            Layout.fillWidth: true
            Layout.fillHeight: true
            ScrollBar.vertical: ScrollBar {}
            header: RowLayout {
                width: list.width
                spacing: 12
                Label { text: ""; Layout.preferredWidth: 10 }
                Label { text: qsTr("Event"); color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 250 }
                Label { text: qsTr("ID"); color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 90 }
                Label { text: qsTr("Count"); color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 60 }
                Label { text: qsTr("Last (s)"); color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 80 }
                Label { text: qsTr("Detail"); color: Theme.muted; font.pixelSize: 12; Layout.fillWidth: true }
            }
            delegate: RowLayout {
                required property var modelData
                width: list.width
                spacing: 12
                Rectangle {
                    width: 10; height: 10; radius: 5
                    color: root.severityColor(modelData.severity)
                    Layout.preferredWidth: 10
                    ToolTip.visible: severityHover.hovered
                    ToolTip.text: modelData.severity
                    HoverHandler { id: severityHover }
                }
                Label { text: modelData.title; color: Theme.text; font.pixelSize: 12; elide: Text.ElideRight; Layout.preferredWidth: 250 }
                Label { text: modelData.identifier; font.family: "monospace"; color: Theme.text; font.pixelSize: 12; Layout.preferredWidth: 90 }
                Label { text: modelData.count; font.family: "monospace"; color: Theme.text; font.pixelSize: 12; Layout.preferredWidth: 60 }
                Label { text: Number(modelData.last).toFixed(3); font.family: "monospace"; color: Theme.muted; font.pixelSize: 12; Layout.preferredWidth: 80 }
                Label {
                    text: modelData.detail
                    color: Theme.text
                    font.pixelSize: 12
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    ToolTip.visible: detailHover.hovered && truncated
                    ToolTip.text: modelData.detail
                    HoverHandler { id: detailHover }
                }
            }
        }
    }

    FileChooser {
        id: exportDialog
        title: qsTr("Export security events")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "csv"
        nameFilters: [qsTr("CSV files (*.csv)"), qsTr("JSON files (*.json)")]
        onAccepted: statusLabel.text = root.connection.export_security_events(selectedFile)
    }
}
