import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// Test scripts of a connection: run them and look at the results.
RowLayout {
    id: root
    required property var connection
    readonly property var tests: connection.tests
    readonly property var summary: tests.summary
    readonly property var details: tests.details
    property string status: ""
    property bool statusIsError: false

    spacing: Theme.spacing

    function show(text, error) {
        status = text
        statusIsError = error
        statusTimer.restart()
    }

    function verdictColor(verdict) {
        return verdict === "passed" ? Theme.success : verdict === "failed" ? Theme.danger
            : verdict === "error" ? Theme.warning : verdict === "running" ? Theme.accent : Theme.muted
    }

    function verdictSymbol(verdict) {
        return verdict === "passed" ? "✔" : verdict === "failed" ? "✘" : verdict === "error" ? "!"
            : verdict === "skipped" ? "–" : verdict === "running" ? "▶" : verdict === "pending" ? "…" : "○"
    }

    Timer {
        id: statusTimer
        interval: 6000
        onTriggered: root.status = ""
    }

    // Scripts, test cases and running
    Card {
        title: qsTr("Test cases")
        Layout.preferredWidth: 100
        Layout.horizontalStretchFactor: 46
        Layout.fillWidth: true
        Layout.fillHeight: true
        headerItems: [
            Button {
                text: qsTr("Load…")
                enabled: !root.tests.running
                onClicked: scriptDialog.open()
            },
            Button {
                text: qsTr("Reload")
                enabled: !root.tests.running && root.tests.scripts.length > 0
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Load the scripts again after editing them")
                onClicked: {
                    const error = root.tests.reload_scripts()
                    root.show(error || qsTr("Reloaded"), error.length > 0)
                }
            }
        ]

        ColumnLayout {
            anchors.fill: parent
            spacing: 10

            RowLayout {
                spacing: 8
                Layout.fillWidth: true
                Button {
                    text: root.tests.running ? qsTr("Stop") : qsTr("Run selected (%1)").arg(root.tests.selected_count)
                    highlighted: !root.tests.running
                    enabled: root.tests.running || (root.connection.connected && root.tests.selected_count > 0)
                    onClicked: {
                        if (root.tests.running) {
                            root.tests.stop()
                            return
                        }
                        const error = root.tests.run_selected()
                        if (error)
                            root.show(error, true)
                    }
                }
                Button {
                    text: qsTr("Export report…")
                    enabled: !root.tests.running && root.summary.total > 0
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("HTML report, or JUnit XML for CI servers")
                    onClicked: reportDialog.open()
                }
            }

            // Results of the last run
            RowLayout {
                visible: root.summary.total > 0
                spacing: 8
                Layout.fillWidth: true
                Pill {
                    visible: root.summary.total > 0
                    text: root.tests.running ? qsTr("%1 / %2").arg(root.summary.done).arg(root.summary.total)
                        : root.summary.stopped ? qsTr("Run stopped") : qsTr("Run %1").arg(root.summary.verdict)
                    tint: root.tests.running ? Theme.accent : root.verdictColor(root.summary.verdict)
                }
                Pill { visible: (root.summary.passed || 0) > 0; text: qsTr("%1 passed").arg(root.summary.passed); tint: Theme.success }
                Pill { visible: (root.summary.failed || 0) > 0; text: qsTr("%1 failed").arg(root.summary.failed); tint: Theme.danger }
                Pill { visible: (root.summary.errors || 0) > 0; text: qsTr("%1 errors").arg(root.summary.errors); tint: Theme.warning }
                Pill { visible: (root.summary.skipped || 0) > 0; text: qsTr("%1 skipped").arg(root.summary.skipped); tint: Theme.muted }
                Item { Layout.fillWidth: true }
                Label {
                    text: root.summary.duration || ""
                    color: Theme.muted
                }
            }

            ProgressBar {
                visible: root.tests.running
                from: 0
                to: Math.max(1, root.summary.total)
                value: root.summary.done
                Layout.fillWidth: true
            }

            Label {
                visible: root.status.length > 0 || !root.connection.connected
                text: root.status || qsTr("Connect to run tests.")
                color: root.status && root.statusIsError ? Theme.danger : root.status ? Theme.success : Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            // Scripts
            Repeater {
                model: root.tests.scripts
                RowLayout {
                    required property var modelData
                    required property int index
                    spacing: 8
                    Layout.fillWidth: true
                    Label {
                        text: "📄 " + modelData.name
                        color: Theme.text
                        font.bold: true
                    }
                    Label {
                        text: modelData.error || qsTr("%1 test cases").arg(modelData.cases)
                        color: modelData.error ? Theme.danger : Theme.muted
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    ToolButton {
                        text: "✕"
                        enabled: !root.tests.running
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Remove the script")
                        onClicked: root.tests.remove_script(index)
                    }
                }
            }

            Label {
                visible: root.tests.scripts.length === 0
                text: qsTr("Load a test script: a Python file with TestModule classes (see examples/testing).")
                color: Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            // Modules and test cases
            ListView {
                clip: true
                spacing: 2
                model: root.tests.items
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    id: row
                    required property var modelData
                    readonly property bool isModule: modelData.kind === "module"
                    width: ListView.view.width - 6
                    highlighted: !isModule && modelData.key === root.tests.current
                    leftPadding: isModule ? 4 : 28
                    onClicked: if (!isModule) root.tests.show_case(modelData.key)
                    onDoubleClicked: if (!root.tests.running && root.connection.connected) root.tests.run_case(modelData.key)
                    ToolTip.visible: hovered && modelData.description.length > 0
                    ToolTip.text: modelData.description
                    ToolTip.delay: 600
                    contentItem: RowLayout {
                        spacing: 8
                        CheckBox {
                            checked: row.modelData.selected
                            enabled: !root.tests.running
                            padding: 0
                            onToggled: root.tests.set_selected(row.modelData.key, checked)
                        }
                        Label {
                            text: root.verdictSymbol(row.modelData.verdict)
                            color: root.verdictColor(row.modelData.verdict)
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            Layout.preferredWidth: 16
                        }
                        Label {
                            text: row.modelData.title
                            color: Theme.text
                            font.bold: row.isModule
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Label {
                            text: row.modelData.duration
                            color: Theme.muted
                            font.pixelSize: 12
                        }
                        ToolButton {
                            text: "▶"
                            font.pixelSize: 11
                            enabled: !root.tests.running && root.connection.connected
                            ToolTip.visible: hovered
                            ToolTip.text: row.isModule ? qsTr("Run this module") : qsTr("Run this test case")
                            onClicked: {
                                const error = root.tests.run_case(row.modelData.key)
                                if (error)
                                    root.show(error, true)
                            }
                        }
                    }
                }
            }
        }
    }

    // Details of the selected test case
    Card {
        title: root.details.title || qsTr("Details")
        Layout.preferredWidth: 100
        Layout.horizontalStretchFactor: 54
        Layout.fillWidth: true
        Layout.fillHeight: true
        headerItems: [
            Pill {
                visible: (root.details.verdict || "") !== ""
                text: root.details.verdict || ""
                tint: root.verdictColor(root.details.verdict)
            },
            Label {
                text: root.details.duration || ""
                color: Theme.muted
            }
        ]

        ColumnLayout {
            anchors.fill: parent
            spacing: 8

            Label {
                visible: (root.details.description || "") !== ""
                text: root.details.description || ""
                color: Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Label {
                visible: root.details.key === undefined
                text: qsTr("Select a test case to see its checks and log.")
                color: Theme.muted
            }
            Rectangle {
                visible: (root.details.error || "") !== ""
                radius: Theme.smallRadius
                color: Theme.alpha(Theme.warning, 0.12)
                border.color: Theme.alpha(Theme.warning, 0.5)
                Layout.fillWidth: true
                Layout.preferredHeight: errorText.implicitHeight + 20
                Label {
                    id: errorText
                    anchors.fill: parent
                    anchors.margins: 10
                    text: root.details.error || ""
                    color: Theme.text
                    font.family: "monospace"
                    wrapMode: Text.Wrap
                }
            }

            ListView {
                id: stepList
                clip: true
                spacing: 2
                model: root.details.steps || []
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                onCountChanged: if (root.tests.running) positionViewAtEnd()
                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: ListView.view.width - 6
                    height: stepText.implicitHeight + 10
                    radius: 6
                    color: modelData.passed === false ? Theme.alpha(Theme.danger, 0.12) : index % 2 ? Theme.alpha(Theme.cardAlt, 0.6) : "transparent"
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 8
                        anchors.rightMargin: 8
                        spacing: 10
                        Label {
                            text: modelData.time + " s"
                            color: Theme.muted
                            font.family: "monospace"
                            font.pixelSize: 12
                            horizontalAlignment: Text.AlignRight
                            Layout.preferredWidth: 72
                        }
                        Label {
                            text: modelData.passed === true ? "✔" : modelData.passed === false ? "✘"
                                : modelData.kind === "send" ? "→" : modelData.kind === "error" ? "!" : "·"
                            color: modelData.passed === true ? Theme.success : modelData.passed === false ? Theme.danger
                                : modelData.kind === "send" ? Theme.accent : Theme.muted
                            font.bold: true
                            Layout.preferredWidth: 14
                        }
                        Label {
                            id: stepText
                            text: modelData.text
                            color: Theme.text
                            font.family: modelData.kind === "send" ? "monospace" : Qt.application.font.family
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                    }
                }
            }
        }
    }

    FileDialog {
        id: scriptDialog
        title: qsTr("Load test script")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("Python test scripts (*.py)")]
        onAccepted: {
            const error = root.tests.load_script(selectedFile)
            if (error)
                root.show(error, true)
        }
    }

    FileDialog {
        id: reportDialog
        title: qsTr("Export test report")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTr("HTML report (*.html)"), qsTr("JUnit XML (*.xml)")]
        defaultSuffix: selectedNameFilter.extensions.length > 0 ? selectedNameFilter.extensions[0] : "html"
        onAccepted: {
            const message = root.tests.export_report(selectedFile)
            root.show(message, message.startsWith("Could"))
        }
    }
}
