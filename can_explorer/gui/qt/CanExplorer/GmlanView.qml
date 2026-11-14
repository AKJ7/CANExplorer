import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// GMLAN (GMW3110) diagnostics: the GM/Opel diagnostic protocol, over ISO-TP (USDT) on this CAN bus. Useful on a
// GMLAN single-wire (SWCAN) connection, but works on any CAN connection that can send. Laid out like the UDS page:
// a header with the state, the services grouped into sections on the left, the trouble codes above the history on
// the right.
ColumnLayout {
    id: root
    required property var connection
    readonly property var gmlan: connection.gmlan
    readonly property bool connected: connection.connected
    readonly property bool ready: connected && !gmlan.busy
    property string errorText: ""

    function report(error) {
        errorText = error || ""
    }

    function applyAddressing() {
        report(gmlan.set_addressing(requestIdField.text, responseIdField.text, extendedBox.checked))
    }

    function fileName(path) {
        return path.replace(/^keystore:/, "").split(/[\\/]/).pop()
    }

    // A titled group of related services in the left column
    component Section: Rectangle {
        id: section
        property string title
        default property alias content: sectionBody.data

        Layout.fillWidth: true
        implicitHeight: sectionColumn.implicitHeight + 24
        radius: Theme.smallRadius
        color: Theme.cardAlt
        border.color: Theme.alpha(Theme.border, 0.6)
        border.width: 1

        ColumnLayout {
            id: sectionColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 12
            spacing: 8

            Label {
                text: section.title.toUpperCase()
                color: Theme.muted
                font.pixelSize: 11
                font.bold: true
                font.letterSpacing: 0.8
            }
            ColumnLayout {
                id: sectionBody
                Layout.fillWidth: true
                spacing: 6
            }
        }
    }

    // A small muted caption above or beside a field
    component FieldLabel: Label {
        color: Theme.muted
        font.pixelSize: 12
    }

    // Shown in place of an empty list
    component EmptyHint: Label {
        Layout.fillWidth: true
        Layout.fillHeight: true
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.WordWrap
        color: Theme.muted
    }

    spacing: 10

    // --- Header: the protocol and what this page has done to the ECU ---
    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Label {
            text: qsTr("GMLAN diagnostics")
            color: Theme.text
            font.pixelSize: Theme.titleSize
            font.bold: true
        }
        Pill {
            text: qsTr("USDT")
            ToolTip.visible: usdtHover.hovered
            ToolTip.text: qsTr("GMLAN's transport, the same segmentation as ISO-TP")
            HoverHandler { id: usdtHover }
        }
        Pill {
            visible: root.gmlan.single_wire
            text: qsTr("Single-wire")
            tint: Theme.chart[4]
        }
        Pill {
            text: root.connected ? qsTr("Connected") : qsTr("Not connected")
            tint: root.connected ? Theme.success : Theme.muted
        }
        Pill {
            visible: root.gmlan.operation !== ""
            text: root.gmlan.operation
            tint: Theme.accent
            ToolTip.visible: operationHover.hovered
            ToolTip.text: qsTr("Diagnostic operation initiated (0x10); Return to normal ends it")
            HoverHandler { id: operationHover }
        }
        Pill {
            visible: root.gmlan.communication_disabled
            text: qsTr("Normal comms off")
            tint: Theme.warning
        }
        Pill {
            visible: root.gmlan.unlocked_level > 0
            text: qsTr("Unlocked · level %1").arg(root.gmlan.unlocked_level.toString(16).toUpperCase())
            tint: Theme.success
        }
        Pill {
            visible: root.gmlan.tester_present
            text: qsTr("Tester present")
            tint: Theme.success
        }
        Item { Layout.fillWidth: true }
        BusyIndicator {
            running: root.gmlan.busy
            visible: running
            Layout.preferredHeight: 24
            Layout.preferredWidth: 24
        }
        Label {
            visible: root.gmlan.busy
            text: qsTr("Waiting for the ECU…")
            color: Theme.muted
        }
    }

    // --- Error banner ---
    Rectangle {
        visible: root.errorText.length > 0
        Layout.fillWidth: true
        implicitHeight: errorRow.implicitHeight + 12
        radius: Theme.smallRadius
        color: Theme.alpha(Theme.danger, 0.12)
        border.color: Theme.alpha(Theme.danger, 0.6)
        border.width: 1

        RowLayout {
            id: errorRow
            anchors.fill: parent
            anchors.leftMargin: 10
            anchors.rightMargin: 4
            spacing: 8

            StatusDot { status: root.errorText ? "error" : "" }
            Label {
                text: root.errorText
                color: Theme.text
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            ToolButton {
                text: "✕"
                flat: true
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Dismiss")
                onClicked: root.errorText = ""
            }
        }
    }

    SplitView {
        Layout.fillWidth: true
        Layout.fillHeight: true

        // --- Services ---
        ScrollView {
            id: servicesScroll
            SplitView.preferredWidth: 380
            SplitView.minimumWidth: 300
            contentWidth: availableWidth
            clip: true

            ColumnLayout {
                width: servicesScroll.availableWidth - 8
                spacing: 10

                Section {
                    title: qsTr("Target ECU")

                    GridLayout {
                        columns: 2
                        columnSpacing: 8
                        rowSpacing: 2
                        Layout.fillWidth: true

                        FieldLabel { text: qsTr("Request ID") }
                        FieldLabel { text: qsTr("Response ID") }
                        TextField {
                            id: requestIdField
                            text: root.gmlan.request_id
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The tester's physical request identifier (hex); functional requests to all nodes go on 101")
                            onEditingFinished: root.applyAddressing()
                        }
                        TextField {
                            id: responseIdField
                            text: root.gmlan.response_id
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The ECU's response identifier (hex), commonly the request plus 400")
                            onEditingFinished: root.applyAddressing()
                        }
                    }
                    CheckBox {
                        id: extendedBox
                        text: qsTr("Extended (29-bit) identifiers")
                        checked: root.gmlan.extended
                        onToggled: root.applyAddressing()
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Programmed state"); Layout.fillWidth: true }
                        Button {
                            text: qsTr("Report")
                            enabled: root.ready
                            Layout.minimumWidth: 90  // Grows to fit longer translations
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("ReportProgrammedState (0xA2): whether the ECU's software and calibrations are programmed")
                            onClicked: root.report(root.gmlan.report_programmed_state())
                        }
                    }
                }

                Section {
                    title: qsTr("Diagnostic mode")

                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: operationBox
                            model: root.gmlan.operations
                            textRole: "name"
                            valueRole: "level"
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Initiate")
                            enabled: root.ready
                            Layout.minimumWidth: 90  // Grows to fit longer translations
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("InitiateDiagnosticOperation (0x10)")
                            onClicked: root.report(root.gmlan.initiate_operation(operationBox.currentValue))
                        }
                    }
                    // One per row: their labels are long in some languages
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Button {
                            text: qsTr("Disable normal comms")
                            enabled: root.ready
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("DisableNormalCommunication (0x28): stop the ECU's normal application messages")
                            onClicked: root.report(root.gmlan.disable_normal_communication())
                        }
                        Button {
                            text: qsTr("Return to normal")
                            enabled: root.ready
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("ReturnToNormalMode (0x20): end diagnostics and resume normal communication")
                            onClicked: root.report(root.gmlan.return_to_normal())
                        }
                    }
                    Switch {
                        text: qsTr("Tester present")
                        checked: root.gmlan.tester_present
                        enabled: root.connected
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Periodically send TesterPresent (0x3E) so the ECU stays in diagnostics")
                        onToggled: if (checked !== root.gmlan.tester_present) root.report(root.gmlan.toggle_tester_present())
                    }
                }

                Section {
                    title: qsTr("Data identifiers")

                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("DID") }
                        TextField {
                            id: didField
                            text: "90"
                            placeholderText: qsTr("hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("One-byte GMLAN data identifier (hex), e.g. 90 for the VIN")
                            onAccepted: if (root.ready) root.report(root.gmlan.read_did(text))
                        }
                        Button {
                            text: qsTr("Read")
                            enabled: root.ready
                            Layout.minimumWidth: 90  // Grows to fit longer translations
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("ReadDataByIdentifier (0x1A)")
                            onClicked: root.report(root.gmlan.read_did(didField.text))
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: writeField
                            placeholderText: qsTr("Data to write, hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Write")
                            enabled: root.ready
                            Layout.minimumWidth: 90  // Grows to fit longer translations
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("WriteDataByIdentifier (0x3B)")
                            onClicked: root.report(root.gmlan.write_did(didField.text, writeField.text))
                        }
                    }
                }

                Section {
                    title: qsTr("Security access")

                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Level") }
                        SpinBox {
                            id: levelBox
                            from: 1
                            to: 0xFD
                            stepSize: 2
                            value: 1
                            textFromValue: value => value.toString(16).toUpperCase()
                            valueFromText: text => parseInt(text, 16) || 1
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The odd level that requests the seed (hex)")
                        }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Unlock")
                            enabled: root.ready
                            Layout.minimumWidth: 90  // Grows to fit longer translations
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("SecurityAccess (0x27): request the seed and send the key")
                            onClicked: root.report(root.gmlan.security_access(levelBox.value.toString(16)))
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Button {
                            Layout.fillWidth: true
                            text: root.gmlan.key_file
                                ? qsTr("Key algorithm: %1").arg(root.fileName(root.gmlan.key_file))
                                : qsTr("Choose key algorithm…")
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Python file defining compute_key(level: int, seed: bytes) -> bytes")
                            onClicked: keyDialog.open()
                        }
                        SeedKeyButton { Layout.fillWidth: true; onChosen: source => root.gmlan.set_key_source(source) }
                    }
                }

                Section {
                    title: qsTr("Raw request")

                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: rawField
                            text: "1A 90"
                            placeholderText: qsTr("GMLAN request, hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                            onAccepted: if (root.ready) root.report(root.gmlan.raw_request(text))
                        }
                        Button {
                            text: qsTr("Send")
                            enabled: root.ready
                            Layout.minimumWidth: 90  // Grows to fit longer translations
                            onClicked: root.report(root.gmlan.raw_request(rawField.text))
                        }
                    }
                }
            }
        }

        SplitView {
            orientation: Qt.Vertical
            SplitView.fillWidth: true

            // --- Trouble codes ---
            ColumnLayout {
                SplitView.preferredHeight: 240
                SplitView.minimumHeight: 120
                spacing: 6

                RowLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: 8
                    spacing: 8

                    Label { text: qsTr("Trouble codes"); color: Theme.text; font.bold: true }
                    Pill {
                        text: String(root.gmlan.dtcs.length)
                        tint: root.gmlan.dtcs.length > 0 ? Theme.warning : Theme.muted
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: qsTr("Read")
                        enabled: root.ready
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("ReadDiagnosticInformation (0xA9) by status mask")
                        onClicked: root.report(root.gmlan.read_dtcs())
                    }
                    Button {
                        text: qsTr("Clear")
                        enabled: root.ready
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Clear all diagnostic information in the ECU (0x04)")
                        onClicked: root.report(root.gmlan.clear_dtcs())
                    }
                }

                EmptyHint {
                    visible: dtcList.count === 0
                    text: qsTr("No trouble codes. Press Read to query the ECU.")
                }
                ListView {
                    id: dtcList
                    visible: count > 0
                    clip: true
                    spacing: 2
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ScrollBar.vertical: ScrollBar {}
                    model: root.gmlan.dtcs

                    delegate: Rectangle {
                        id: dtcRow
                        required property var modelData

                        width: ListView.view.width - 10
                        implicitHeight: dtcLine.implicitHeight + 16
                        radius: Theme.smallRadius
                        color: dtcHover.hovered ? Theme.alpha(Theme.accent, 0.08) : "transparent"

                        HoverHandler { id: dtcHover }

                        RowLayout {
                            id: dtcLine
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 10
                            spacing: 10

                            StatusDot { status: "warning" }
                            Label {
                                text: dtcRow.modelData.code
                                color: Theme.text
                                font.family: "monospace"
                                font.bold: true
                            }
                            Label {
                                text: qsTr("status %1").arg(dtcRow.modelData.status)
                                color: Theme.muted
                                font.family: "monospace"
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            // --- History ---
            ColumnLayout {
                SplitView.fillHeight: true
                SplitView.minimumHeight: 120
                spacing: 6

                RowLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: 8
                    Layout.topMargin: 6
                    spacing: 8

                    Label { text: qsTr("History"); color: Theme.text; font.bold: true }
                    Pill {
                        text: String(history.count)
                        tint: Theme.muted
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: qsTr("Clear")
                        flat: true
                        enabled: history.count > 0
                        onClicked: root.gmlan.clear_history()
                    }
                }

                EmptyHint {
                    visible: history.count === 0
                    text: root.connected
                        ? qsTr("Requests and the ECU's responses appear here.")
                        : qsTr("Connect to the bus first, then set the ECU's request and response identifiers.")
                }
                ListView {
                    id: history
                    visible: count > 0
                    clip: true
                    spacing: 6
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ScrollBar.vertical: ScrollBar {}
                    model: root.gmlan.history
                    onCountChanged: positionViewAtEnd()

                    delegate: Rectangle {
                        id: entry
                        required property var modelData
                        required property int index
                        readonly property string outcome: modelData.positive ? "ok" : modelData.error ? "error" : "warning"
                        readonly property color tint: modelData.positive ? Theme.success
                            : modelData.error ? Theme.danger : Theme.warning

                        width: ListView.view.width - 10
                        x: 2
                        implicitHeight: entryColumn.implicitHeight + 16
                        radius: Theme.smallRadius
                        color: Theme.cardAlt
                        border.color: Theme.alpha(Theme.border, 0.6)
                        border.width: 1

                        // Outcome stripe
                        Rectangle {
                            x: 6
                            y: 8
                            width: 3
                            height: parent.height - 16
                            radius: 1.5
                            color: entry.tint
                        }

                        ColumnLayout {
                            id: entryColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.leftMargin: 18
                            anchors.rightMargin: 10
                            anchors.topMargin: 8
                            spacing: 3

                            // Request
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                Label { text: "→"; color: Theme.muted; font.family: "monospace" }
                                Label {
                                    text: entry.modelData.request_description || entry.modelData.request
                                    color: Theme.text
                                    font.bold: true
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Label {
                                    text: `#${entry.index + 1}`
                                    color: Theme.muted
                                    font.pixelSize: 11
                                }
                            }
                            Label {
                                visible: entry.modelData.request_description !== ""
                                text: entry.modelData.request
                                color: Theme.muted
                                font.family: "monospace"
                                wrapMode: Text.WrapAnywhere
                                Layout.fillWidth: true
                                Layout.leftMargin: 20
                            }

                            // Response
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.topMargin: 3
                                spacing: 8
                                Item {
                                    implicitWidth: 12
                                    implicitHeight: 12
                                    StatusDot { anchors.centerIn: parent; status: entry.outcome }
                                }
                                Label {
                                    text: entry.modelData.description
                                    color: Theme.text
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                            }
                            Label {
                                visible: entry.modelData.response !== ""
                                text: "← " + entry.modelData.response
                                color: Theme.muted
                                font.family: "monospace"
                                wrapMode: Text.WrapAnywhere
                                Layout.fillWidth: true
                                Layout.leftMargin: 20
                            }
                        }
                    }
                }
            }
        }
    }

    FileChooser {
        id: keyDialog
        title: qsTr("Seed/key algorithm")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: root.gmlan.set_key_file(selectedFile)
    }
}
