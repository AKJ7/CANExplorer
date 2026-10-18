import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Comparing two recordings: identifiers only in one of them, cycle times, payloads and signal values.
ColumnLayout {
    id: root
    readonly property var compare: connectionManager.compare
    readonly property var counts: compare.counts
    readonly property string pageTitle: compare.recording_a.name && compare.recording_b.name
        ? `${compare.recording_a.name} ↔ ${compare.recording_b.name}` : ""
    property string message: ""
    property bool messageIsError: false
    property string openingSide: "a"

    spacing: Theme.spacing

    function show(text, error) {
        message = text
        messageIsError = error
    }

    function statusText(status) {
        return status === "changed" ? qsTr("changed") : status === "only_a" ? qsTr("only in A")
             : status === "only_b" ? qsTr("only in B") : qsTr("same")
    }

    function statusColor(status) {
        return status === "changed" ? Theme.warning : status === "only_a" ? Theme.danger
             : status === "only_b" ? Theme.accent : Theme.success
    }

    function applyAlignment() {
        show(compare.set_alignment(alignBox.currentValue, alignIdField.text), true)
    }

    // The two recordings
    RowLayout {
        spacing: Theme.spacing
        Layout.fillWidth: true

        Repeater {
            model: [
                { side: "a", title: qsTr("Recording A") },
                { side: "b", title: qsTr("Recording B") },
            ]
            Card {
                id: fileCard
                required property var modelData
                readonly property var info: modelData.side === "a" ? root.compare.recording_a : root.compare.recording_b
                Layout.fillWidth: true
                Layout.preferredHeight: 84
                padding: 14

                RowLayout {
                    anchors.fill: parent
                    spacing: 12
                    ColumnLayout {
                        spacing: 2
                        Layout.fillWidth: true
                        Label {
                            text: fileCard.modelData.title
                            color: Theme.muted
                            font.pixelSize: 12
                        }
                        Label {
                            text: fileCard.info.loading ? qsTr("Reading…") : fileCard.info.name || qsTr("No recording")
                            font.bold: true
                            elide: Text.ElideMiddle
                            Layout.fillWidth: true
                        }
                        Label {
                            text: fileCard.info.error || fileCard.info.text
                            color: fileCard.info.error ? Theme.danger : Theme.muted
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                    }
                    Button {
                        text: qsTr("Open…")
                        enabled: !fileCard.info.loading
                        onClicked: {
                            root.openingSide = fileCard.modelData.side
                            fileDialog.open()
                        }
                    }
                }
            }
        }
        Button {
            text: "⇄"
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Swap A and B")
            enabled: !root.compare.busy
            onClicked: root.compare.swap()
        }
    }

    // Options
    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Label { text: qsTr("Align") }
        ComboBox {
            id: alignBox
            model: root.compare.alignments
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 260
            onActivated: root.applyAlignment()
            ToolTip.visible: hovered
            ToolTip.text: qsTr("How frames of an identifier are paired: by their time since the start (or since the first "
                               + "frame of an identifier, e.g. a wake-up message), or the n-th frame in A with the n-th in B")
        }
        TextField {
            id: alignIdField
            visible: alignBox.currentValue === "id"
            placeholderText: qsTr("ID, hex")
            Layout.preferredWidth: 110
            onEditingFinished: root.applyAlignment()
        }
        Button {
            text: root.compare.database_file ? root.compare.database_file.split(/[\\/]/).pop() : qsTr("Database…")
            ToolTip.visible: hovered
            ToolTip.text: root.compare.database_file ? qsTr("Click to unload the database")
                                                     : qsTr("Load a signal database to compare signal values and name the messages")
            onClicked: root.compare.database_file ? root.compare.unload_database() : databaseDialog.open()
        }
        BusyIndicator {
            running: root.compare.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
        Label {
            text: root.compare.align_error || root.message
            color: root.compare.align_error || root.messageIsError ? Theme.danger : Theme.success
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        CheckBox {
            id: showSame
            text: qsTr("Show unchanged")
        }
        Button {
            text: qsTr("Export CSV…")
            enabled: root.compare.has_result
            onClicked: exportDialog.open()
        }
    }

    Card {
        Layout.fillWidth: true
        Layout.fillHeight: true
        padding: 12

        ColumnLayout {
            anchors.fill: parent
            spacing: 6

            Label {
                visible: !root.compare.has_result
                text: qsTr("Open two recordings to compare them.")
                color: Theme.muted
            }

            RowLayout {
                visible: root.compare.has_result
                spacing: 8
                Pill { text: qsTr("%1 changed").arg(root.counts.changed || 0); tint: Theme.warning }
                Pill { text: qsTr("%1 only in A").arg(root.counts.only_a || 0); tint: Theme.danger }
                Pill { text: qsTr("%1 only in B").arg(root.counts.only_b || 0); tint: Theme.accent }
                Pill { text: qsTr("%1 same").arg(root.counts.same || 0); tint: Theme.success }
                Item { Layout.fillWidth: true }
                TabBar {
                    id: tabs
                    TabButton { text: qsTr("Identifiers"); width: implicitWidth }
                    TabButton {
                        text: qsTr("Signals")
                        width: implicitWidth
                        enabled: root.compare.database_file.length > 0
                    }
                }
            }

            StackLayout {
                visible: root.compare.has_result
                currentIndex: tabs.currentIndex
                Layout.fillWidth: true
                Layout.fillHeight: true

                // Identifiers
                ColumnLayout {
                    spacing: 0
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 8
                        Layout.rightMargin: 20
                        spacing: 8
                        Repeater {
                            model: [
                                [qsTr("Identifier"), 170], [qsTr("Status"), 90], [qsTr("Frames A / B"), 110],
                                [qsTr("Cycle A / B (ms)"), 160], [qsTr("Differing"), 90],
                            ]
                            Label {
                                required property var modelData
                                text: modelData[0]
                                color: Theme.muted
                                font.pixelSize: 12
                                Layout.preferredWidth: modelData[1]
                            }
                        }
                        Label { text: qsTr("Notes"); color: Theme.muted; font.pixelSize: 12; Layout.fillWidth: true }
                    }
                    ListView {
                        id: idList
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ScrollBar.vertical: ScrollBar {}
                        model: root.compare.identifiers.filter(item => showSame.checked || item.status !== "same")
                        delegate: ItemDelegate {
                            id: idItem
                            required property var modelData
                            width: idList.width - 12
                            ToolTip.visible: hovered && modelData.first_time.length > 0
                            ToolTip.text: qsTr("First difference at %1 s\nA: %2\nB: %3")
                                .arg(modelData.first_time).arg(modelData.data_a).arg(modelData.data_b)
                            contentItem: RowLayout {
                                spacing: 8
                                Label {
                                    text: idItem.modelData.id + (idItem.modelData.name ? `  ${idItem.modelData.name}` : "")
                                    font.family: "monospace"
                                    elide: Text.ElideRight
                                    Layout.preferredWidth: 170
                                }
                                Item {
                                    Layout.preferredWidth: 90
                                    implicitHeight: statusPill.implicitHeight
                                    Pill {
                                        id: statusPill
                                        text: root.statusText(idItem.modelData.status)
                                        tint: root.statusColor(idItem.modelData.status)
                                    }
                                }
                                Label {
                                    text: `${idItem.modelData.count_a} / ${idItem.modelData.count_b}`
                                    Layout.preferredWidth: 110
                                }
                                Label {
                                    text: (idItem.modelData.cycle_a || "–") + " / " + (idItem.modelData.cycle_b || "–")
                                          + (idItem.modelData.cycle_change ? `  (${idItem.modelData.cycle_change})` : "")
                                    Layout.preferredWidth: 160
                                }
                                Label {
                                    text: idItem.modelData.paired ? `${idItem.modelData.differing} / ${idItem.modelData.paired}` : ""
                                    color: idItem.modelData.differing ? Theme.warning : Theme.text
                                    Layout.preferredWidth: 90
                                }
                                Label {
                                    text: idItem.modelData.notes
                                    color: Theme.muted
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }

                // Signals
                ColumnLayout {
                    spacing: 0
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 8
                        Layout.rightMargin: 20
                        spacing: 8
                        Repeater {
                            model: [
                                [qsTr("Signal"), 260], [qsTr("Range A"), 150], [qsTr("Range B"), 150],
                                [qsTr("Mean A / B"), 150], [qsTr("Differing"), 90], [qsTr("Max. difference"), 110],
                            ]
                            Label {
                                required property var modelData
                                text: modelData[0]
                                color: Theme.muted
                                font.pixelSize: 12
                                Layout.preferredWidth: modelData[1]
                            }
                        }
                        Label { text: qsTr("First difference"); color: Theme.muted; font.pixelSize: 12; Layout.fillWidth: true }
                    }
                    ListView {
                        id: signalList
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ScrollBar.vertical: ScrollBar {}
                        model: root.compare.signal_differences.filter(item => showSame.checked || item.changed)
                        delegate: ItemDelegate {
                            id: signalItem
                            required property var modelData
                            width: signalList.width - 12
                            contentItem: RowLayout {
                                spacing: 8
                                Label {
                                    text: signalItem.modelData.key + (signalItem.modelData.unit ? ` [${signalItem.modelData.unit}]` : "")
                                    color: signalItem.modelData.changed ? Theme.text : Theme.muted
                                    elide: Text.ElideRight
                                    Layout.preferredWidth: 260
                                }
                                Label { text: signalItem.modelData.range_a; Layout.preferredWidth: 150 }
                                Label { text: signalItem.modelData.range_b; Layout.preferredWidth: 150 }
                                Label {
                                    text: `${signalItem.modelData.mean_a || "–"} / ${signalItem.modelData.mean_b || "–"}`
                                    elide: Text.ElideRight
                                    Layout.preferredWidth: 150
                                }
                                Label {
                                    text: signalItem.modelData.paired ? `${signalItem.modelData.differing} / ${signalItem.modelData.paired}` : ""
                                    color: signalItem.modelData.differing ? Theme.warning : Theme.text
                                    Layout.preferredWidth: 90
                                }
                                Label { text: signalItem.modelData.max_difference; Layout.preferredWidth: 110 }
                                Label {
                                    text: signalItem.modelData.first
                                    color: Theme.muted
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    FileDialog {
        id: fileDialog
        title: root.openingSide === "a" ? qsTr("Recording A") : qsTr("Recording B")
        fileMode: FileDialog.OpenFile
        nameFilters: root.compare.file_filters
        onAccepted: root.show(root.compare.open_file(root.openingSide, selectedFile), true)
    }

    FileDialog {
        id: databaseDialog
        title: qsTr("Signal database")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("Signal databases (*.dbc *.dbf *.kcd *.sym *.arxml *.ldf)"), qsTr("All files (*)")]
        onAccepted: root.show(root.compare.load_database(selectedFile), true)
    }

    FileDialog {
        id: exportDialog
        title: qsTr("Export comparison")
        fileMode: FileDialog.SaveFile
        defaultSuffix: "csv"
        nameFilters: [qsTr("CSV files (*.csv)")]
        onAccepted: {
            const text = root.compare.export_csv(selectedFile)
            root.show(text, !text.startsWith("Saved"))
        }
    }
}
