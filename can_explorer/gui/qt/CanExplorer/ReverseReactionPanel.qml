import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Capture reaction: record a baseline, do an action, and diff what changed on the bus ("I did X, what changed?").
ColumnLayout {
    id: root
    required property var tools
    readonly property var reaction: tools.reaction

    signal fieldUsed()
    signal plotted()
    signal message(string text)

    spacing: 8

    function step() {
        const phase = root.reaction.phase
        let error = ""
        if (phase === "idle" || phase === "done")
            error = root.reaction.start_baseline()
        else if (phase === "baseline")
            error = root.reaction.mark_action()
        else if (phase === "action")
            root.reaction.capture()
        if (error)
            root.message(error)
    }

    RowLayout {
        spacing: 8
        Layout.fillWidth: true

        Button {
            text: {
                switch (root.reaction.phase) {
                case "baseline": return qsTr("Start action")
                case "action": return qsTr("Capture reaction")
                default: return qsTr("Record baseline")
                }
            }
            highlighted: true
            enabled: !root.reaction.running
            ToolTip.visible: hovered
            ToolTip.text: qsTr("1. Record a baseline while the bus is steady  2. Start the action and do it  3. Capture the reaction")
            onClicked: root.step()
        }
        // Phase dots
        Repeater {
            model: [qsTr("Baseline"), qsTr("Action"), qsTr("Result")]
            RowLayout {
                required property int index
                required property string modelData
                spacing: 4
                readonly property int phaseIndex: root.reaction.phase === "baseline" ? 0
                    : root.reaction.phase === "action" ? 1
                    : root.reaction.phase === "done" ? 2 : -1
                Rectangle {
                    width: 10; height: 10; radius: 5
                    color: parent.phaseIndex >= parent.index ? Theme.accent : Theme.alpha(Theme.muted, 0.4)
                }
                Label { text: parent.modelData; color: Theme.muted; font.pixelSize: 12 }
            }
        }
        Button {
            text: qsTr("Reset")
            visible: root.reaction.phase !== "idle"
            onClicked: root.reaction.reset()
        }
        BusyIndicator {
            running: root.reaction.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Item { Layout.fillWidth: true }
    }

    Label {
        text: root.reaction.status
            || qsTr("Find what a CAN message controls: capture a steady baseline, then do the action (press a button, open a door) and see which identifiers appeared and which bits changed — safely, by watching, not by sending.")
        color: Theme.muted
        font.pixelSize: 12
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    ReverseResults {
        rows: root.reaction.results
        columns: [
            { title: qsTr("ID"), role: "id", width: 70, mono: true },
            { title: qsTr("Field"), role: "key", width: 150, mono: true },
            { title: qsTr("Kind"), role: "kind", width: 80 },
            { title: qsTr("Change"), role: "change", width: 0, mono: true },
            { title: qsTr("Flips"), role: "flips", width: 60, mono: true },
        ]
        cellColor: (row, role) => role === "kind" && row.kind === "new" ? Theme.success : Theme.text
        emptyText: root.reaction.phase === "done"
            ? qsTr("Nothing changed during the action")
            : qsTr("The identifiers and bits that react to the action appear here, most reactive first")
        Layout.fillWidth: true
        Layout.fillHeight: true
        onUseRow: index => {
            root.reaction.use(index)
            root.fieldUsed()
        }
        onPlotRow: index => {
            const error = root.reaction.plot(index)
            if (error)
                root.message(error)
            else
                root.plotted()
        }
    }
}
