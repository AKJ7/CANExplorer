import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// Interactive UDS discovery for reverse engineering: enumerate the services, data identifiers, subfunctions and
// routines an ECU answers to, or sweep one byte of a request. Uses the transport and addressing of the
// Diagnostics view. Only the interesting answers (positive, locked, wrong-session, rejected) are listed.
ColumnLayout {
    id: root
    required property var connection
    readonly property var scan: connection.uds_scan
    readonly property var diagnostics: connection.diagnostics
    readonly property string kind: kindBox.currentValue
    readonly property bool ready: connection.connected && !scan.running

    function report(error) {
        errorLabel.text = error
    }

    function categoryColor(category) {
        return category === "positive" ? Theme.success
             : category === "security" ? Theme.warning
             : category === "conditions" ? Theme.accent
             : category === "unresponsive" ? Theme.danger
             : Theme.chart[5]
    }

    function categoryLabel(category) {
        return category === "positive" ? qsTr("supported")
             : category === "security" ? qsTr("locked")
             : category === "conditions" ? qsTr("wrong state/session")
             : category === "unresponsive" ? qsTr("no response (hang?)")
             : qsTr("rejected")
    }

    function startScan() {
        report(scan.start({
            kind: root.kind,
            known_only: knownOnlyBox.checked,
            start: startField.text,
            end: endField.text,
            service: serviceField.text,
            control_type: controlBox.currentValue,
            template: templateField.text,
            position: Number(positionField.text),
            width: widthBox.currentIndex + 1,
            timeout_ms: Number(timeoutField.text),
            delay_ms: Number(delayField.text),
            enter_session: sessionBox.currentValue,
            allow_state_changing: allowBox.checked,
            count: Number(countField.text),
            fuzz_seed: fuzzSeedField.text,
        }))
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("UDS discovery over %1").arg(root.scan.transport_name); font.bold: true }
        Label { text: qsTr("Scan") }
        ComboBox {
            id: kindBox
            model: root.scan.kinds
            textRole: "name"
            valueRole: "value"
            currentIndex: 1
            Layout.preferredWidth: 220
        }

        // Services: known list or every SID
        CheckBox {
            id: knownOnlyBox
            text: qsTr("Known services only")
            checked: true
            visible: root.kind === "services"
        }

        // The service whose subfunctions are scanned
        Label { text: qsTr("Service"); visible: root.kind === "subfunctions" }
        TextField {
            id: serviceField
            text: "10"
            visible: root.kind === "subfunctions"
            Layout.preferredWidth: 50
        }

        // Routine control type
        ComboBox {
            id: controlBox
            visible: root.kind === "routines"
            model: [{ name: qsTr("request results (read-only)"), value: 3 },
                    { name: qsTr("start routine"), value: 1 },
                    { name: qsTr("stop routine"), value: 2 }]
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 200
        }

        // Sweep template and position
        Label { text: root.kind === "fuzz" ? qsTr("Seeds") : qsTr("Template")
                visible: root.kind === "sweep" || root.kind === "fuzz" }
        TextField {
            id: templateField
            text: "22 F1 90"
            placeholderText: root.kind === "fuzz" ? qsTr("seed requests, hex; ; separated (optional)")
                                                  : qsTr("request, hex")
            visible: root.kind === "sweep" || root.kind === "fuzz"
            Layout.preferredWidth: root.kind === "fuzz" ? 220 : 120
            ToolTip.visible: hovered && root.kind === "fuzz"
            ToolTip.text: qsTr("Requests to mutate (optional; empty fuzzes random requests from the known services)")
        }
        Label { text: qsTr("Byte"); visible: root.kind === "sweep" }
        TextField {
            id: positionField
            text: "1"
            visible: root.kind === "sweep"
            Layout.preferredWidth: 40
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Index of the first byte to replace (0 is the service byte)")
        }
        ComboBox {
            id: widthBox
            visible: root.kind === "sweep"
            model: [qsTr("1 byte"), qsTr("2 bytes")]
            Layout.preferredWidth: 90
        }

        Label { text: qsTr("Range"); visible: root.kind !== "fuzz" }
        TextField {
            id: startField
            text: "0000"
            placeholderText: qsTr("start")
            visible: root.kind !== "fuzz"
            Layout.preferredWidth: 70
        }
        Label { text: "–"; visible: root.kind !== "fuzz" }
        TextField {
            id: endField
            text: "FFFF"
            placeholderText: qsTr("end")
            visible: root.kind !== "fuzz"
            Layout.preferredWidth: 70
        }

        Label { text: qsTr("Iterations"); visible: root.kind === "fuzz" }
        TextField {
            id: countField
            text: "500"
            visible: root.kind === "fuzz"
            validator: IntValidator { bottom: 1; top: 1000000 }
            Layout.preferredWidth: 80
        }
        Label { text: qsTr("Seed"); visible: root.kind === "fuzz" }
        TextField {
            id: fuzzSeedField
            text: "1"
            placeholderText: qsTr("reproducible")
            visible: root.kind === "fuzz"
            Layout.preferredWidth: 70
            ToolTip.visible: hovered
            ToolTip.text: qsTr("A number making the run reproducible: the same seed replays the same requests")
        }
        Item { Layout.fillWidth: true }
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.leftMargin: 4
        Layout.rightMargin: 4

        Label { text: qsTr("Enter session") }
        ComboBox {
            id: sessionBox
            model: [{ name: qsTr("keep current"), value: "" }].concat(root.diagnostics.sessions.map(
                s => ({ name: s.name, value: s.value.toString(16) })))
            textRole: "name"
            valueRole: "value"
            Layout.preferredWidth: 160
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Change to this diagnostic session before scanning, to reach services and DIDs that need it")
        }
        Label { text: qsTr("Per-request timeout") }
        TextField {
            id: timeoutField
            text: "200"
            Layout.preferredWidth: 60
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Milliseconds to wait for each response; smaller is faster but may miss slow ECUs")
        }
        Label { text: qsTr("ms, delay") }
        TextField {
            id: delayField
            text: "0"
            Layout.preferredWidth: 50
        }
        Label { text: qsTr("ms") }
        CheckBox {
            id: allowBox
            text: qsTr("Allow state-changing probes")
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Needed for the byte sweep and for starting/stopping routines, which can change the ECU")
        }
        Item { Layout.fillWidth: true }

        Button {
            text: root.scan.running ? qsTr("Stop") : qsTr("Start")
            highlighted: !root.scan.running
            enabled: root.scan.running || root.connection.connected
            onClicked: {
                report("")
                if (root.scan.running)
                    root.scan.stop()
                else
                    root.startScan()
            }
        }
        Button {
            text: qsTr("Clear")
            enabled: !root.scan.running && root.scan.found > 0
            onClicked: root.scan.clear()
        }
        BusyIndicator {
            running: root.scan.running
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
    }

    // Authorisation reminder: this probes an ECU and some probes change its state
    Label {
        text: qsTr("Scan only ECUs you own or are authorised to test. Probing changes nothing by default, but a byte sweep or starting routines can. Keep the vehicle stationary and the power stable.")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
        Layout.rightMargin: 4
    }

    Label {
        id: errorLabel
        color: Theme.danger
        visible: text.length > 0
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.leftMargin: 4
        Layout.rightMargin: 4

        ProgressBar {
            from: 0
            to: 1
            value: root.scan.progress
            Layout.fillWidth: true
        }
        Label {
            text: qsTr("%1 of %2 probed · %3 found").arg(root.scan.scanned.toLocaleString(Qt.locale(), "f", 0))
                      .arg(root.scan.total.toLocaleString(Qt.locale(), "f", 0))
                      .arg(root.scan.found.toLocaleString(Qt.locale(), "f", 0))
            color: Theme.muted
        }
    }

    Card {
        Layout.fillWidth: true
        Layout.fillHeight: true
        padding: 1

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            ListView {
                id: resultList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.scan.results
                delegate: ItemDelegate {
                    id: row
                    required property var modelData
                    width: resultList.width
                    height: 40
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Click to send this request in the Diagnostics view")
                    onClicked: root.report(root.scan.inspect(modelData.request))

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10
                        spacing: 10

                        Rectangle {
                            width: 10
                            height: 10
                            radius: 5
                            color: root.categoryColor(row.modelData.category)
                        }
                        Label {
                            text: row.modelData.identifier
                            font.family: "monospace"
                            elide: Text.ElideRight
                            Layout.preferredWidth: 280
                        }
                        Label {
                            text: root.categoryLabel(row.modelData.category)
                            color: root.categoryColor(row.modelData.category)
                            Layout.preferredWidth: 130
                        }
                        Label {
                            text: row.modelData.summary
                            font.family: "monospace"
                            color: Theme.muted
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                    }
                }
            }

            Label {
                visible: root.scan.truncated
                text: qsTr("Showing the first %1 results; narrow the range to see the rest.").arg(2000)
                color: Theme.warning
                padding: 6
                Layout.fillWidth: true
            }
            Label {
                visible: root.scan.found === 0 && !root.scan.running && root.scan.state === "done"
                text: qsTr("Nothing answered in this range.")
                color: Theme.muted
                padding: 6
            }
        }
    }
}
