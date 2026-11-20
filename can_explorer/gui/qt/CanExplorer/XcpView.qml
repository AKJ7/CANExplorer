import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// XCP/CCP measurement and calibration: a header with the session's state, the slave and its tools grouped into
// sections on the left (connection, seed & key, measuring, pages, memory, datasets), and the A2L's variables above
// the log on the right. Curves, maps and blocks open in an editor.
ColumnLayout {
    id: root
    required property var connection
    readonly property var xcp: connection.xcp
    readonly property bool connected: xcp.connected
    readonly property bool ready: connected && !xcp.busy
    readonly property string protocol: protocolBox.currentText
    readonly property string transport: protocol === "CCP" ? "CAN" : transportBox.currentText
    readonly property bool ethernet: transport === "UDP" || transport === "TCP"
    readonly property var eventNames: [qsTr("Poll")].concat(xcp.events.map(e => e.name))
    readonly property var stimNames: [qsTr("Direct")].concat(xcp.events.map(e => qsTr("STIM %1").arg(e.name)))
    readonly property var kindFilters: [qsTr("All"), qsTr("Measurements"), qsTr("Characteristics"), qsTr("Plotted"), qsTr("By DAQ")]
    readonly property var shownVariables: xcp.variables.filter(row => matches(row))
    readonly property int daqCount: xcp.variables.filter(row => row.daq && row.acquisition > 0).length
    readonly property int stimCount: xcp.variables.filter(row => row.stim && row.acquisition > 0).length
    property string errorText: ""

    // Column widths of the variable table (the header and every row use the same)
    readonly property int valueWidth: 170
    readonly property int typeWidth: 104
    readonly property int measureWidth: 100
    readonly property int plotWidth: 36
    readonly property int setWidth: 150
    // Narrow: the type and address move under the name (in place of the description)
    readonly property bool compact: variableList.width < 840

    function report(error) {
        errorText = error
    }

    function fileName(path) {
        return path.replace(/^(keystore|profile):/, "").split(/[\\/]/).pop()
    }

    function matches(row) {
        const text = filterField.text.trim().toLowerCase()
        if (text && row.name.toLowerCase().indexOf(text) < 0 && row.description.toLowerCase().indexOf(text) < 0)
            return false
        switch (kindBox.currentIndex) {
        case 1: return row.kind === "MEASUREMENT"
        case 2: return row.kind !== "MEASUREMENT"
        case 3: return row.plotted
        case 4: return row.acquisition > 0
        }
        return true
    }

    function connectSlave() {
        report(root.xcp.connect_slave({
            protocol: protocolBox.currentText,
            transport: root.transport,
            request_id: requestField.text,
            response_id: responseField.text,
            extended: extendedBox.checked,
            station_address: stationField.text,
            byte_order: byteOrderBox.currentIndex === 1 ? "big" : "little",
            host: hostField.text,
            port: portField.text,
        }))
    }

    function applyA2lSettings() {
        const settings = root.xcp.a2l_settings()
        if (Object.keys(settings).length === 0) {
            report(qsTr("The A2L has no XCP or CCP interface settings"))
            return
        }
        protocolBox.currentIndex = Math.max(0, root.xcp.protocols.indexOf(settings.protocol))
        if (settings.transport)
            transportBox.currentIndex = Math.max(0, root.xcp.transports.indexOf(settings.transport))
        if (settings.request_id !== undefined) requestField.text = settings.request_id
        if (settings.response_id !== undefined) responseField.text = settings.response_id
        if (settings.extended !== undefined) extendedBox.checked = settings.extended
        if (settings.station_address !== undefined) stationField.text = settings.station_address
        if (settings.host !== undefined) hostField.text = settings.host
        if (settings.port !== undefined) portField.text = String(settings.port)
        report("")
    }

    function levelColor(level) {
        return level === "ok" ? Theme.success : level === "warning" ? Theme.warning : level === "error" ? Theme.danger : Theme.text
    }

    // A titled group of related controls in the left column
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

    // A small muted caption above a field
    component FieldLabel: Label {
        color: Theme.muted
        font.pixelSize: 12
    }

    // A muted column title of the variable table
    component HeaderLabel: Label {
        color: Theme.muted
        font.pixelSize: 12
        elide: Text.ElideRight
    }

    spacing: 10

    // --- Header ---
    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Label {
            text: qsTr("Measurement & calibration")
            color: Theme.text
            font.pixelSize: Theme.titleSize
            font.bold: true
        }
        Pill {
            text: root.protocol === "CCP" ? "CCP" : "XCP · " + root.transport
        }
        Pill {
            text: root.connected ? qsTr("Connected") : qsTr("Not connected")
            tint: root.connected ? Theme.success : Theme.muted
        }
        Pill {
            visible: root.xcp.a2l_file !== ""
            text: root.fileName(root.xcp.a2l_file)
            tint: Theme.accent
            ToolTip.visible: a2lHover.hovered
            ToolTip.text: root.xcp.a2l_file
            HoverHandler { id: a2lHover }
        }
        Pill {
            visible: root.xcp.info.EPK !== undefined
            readonly property bool matching: (root.xcp.info.EPK || "").startsWith("matches")
            text: matching ? qsTr("EPK ok") : qsTr("EPK mismatch")
            tint: matching ? Theme.success : Theme.danger
            ToolTip.visible: epkHover.hovered
            ToolTip.text: matching
                ? qsTr("The A2L's build identifier matches the ECU")
                : qsTr("The A2L does not describe this ECU software: %1").arg(root.xcp.info.EPK || "")
            HoverHandler { id: epkHover }
        }
        Pill {
            visible: root.connected && root.xcp.info.locked !== undefined && root.xcp.info.locked !== "none"
            text: qsTr("Locked: %1").arg(root.xcp.info.locked || "")
            tint: Theme.warning
        }
        Pill {
            visible: root.xcp.daq_running
            text: qsTr("DAQ running")
            tint: Theme.success
        }
        Item { Layout.fillWidth: true }
        BusyIndicator {
            running: root.xcp.busy
            visible: running
            Layout.preferredHeight: 24
            Layout.preferredWidth: 24
        }
        Label {
            visible: root.xcp.busy
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
                id: errorLabel
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

        // --- Slave and tools ---
        ScrollView {
            id: toolsScroll
            SplitView.preferredWidth: 330
            SplitView.minimumWidth: 280
            contentWidth: availableWidth
            clip: true

            ColumnLayout {
                width: toolsScroll.availableWidth - 8
                spacing: 10

                Section {
                    title: qsTr("Slave")

                    GridLayout {
                        columns: 2
                        columnSpacing: 8
                        rowSpacing: 2
                        Layout.fillWidth: true

                        FieldLabel { text: qsTr("Protocol") }
                        FieldLabel { text: qsTr("Transport") }
                        ComboBox {
                            id: protocolBox
                            model: root.xcp.protocols
                            enabled: !root.connected
                            Layout.fillWidth: true
                        }
                        ComboBox {
                            id: transportBox
                            model: root.protocol === "CCP" ? ["CAN"] : root.xcp.transports
                            enabled: !root.connected && root.protocol === "XCP"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("XCP on CAN, CAN FD, or Ethernet (UDP or TCP); CCP runs on CAN")
                        }

                        FieldLabel { text: qsTr("Command ID"); visible: !root.ethernet }
                        FieldLabel { text: qsTr("Response ID"); visible: !root.ethernet }
                        TextField {
                            id: requestField
                            text: "7E0"
                            visible: !root.ethernet
                            enabled: !root.connected
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("CAN identifier (hex) the master sends commands on (XCP CMD / CCP CRO)")
                        }
                        TextField {
                            id: responseField
                            text: "7E1"
                            visible: !root.ethernet
                            enabled: !root.connected
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("CAN identifier (hex) the slave answers on (XCP RES / CCP DTO)")
                        }

                        FieldLabel { text: qsTr("Host"); visible: root.ethernet }
                        FieldLabel { text: qsTr("Port"); visible: root.ethernet }
                        TextField {
                            id: hostField
                            text: "127.0.0.1"
                            visible: root.ethernet
                            enabled: !root.connected
                            Layout.fillWidth: true
                        }
                        TextField {
                            id: portField
                            text: "5555"
                            visible: root.ethernet
                            enabled: !root.connected
                            validator: IntValidator { bottom: 1; top: 65535 }
                            Layout.fillWidth: true
                        }

                        FieldLabel { text: qsTr("Station address"); visible: root.protocol === "CCP" }
                        FieldLabel { text: qsTr("Byte order"); visible: root.protocol === "CCP" }
                        TextField {
                            id: stationField
                            text: "0"
                            visible: root.protocol === "CCP"
                            enabled: !root.connected
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("CCP station address (hex) selecting the ECU on a shared bus")
                        }
                        ComboBox {
                            id: byteOrderBox
                            visible: root.protocol === "CCP"
                            enabled: !root.connected
                            model: [qsTr("Intel"), qsTr("Motorola")]
                            currentIndex: 1
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Byte order of the ECU's addresses and values (XCP reports its own)")
                        }
                    }
                    CheckBox {
                        id: extendedBox
                        text: qsTr("Extended (29-bit) identifiers")
                        visible: !root.ethernet
                        enabled: !root.connected
                    }
                    Button {
                        Layout.fillWidth: true
                        text: root.connected ? qsTr("Disconnect") : qsTr("Connect")
                        highlighted: !root.connected
                        enabled: (root.connected || root.connection.connected || root.ethernet) && !root.xcp.busy
                        onClicked: {
                            root.report("")
                            if (root.connected)
                                root.xcp.disconnect_slave()
                            else
                                root.connectSlave()
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            Layout.fillWidth: true
                            text: root.xcp.a2l_file ? qsTr("Unload A2L") : qsTr("Load A2L…")
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The A2L file describes the ECU's measurements and calibration values")
                            onClicked: root.xcp.a2l_file ? root.xcp.unload_a2l() : a2lDialog.open()
                        }
                        Button {
                            Layout.fillWidth: true
                            text: qsTr("Settings from A2L")
                            enabled: root.xcp.a2l_file.length > 0 && !root.connected
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Fill in the protocol, transport and identifiers from the A2L's IF_DATA")
                            onClicked: root.applyA2lSettings()
                        }
                    }
                    // What the slave reported
                    GridLayout {
                        visible: root.connected && root.xcp.details.length > 0
                        columns: 2
                        columnSpacing: 10
                        rowSpacing: 2
                        Layout.fillWidth: true
                        Layout.topMargin: 4

                        Repeater {
                            model: root.xcp.details
                            delegate: FieldLabel {
                                required property var modelData
                                required property int index
                                text: modelData.label.charAt(0).toUpperCase() + modelData.label.slice(1)
                                Layout.row: index
                                Layout.column: 0
                            }
                        }
                        Repeater {
                            model: root.xcp.details
                            delegate: Label {
                                required property var modelData
                                required property int index
                                text: modelData.value
                                color: root.levelColor(modelData.level)
                                font.pixelSize: 12
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                                Layout.row: index
                                Layout.column: 1
                            }
                        }
                    }
                }

                Section {
                    title: qsTr("Seed & key")

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        color: Theme.muted
                        font.pixelSize: 12
                        text: root.xcp.key_source
                            ? qsTr("Algorithm: %1. Locked resources are unlocked when a command needs them.").arg(root.fileName(root.xcp.key_source))
                            : qsTr("No algorithm: commands on locked resources are refused.")
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            Layout.fillWidth: true
                            text: qsTr("Python file…")
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("A Python file defining compute_key(level, seed) -> bytes; level is the resource to unlock (XCP: 1 CAL/PAG, 4 DAQ, 16 PGM; CCP: 1 CAL, 2 DAQ, 64 PGM)")
                            onClicked: keyDialog.open()
                        }
                        SeedKeyButton {
                            Layout.fillWidth: true
                            onChosen: source => root.xcp.set_key_source(source)
                        }
                    }
                    Button {
                        Layout.fillWidth: true
                        text: qsTr("Unlock all")
                        enabled: root.ready && root.xcp.key_source.length > 0
                        onClicked: root.report(root.xcp.unlock())
                    }
                }

                Section {
                    title: qsTr("Measuring")

                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Poll every") }
                        SpinBox {
                            from: 50
                            to: 5000
                            stepSize: 50
                            value: 200
                            editable: true
                            Layout.fillWidth: true
                            onValueModified: root.xcp.set_poll_interval(value)
                        }
                        FieldLabel { text: qsTr("ms") }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        color: Theme.muted
                        font.pixelSize: 12
                        text: root.daqCount > 0 || root.stimCount > 0
                            ? qsTr("%n measurement(s) set to a DAQ event.", "", root.daqCount)
                              + (root.stimCount > 0 ? " " + qsTr("%n value(s) written by STIM.", "", root.stimCount) : "")
                            : qsTr("For synchronous measurement, set a measurement's Measure column to a DAQ event; to write a value at an event, set it to STIM.")
                    }
                    Button {
                        Layout.fillWidth: true
                        text: root.xcp.daq_running ? qsTr("Stop DAQ") : qsTr("Start DAQ")
                        highlighted: root.xcp.daq_running
                        enabled: root.ready && (root.xcp.daq_running || root.daqCount > 0 || root.stimCount > 0)
                        onClicked: {
                            if (root.xcp.daq_running)
                                root.xcp.stop_daq()
                            else
                                root.report(root.xcp.start_daq())
                        }
                    }
                }

                Section {
                    title: qsTr("Calibration pages")

                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: pageField
                            text: "0"
                            font.family: "monospace"
                            Layout.fillWidth: true
                            placeholderText: root.protocol === "CCP" ? qsTr("page address, hex") : qsTr("page number")
                            ToolTip.visible: hovered
                            ToolTip.text: root.protocol === "CCP"
                                ? qsTr("Start address (hex) of the calibration page to activate")
                                : qsTr("Calibration page for the ECU and the XCP access (e.g. 0 working, 1 reference)")
                        }
                        Button {
                            text: qsTr("Activate")
                            enabled: root.ready
                            onClicked: root.report(root.xcp.set_page(pageField.text))
                        }
                    }
                    Button {
                        Layout.fillWidth: true
                        visible: root.protocol === "XCP"
                        text: qsTr("Restore working page from reference")
                        enabled: root.ready
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("COPY_CAL_PAGE 1 → 0: copy the reference page over the working page, undoing every change")
                        onClicked: root.report(root.xcp.copy_page(1, 0))
                    }
                }

                Section {
                    title: qsTr("Flash programming")
                    visible: root.protocol === "XCP"

                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            text: qsTr("File…")
                            enabled: !root.xcp.flashing
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Intel HEX, Motorola S-record or binary (at address 0) file to program")
                            onClicked: imageDialog.open()
                        }
                        Label {
                            Layout.fillWidth: true
                            text: root.xcp.flash_file ? root.fileName(root.xcp.flash_file) : qsTr("No file")
                            color: root.xcp.flash_file ? Theme.text : Theme.muted
                            elide: Text.ElideMiddle
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: root.xcp.flash_image.length > 0
                        text: root.xcp.flash_image
                        wrapMode: Text.WrapAnywhere
                        color: Theme.muted
                        font.family: "monospace"
                        font.pixelSize: 11
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Erase") }
                        ComboBox {
                            id: eraseBox
                            Layout.fillWidth: true
                            readonly property var labels: ({
                                "sectors": qsTr("Sectors touched"),
                                "segments": qsTr("Image ranges only"),
                                "functional": qsTr("Functional areas"),
                                "none": qsTr("Nothing")
                            })
                            model: root.xcp.erase_modes.map(mode => labels[mode] || mode)
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Flash is erased a sector at a time: by default every sector the image touches (from GET_SECTOR_INFO)")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: root.xcp.erase_modes[eraseBox.currentIndex] === "functional"
                        CheckBox { id: calibrationAreaBox; text: qsTr("Calibration") }
                        CheckBox { id: codeAreaBox; text: qsTr("Code"); checked: true }
                        CheckBox { id: nvramAreaBox; text: qsTr("NVRAM") }
                    }
                    CheckBox {
                        id: verifyBox
                        text: qsTr("Verify by checksum")
                        checked: true
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("BUILD_CHECKSUM over each programmed range, compared with the file")
                    }
                    CheckBox {
                        id: resetBox
                        text: qsTr("Reset the ECU afterwards")
                        checked: true
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("PROGRAM_RESET starts the new software and ends the session")
                    }
                    CheckBox {
                        id: advancedBox
                        text: qsTr("More options")
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        visible: advancedBox.checked
                        spacing: 6

                        RowLayout {
                            Layout.fillWidth: true
                            FieldLabel { text: qsTr("Flash kernel") }
                            Label {
                                Layout.fillWidth: true
                                text: root.xcp.kernel_file ? root.fileName(root.xcp.kernel_file) : qsTr("None")
                                color: root.xcp.kernel_file ? Theme.text : Theme.muted
                                elide: Text.ElideMiddle
                            }
                            Button {
                                text: root.xcp.kernel_file ? qsTr("Remove") : qsTr("File…")
                                ToolTip.visible: hovered
                                ToolTip.text: qsTr("Code the ECU needs in RAM to program its flash: announced with PROGRAM_PREPARE and downloaded first")
                                onClicked: root.xcp.kernel_file ? root.report(root.xcp.load_kernel("")) : kernelDialog.open()
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            FieldLabel { text: qsTr("Compression") }
                            TextField {
                                id: compressionField
                                text: "00"
                                font.family: "monospace"
                                Layout.preferredWidth: 48
                                ToolTip.visible: hovered
                                ToolTip.text: qsTr("PROGRAM_FORMAT compression method (hex): 00 none, 80–FF the manufacturer's")
                            }
                            FieldLabel { text: qsTr("Encryption") }
                            TextField {
                                id: encryptionField
                                text: "00"
                                font.family: "monospace"
                                Layout.preferredWidth: 48
                                ToolTip.visible: hovered
                                ToolTip.text: qsTr("PROGRAM_FORMAT encryption method (hex): 00 none, 80–FF the manufacturer's")
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            FieldLabel { text: qsTr("Encoder") }
                            Label {
                                Layout.fillWidth: true
                                text: root.xcp.encoder_file ? root.fileName(root.xcp.encoder_file) : qsTr("None: the file is sent as it is")
                                color: root.xcp.encoder_file ? Theme.text : Theme.muted
                                elide: Text.ElideMiddle
                            }
                            Button {
                                text: root.xcp.encoder_file ? qsTr("Remove") : qsTr("Python file…")
                                ToolTip.visible: hovered
                                ToolTip.text: qsTr("A Python file defining encode(data, address) -> bytes that compresses or encrypts each range before it is sent")
                                onClicked: root.xcp.encoder_file ? root.report(root.xcp.set_encoder("")) : encoderDialog.open()
                            }
                        }
                        CheckBox {
                            id: programMaxBox
                            text: qsTr("Full packets as PROGRAM_MAX")
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            FieldLabel { text: qsTr("ECU check") }
                            ComboBox {
                                id: ecuCheckBox
                                Layout.fillWidth: true
                                readonly property var labels: ({
                                    "none": qsTr("None"),
                                    "internal": qsTr("The ECU's own"),
                                    "value": qsTr("Send a value")
                                })
                                model: root.xcp.ecu_checks.map(check => labels[check] || check)
                                ToolTip.visible: hovered
                                ToolTip.text: qsTr("PROGRAM_VERIFY after programming: the ECU runs its own check, or compares a value you give")
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            visible: ecuCheckBox.currentIndex > 0
                            FieldLabel { text: qsTr("Type") }
                            TextField {
                                id: checkTypeField
                                text: "0004"
                                font.family: "monospace"
                                Layout.preferredWidth: 56
                                ToolTip.visible: hovered
                                ToolTip.text: qsTr("Verification type (hex): 0001 calibration areas, 0002 code areas, 0004 complete flash")
                            }
                            FieldLabel { text: qsTr("Value"); visible: ecuCheckBox.currentIndex === 2 }
                            TextField {
                                id: checkValueField
                                visible: ecuCheckBox.currentIndex === 2
                                text: "00000000"
                                font.family: "monospace"
                                Layout.fillWidth: true
                            }
                        }
                    }
                    Button {
                        Layout.fillWidth: true
                        text: qsTr("Program")
                        highlighted: true
                        enabled: root.ready && root.xcp.can_program && root.xcp.flash_file.length > 0 && !root.xcp.daq_running
                        ToolTip.visible: hovered
                        ToolTip.text: root.xcp.can_program
                            ? qsTr("Erase, program and check the file in the ECU's flash (PGM)")
                            : qsTr("Connect to an ECU that offers programming (PGM)")
                        onClicked: root.report(root.xcp.program({
                            erase: root.xcp.erase_modes[eraseBox.currentIndex],
                            verify: verifyBox.checked,
                            reset: resetBox.checked,
                            clear_areas: (calibrationAreaBox.checked ? 1 : 0) | (codeAreaBox.checked ? 2 : 0) | (nvramAreaBox.checked ? 4 : 0),
                            compression: compressionField.text,
                            encryption: encryptionField.text,
                            program_max: programMaxBox.checked,
                            ecu_check: root.xcp.ecu_checks[ecuCheckBox.currentIndex],
                            check_type: checkTypeField.text,
                            check_value: checkValueField.text,
                        }))
                    }
                    ProgressBar {
                        Layout.fillWidth: true
                        visible: root.xcp.flash_steps.length > 0
                        value: root.xcp.flash_progress
                    }
                    Repeater {
                        model: root.xcp.flash_steps
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 6
                            Label {
                                text: modelData.status === "done" ? "✓" : modelData.status === "failed" ? "✕"
                                    : modelData.status === "running" ? "▶" : modelData.status === "skipped" ? "–" : "○"
                                color: modelData.status === "done" ? Theme.success : modelData.status === "failed" ? Theme.danger
                                    : modelData.status === "running" ? Theme.accent : Theme.muted
                                font.bold: true
                                Layout.preferredWidth: 14
                            }
                            Label {
                                Layout.fillWidth: true
                                text: modelData.detail ? modelData.name + " · " + modelData.detail : modelData.name
                                color: modelData.status === "failed" ? Theme.danger
                                    : modelData.status === "pending" || modelData.status === "skipped" ? Theme.muted : Theme.text
                                font.pixelSize: 12
                                wrapMode: Text.WordWrap
                            }
                        }
                    }
                }

                Section {
                    title: qsTr("Memory")

                    GridLayout {
                        columns: 2
                        columnSpacing: 8
                        rowSpacing: 2
                        Layout.fillWidth: true

                        FieldLabel { text: qsTr("Address (hex[:ext])") }
                        FieldLabel { text: qsTr("Bytes") }
                        TextField {
                            id: addressField
                            text: "1000"
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Address in hex, optionally with the address extension after a colon, e.g. 1000:1")
                        }
                        SpinBox {
                            id: sizeBox
                            from: 1
                            to: 0xFFFF
                            value: 16
                            editable: true
                            Layout.preferredWidth: 120
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            Layout.fillWidth: true
                            text: qsTr("Read")
                            enabled: root.ready
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Read up to 64 bytes into the log")
                            onClicked: root.report(root.xcp.read_address(addressField.text, sizeBox.value))
                        }
                        Button {
                            Layout.fillWidth: true
                            text: qsTr("Checksum")
                            enabled: root.ready
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("BUILD_CHECKSUM over the range; XCP also compares it with the uploaded bytes")
                            onClicked: root.report(root.xcp.checksum(addressField.text.split(":")[0], String(sizeBox.value)))
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: writeField
                            placeholderText: qsTr("Bytes to write, hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Write")
                            enabled: root.ready && writeField.text.trim().length > 0
                            onClicked: root.report(root.xcp.write_address(addressField.text, writeField.text))
                        }
                    }
                }

                Section {
                    title: qsTr("Dataset")

                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            Layout.fillWidth: true
                            text: qsTr("Save…")
                            enabled: root.ready && root.xcp.a2l_file.length > 0
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Save the ECU's calibration values (JSON, or DCM by the .dcm extension)")
                            onClicked: saveDatasetDialog.open()
                        }
                        Button {
                            Layout.fillWidth: true
                            text: qsTr("Compare…")
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Load a dataset (JSON or DCM) and compare it with the ECU")
                            onClicked: loadDatasetDialog.open()
                        }
                    }
                    Label {
                        visible: root.xcp.dataset_file.length > 0
                        Layout.fillWidth: true
                        elide: Text.ElideMiddle
                        font.pixelSize: 12
                        color: root.xcp.differences.length ? Theme.warning : Theme.success
                        text: root.xcp.differences.length
                            ? qsTr("%1: %n value(s) differ from the ECU", "", root.xcp.differences.length).arg(root.fileName(root.xcp.dataset_file))
                            : qsTr("%1: same as the ECU").arg(root.fileName(root.xcp.dataset_file))
                    }
                    Repeater {
                        model: root.xcp.differences
                        delegate: ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 0
                            Label { text: modelData.name; font.bold: true; font.pixelSize: 12 }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("file %1  ·  ECU %2").arg(modelData.file).arg(modelData.ecu)
                                font.family: "monospace"
                                font.pixelSize: 11
                                color: Theme.muted
                                elide: Text.ElideRight
                            }
                        }
                    }
                    Button {
                        Layout.fillWidth: true
                        visible: root.xcp.dataset_file.length > 0
                        text: qsTr("Write dataset to the ECU")
                        enabled: root.ready && root.xcp.differences.length > 0
                        onClicked: root.report(root.xcp.write_dataset())
                    }
                }
            }
        }

        // --- Variables and log ---
        SplitView {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 520
            orientation: Qt.Vertical

            Card {
                SplitView.fillHeight: true
                SplitView.minimumHeight: 200
                padding: 1

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0

                    // Toolbar
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.margins: 8
                        spacing: 8

                        Label { text: qsTr("Variables"); font.bold: true }
                        Rectangle {
                            implicitWidth: countLabel.implicitWidth + 14
                            implicitHeight: 22
                            radius: 11
                            color: Theme.alpha(Theme.muted, 0.18)
                            Label {
                                id: countLabel
                                anchors.centerIn: parent
                                text: root.shownVariables.length === root.xcp.variables.length
                                    ? String(root.xcp.variables.length)
                                    : root.shownVariables.length + " / " + root.xcp.variables.length
                                font.pixelSize: 12
                                color: Theme.muted
                            }
                        }
                        TextField {
                            id: filterField
                            Layout.fillWidth: true
                            placeholderText: qsTr("Filter by name or description")
                        }
                        ComboBox {
                            id: kindBox
                            model: root.kindFilters
                            Layout.preferredWidth: 160
                        }
                    }

                    // Column titles
                    Rectangle {
                        visible: root.xcp.variables.length > 0
                        Layout.fillWidth: true
                        implicitHeight: 30
                        color: Theme.cardAlt

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12 + variableScroll.width
                            spacing: 8
                            HeaderLabel { text: qsTr("Name"); Layout.fillWidth: true; Layout.minimumWidth: 120 }
                            HeaderLabel { text: qsTr("Value"); Layout.preferredWidth: root.valueWidth }
                            HeaderLabel { text: qsTr("Type · address"); Layout.preferredWidth: root.typeWidth; visible: !root.compact }
                            HeaderLabel { text: qsTr("Measure"); Layout.preferredWidth: root.measureWidth }
                            HeaderLabel { text: qsTr("Plot"); Layout.preferredWidth: root.plotWidth }
                            HeaderLabel { text: qsTr("Set"); Layout.preferredWidth: root.setWidth }
                        }
                    }

                    ListView {
                        id: variableList
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.xcp.variables.length > 0
                        ScrollBar.vertical: ScrollBar { id: variableScroll }
                        model: root.shownVariables
                        delegate: Rectangle {
                            id: row
                            required property var modelData
                            required property int index
                            width: variableList.width
                            height: 44
                            color: rowHover.hovered ? Theme.alpha(Theme.accent, 0.06)
                                 : index % 2 ? Theme.alpha(Theme.cardAlt, 0.5) : "transparent"
                            HoverHandler { id: rowHover }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 12
                                anchors.rightMargin: 12 + variableScroll.width
                                spacing: 8

                                // Name and description
                                ColumnLayout {
                                    id: nameColumn
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 120
                                    spacing: 0
                                    RowLayout {
                                        spacing: 6
                                        Layout.fillWidth: true
                                        Label {
                                            text: row.modelData.name
                                            font.bold: true
                                            elide: Text.ElideRight
                                            Layout.preferredWidth: Math.ceil(implicitWidth) + 1
                                            Layout.maximumWidth: nameColumn.width - (calBadge.visible ? calBadge.width + 6 : 0)
                                            ToolTip.visible: nameHover.hovered && truncated
                                            ToolTip.text: text
                                            HoverHandler { id: nameHover }
                                        }
                                        Label {
                                            id: calBadge
                                            visible: row.modelData.writable
                                            text: qsTr("CAL")
                                            font.pixelSize: 9
                                            font.bold: true
                                            color: Theme.accent
                                            padding: 2
                                            leftPadding: 4
                                            rightPadding: 4
                                            background: Rectangle { radius: 3; color: Theme.alpha(Theme.accent, 0.15) }
                                            ToolTip.visible: calHover.hovered
                                            ToolTip.text: qsTr("Calibration value: can be written")
                                            HoverHandler { id: calHover }
                                        }
                                    }
                                    Label {
                                        text: root.compact
                                            ? row.modelData.type + " · " + row.modelData.address
                                            : row.modelData.description
                                        color: Theme.muted
                                        font.pixelSize: 11
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                        ToolTip.visible: descriptionHover.hovered && (root.compact || truncated)
                                        ToolTip.text: row.modelData.description
                                        HoverHandler { id: descriptionHover }
                                    }
                                }
                                // Value
                                Label {
                                    Layout.preferredWidth: root.valueWidth
                                    Layout.maximumWidth: root.valueWidth
                                    text: row.modelData.error ? row.modelData.error : (row.modelData.value || "–")
                                    color: row.modelData.error ? Theme.danger : row.modelData.value ? Theme.text : Theme.muted
                                    font.family: row.modelData.error ? Qt.application.font.family : "monospace"
                                    font.pixelSize: row.modelData.error ? 12 : 13
                                    elide: Text.ElideRight
                                    ToolTip.visible: valueHover.hovered && (truncated || row.modelData.raw.length > 0)
                                    ToolTip.text: text + (row.modelData.raw ? "\n" + qsTr("raw: %1").arg(row.modelData.raw) : "")
                                    HoverHandler { id: valueHover }
                                }
                                // Type and address
                                ColumnLayout {
                                    visible: !root.compact
                                    Layout.preferredWidth: root.typeWidth
                                    Layout.maximumWidth: root.typeWidth
                                    spacing: 0
                                    Label {
                                        Layout.fillWidth: true
                                        text: row.modelData.type
                                        color: Theme.muted
                                        font.pixelSize: 11
                                        elide: Text.ElideRight
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: row.modelData.address
                                        color: Theme.muted
                                        font.family: "monospace"
                                        font.pixelSize: 11
                                        elide: Text.ElideRight
                                    }
                                }
                                // Measure: polled or by a DAQ event; a scalar value: written directly or by STIM at an event
                                Item {
                                    Layout.preferredWidth: root.measureWidth
                                    implicitHeight: measureBox.implicitHeight
                                    ComboBox {
                                        id: measureBox
                                        anchors.fill: parent
                                        visible: row.modelData.daq || row.modelData.stim
                                        model: row.modelData.daq ? root.eventNames : root.stimNames
                                        currentIndex: row.modelData.acquisition
                                        enabled: !root.xcp.daq_running
                                        ToolTip.visible: hovered
                                        ToolTip.text: row.modelData.daq
                                            ? qsTr("Poll, or measure by DAQ on an event of the ECU")
                                            : qsTr("Write directly, or by STIM: while DAQ runs, Set sends the value and the ECU writes it at the event")
                                        onActivated: index => root.xcp.set_acquisition(row.modelData.name, index)
                                    }
                                }
                                // Plot
                                Item {
                                    Layout.preferredWidth: root.plotWidth
                                    implicitHeight: plotBox.implicitHeight
                                    CheckBox {
                                        id: plotBox
                                        anchors.centerIn: parent
                                        visible: (row.modelData.kind === "MEASUREMENT" || row.modelData.kind === "VALUE") && !row.modelData.structured
                                        checked: row.modelData.plotted
                                        ToolTip.visible: hovered
                                        ToolTip.text: qsTr("Plot the value on the Plot page")
                                        onToggled: root.xcp.set_plotted(row.modelData.name, checked)
                                    }
                                }
                                // Set or edit
                                Item {
                                    Layout.preferredWidth: root.setWidth
                                    implicitHeight: 34

                                    RowLayout {
                                        anchors.fill: parent
                                        spacing: 4
                                        visible: row.modelData.writable && !row.modelData.structured
                                        TextField {
                                            id: setField
                                            Layout.fillWidth: true
                                            placeholderText: qsTr("value")
                                            enabled: root.connected
                                            onAccepted: {
                                                root.report(root.xcp.write_variable(row.modelData.name, text))
                                                text = ""
                                            }
                                        }
                                        Button {
                                            text: qsTr("Set")
                                            enabled: root.ready && setField.text.length > 0
                                            onClicked: {
                                                root.report(root.xcp.write_variable(row.modelData.name, setField.text))
                                                setField.text = ""
                                            }
                                        }
                                    }
                                    Button {
                                        anchors.fill: parent
                                        visible: row.modelData.structured && !row.modelData.error
                                        text: row.modelData.writable ? qsTr("Edit…") : qsTr("Show…")
                                        enabled: root.ready
                                        onClicked: root.report(root.xcp.open_editor(row.modelData.name))
                                    }
                                }
                            }
                        }
                    }

                    Label {
                        visible: root.xcp.variables.length > 0 && root.shownVariables.length === 0
                        Layout.fillWidth: true
                        padding: 16
                        horizontalAlignment: Text.AlignHCenter
                        color: Theme.muted
                        text: qsTr("No variable matches the filter.")
                    }

                    // Without an A2L
                    ColumnLayout {
                        visible: root.xcp.variables.length === 0
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 12

                        Item { Layout.fillHeight: true }
                        Label {
                            Layout.alignment: Qt.AlignHCenter
                            text: qsTr("No A2L file loaded")
                            font.pixelSize: 16
                            font.bold: true
                        }
                        Label {
                            Layout.alignment: Qt.AlignHCenter
                            Layout.maximumWidth: 460
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            color: Theme.muted
                            text: qsTr("An A2L file names the ECU's measurements and calibration values, with their addresses, types and units. Without one, read and write memory by address on the left.")
                        }
                        Button {
                            Layout.alignment: Qt.AlignHCenter
                            text: qsTr("Load A2L…")
                            highlighted: true
                            onClicked: a2lDialog.open()
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }

            // Log
            Card {
                SplitView.preferredHeight: 140
                SplitView.minimumHeight: 70
                padding: 1

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 2

                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: qsTr("Log"); color: Theme.muted }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Clear")
                            flat: true
                            enabled: root.xcp.log.length > 0
                            onClicked: root.xcp.clear_log()
                        }
                    }
                    ListView {
                        id: logList
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ScrollBar.vertical: ScrollBar {}
                        model: root.xcp.log
                        onCountChanged: positionViewAtEnd()
                        delegate: Label {
                            required property var modelData
                            width: logList.width - 12
                            text: modelData.text
                            font.family: "monospace"
                            font.pixelSize: 12
                            color: modelData.error ? Theme.danger : Theme.text
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }
        }
    }

    // --- Editor of curves, maps, blocks and arrays ---
    Popup {
        id: editorPopup
        readonly property var editor: root.xcp.editor
        readonly property bool isMap: editor.kind === "MAP"
        readonly property var rows: editor.rows || []
        readonly property var xAxis: editor.x || []
        readonly property var yAxis: editor.y || []
        readonly property int columns: Math.max(1, rows.length ? rows[0].length : 1)
        readonly property bool hasLabelColumn: isMap || xAxis.length > 0
        readonly property var flatValues: [].concat.apply([], rows)
        readonly property var numbers: flatValues.map(v => parseFloat(v)).filter(v => !isNaN(v))
        readonly property real low: numbers.length ? Math.min.apply(null, numbers) : 0
        readonly property real high: numbers.length ? Math.max.apply(null, numbers) : 0
        property int edits: 0  // Cells that differ from the ECU's values

        function shade(text) {
            const value = parseFloat(text)
            if (isNaN(value) || high <= low)
                return Theme.alpha(Theme.accent, 0.08)
            return Theme.alpha(Theme.accent, 0.06 + 0.34 * (value - low) / (high - low))
        }

        function cells(repeater) {
            const values = []
            for (let i = 0; i < repeater.count; ++i)
                values.push(repeater.itemAt(i).text)
            return values
        }

        function countEdits() {
            let count = 0
            for (const repeater of [valueCells, xCells, yCells])
                for (let i = 0; i < repeater.count; ++i)
                    if (repeater.itemAt(i) && repeater.itemAt(i).edited)
                        ++count
            edits = count
        }

        function revert() {
            for (const repeater of [valueCells, xCells, yCells])
                for (let i = 0; i < repeater.count; ++i)
                    repeater.itemAt(i).text = repeater.itemAt(i).original
            countEdits()
        }

        function apply() {
            const flat = cells(valueCells)
            const rows = []
            for (let i = 0; i < flat.length; i += columns)
                rows.push(flat.slice(i, i + columns))
            root.report(root.xcp.write_editor(rows, cells(xCells), cells(yCells)))
        }

        anchors.centerIn: Overlay.overlay
        modal: true
        padding: 16
        width: Math.min(root.width - 40, Math.max(460, grid.implicitWidth + 2 * padding + 24))
        height: Math.min(root.height - 40, editorColumn.implicitHeight + 2 * padding)
        onClosed: if (root.xcp.editor.name !== undefined) root.xcp.close_editor()
        onRowsChanged: Qt.callLater(countEdits)

        Connections {
            target: root.xcp
            function onEditor_changed() {
                if (root.xcp.editor.name !== undefined)
                    editorPopup.open()
                else
                    editorPopup.close()
            }
        }

        // A cell of the grid: shows whether it was changed
        component Cell: TextField {
            property string original
            readonly property bool edited: text !== original
            implicitWidth: 76
            horizontalAlignment: Text.AlignRight
            font.family: "monospace"
            selectByMouse: true
            onTextChanged: editorPopup.countEdits()
        }

        ColumnLayout {
            id: editorColumn
            anchors.fill: parent
            spacing: 8

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Label {
                    text: editorPopup.editor.name || ""
                    font.pixelSize: Theme.titleSize
                    font.bold: true
                }
                Pill { text: editorPopup.editor.kind || "" }
                Pill {
                    visible: !!editorPopup.editor.unit
                    text: editorPopup.editor.unit || ""
                    tint: Theme.muted
                }
                Pill {
                    visible: editorPopup.editor.writable !== true
                    text: qsTr("Read-only")
                    tint: Theme.warning
                }
                Item { Layout.fillWidth: true }
            }
            Label {
                visible: !!editorPopup.editor.description
                text: editorPopup.editor.description || ""
                color: Theme.muted
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            Label {
                Layout.fillWidth: true
                color: Theme.muted
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                text: [
                    editorPopup.editor.limits ? qsTr("Limits %1").arg(editorPopup.editor.limits) : "",
                    editorPopup.isMap
                        ? qsTr("Columns: %1 · rows: %2").arg(editorPopup.editor.x_name || "X").arg(editorPopup.editor.y_name || "Y")
                        : (editorPopup.xAxis.length ? qsTr("Top row: %1").arg(editorPopup.editor.x_name || "X") : ""),
                    editorPopup.editor.writable && !(editorPopup.editor.x_editable || editorPopup.editor.y_editable) && editorPopup.xAxis.length
                        ? qsTr("Axis points are fixed or shared; change a shared axis through its AXIS_PTS.") : ""
                ].filter(t => t).join("  ·  ")
            }

            ScrollView {
                id: gridScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredHeight: grid.implicitHeight + 16
                clip: true

                GridLayout {
                    id: grid
                    columns: editorPopup.columns + (editorPopup.hasLabelColumn ? 1 : 0)
                    rowSpacing: 3
                    columnSpacing: 3

                    // Corner (above the Y axis), or the label of the X axis row of a curve
                    Label {
                        visible: editorPopup.hasLabelColumn
                        Layout.row: 0
                        Layout.column: 0
                        Layout.preferredWidth: 76
                        text: editorPopup.isMap ? "Y \\ X" : "X"
                        color: Theme.muted
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignRight
                    }
                    Repeater {
                        id: xCells
                        model: editorPopup.xAxis
                        delegate: Cell {
                            required property var modelData
                            required property int index
                            original: modelData
                            text: modelData
                            readOnly: !editorPopup.editor.x_editable || !editorPopup.editor.writable
                            font.bold: true
                            Layout.row: 0
                            Layout.column: 1 + index
                            background: Rectangle {
                                radius: 4
                                color: Theme.cardAlt
                                border.color: parent.edited ? Theme.warning : Theme.border
                                border.width: parent.edited ? 2 : 1
                            }
                        }
                    }
                    // The label of the value row of a curve
                    Label {
                        visible: editorPopup.hasLabelColumn && !editorPopup.isMap
                        Layout.row: 1
                        Layout.column: 0
                        Layout.preferredWidth: 76
                        text: qsTr("Value")
                        color: Theme.muted
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignRight
                    }
                    Repeater {
                        id: yCells
                        model: editorPopup.isMap ? editorPopup.yAxis : []
                        delegate: Cell {
                            required property var modelData
                            required property int index
                            original: modelData
                            text: modelData
                            readOnly: !editorPopup.editor.y_editable || !editorPopup.editor.writable
                            font.bold: true
                            Layout.row: 1 + index
                            Layout.column: 0
                            background: Rectangle {
                                radius: 4
                                color: Theme.cardAlt
                                border.color: parent.edited ? Theme.warning : Theme.border
                                border.width: parent.edited ? 2 : 1
                            }
                        }
                    }
                    Repeater {
                        id: valueCells
                        model: editorPopup.flatValues
                        delegate: Cell {
                            required property var modelData
                            required property int index
                            original: modelData
                            text: modelData
                            readOnly: !editorPopup.editor.writable
                            Layout.row: (editorPopup.hasLabelColumn ? 1 : 0) + Math.floor(index / editorPopup.columns)
                            Layout.column: (editorPopup.hasLabelColumn ? 1 : 0) + index % editorPopup.columns
                            background: Rectangle {
                                radius: 4
                                color: editorPopup.shade(parent.text)
                                border.color: parent.edited ? Theme.warning : parent.activeFocus ? Theme.accent : Theme.alpha(Theme.border, 0.6)
                                border.width: parent.edited || parent.activeFocus ? 2 : 1
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Label {
                    visible: editorPopup.edits > 0
                    text: qsTr("%n cell(s) changed", "", editorPopup.edits)
                    color: Theme.warning
                }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Revert")
                    visible: editorPopup.editor.writable === true
                    enabled: editorPopup.edits > 0
                    onClicked: editorPopup.revert()
                }
                Button {
                    text: qsTr("Re-read")
                    enabled: root.ready
                    onClicked: root.report(root.xcp.open_editor(editorPopup.editor.name))
                }
                Button {
                    text: qsTr("Write")
                    highlighted: true
                    visible: editorPopup.editor.writable === true
                    enabled: root.ready && editorPopup.edits > 0
                    onClicked: editorPopup.apply()
                }
                Button {
                    text: qsTr("Close")
                    onClicked: editorPopup.close()
                }
            }
        }
    }

    FileChooser {
        id: a2lDialog
        title: qsTr("A2L file")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("A2L files (*.a2l)"), qsTr("All files (*)")]
        onAccepted: root.report(root.xcp.load_a2l(selectedFile))
    }
    FileChooser {
        id: keyDialog
        title: qsTr("Seed & key algorithm")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Python files (*.py)"), qsTr("All files (*)")]
        onAccepted: root.xcp.set_key_file(selectedFile)
    }
    FileChooser {
        id: saveDatasetDialog
        title: qsTr("Save calibration dataset")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "json"
        nameFilters: [qsTr("Calibration datasets (*.json *.dcm)"), qsTr("All files (*)")]
        onAccepted: root.report(root.xcp.save_dataset(selectedFile))
    }
    FileChooser {
        id: kernelDialog
        title: qsTr("Flash kernel")
        fileMode: FileChooser.OpenFile
        nameFilters: [
            qsTr("Firmware files (*.hex *.ihex *.ihx *.s19 *.s28 *.s37 *.srec *.mot *.bin)"),
            qsTr("All files (*)"),
        ]
        onAccepted: root.report(root.xcp.load_kernel(selectedFile))
    }
    FileChooser {
        id: encoderDialog
        title: qsTr("Encoder")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Python files (*.py)"), qsTr("All files (*)")]
        onAccepted: root.report(root.xcp.set_encoder(selectedFile))
    }
    FileChooser {
        id: imageDialog
        title: qsTr("File to program")
        fileMode: FileChooser.OpenFile
        nameFilters: [
            qsTr("Firmware files (*.hex *.ihex *.ihx *.s19 *.s28 *.s37 *.srec *.mot *.bin)"),
            qsTr("All files (*)"),
        ]
        onAccepted: root.report(root.xcp.load_image(selectedFile))
    }
    FileChooser {
        id: loadDatasetDialog
        title: qsTr("Calibration dataset")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Calibration datasets (*.json *.dcm)"), qsTr("All files (*)")]
        onAccepted: root.report(root.xcp.load_dataset(selectedFile))
    }
}
