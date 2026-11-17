import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// UDS diagnostics: a header with the transport and state, the services grouped into sections on the left, and the
// ECU's trouble codes above the request/response history on the right.
ColumnLayout {
    id: root
    required property var diagnostics
    required property bool connected
    readonly property bool ready: connected && !diagnostics.busy
    readonly property string addressing: diagnostics.addressing
    property string errorText: ""

    function report(error) {
        errorText = error
    }

    function applyAddressing() {
        if (addressing === "doip")
            report(diagnostics.set_target(targetField.text))
        else
            report(diagnostics.set_addressing(requestIdField.text, responseIdField.text, extendedBox.checked, nadField.text))
    }

    function selectedDid() {
        return didBox.editText.trim().split(/\s+/)[0]
    }

    function fileName(path) {
        return path.split(/[\\/]/).pop()
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

    // A small muted caption above a field
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

    // --- Header ---
    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Label {
            text: qsTr("UDS diagnostics")
            color: Theme.text
            font.pixelSize: Theme.titleSize
            font.bold: true
        }
        Pill {
            text: root.diagnostics.transport_name
        }
        Pill {
            text: root.connected ? qsTr("Connected") : qsTr("Not connected")
            tint: root.connected ? Theme.success : Theme.muted
        }
        Pill {
            visible: root.diagnostics.tester_present
            text: qsTr("Tester present")
            tint: Theme.success
        }
        Pill {
            visible: root.diagnostics.description_file !== ""
            text: root.fileName(root.diagnostics.description_file)
            tint: Theme.accent
            ToolTip.visible: descriptionHover.hovered
            ToolTip.text: root.diagnostics.description_file
            HoverHandler { id: descriptionHover }
        }
        Item { Layout.fillWidth: true }
        BusyIndicator {
            running: root.diagnostics.busy
            visible: running
            Layout.preferredHeight: 24
            Layout.preferredWidth: 24
        }
        Label {
            visible: root.diagnostics.busy
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
                        visible: root.addressing === "can"
                        columns: 2
                        columnSpacing: 8
                        rowSpacing: 2
                        Layout.fillWidth: true

                        FieldLabel { text: qsTr("Request ID") }
                        FieldLabel { text: qsTr("Response ID") }
                        TextField {
                            id: requestIdField
                            text: "7E0"
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("CAN identifier in hex. For extended or mixed addressing add the address byte that starts every frame after a colon, e.g. 6F1:12")
                            onEditingFinished: root.applyAddressing()
                        }
                        TextField {
                            id: responseIdField
                            text: "7E8"
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("CAN identifier in hex. For extended or mixed addressing add the address byte that starts every frame after a colon, e.g. 6F1:12")
                            onEditingFinished: root.applyAddressing()
                        }
                    }
                    CheckBox {
                        id: extendedBox
                        visible: root.addressing === "can"
                        text: qsTr("Extended (29-bit) identifiers")
                        onToggled: root.applyAddressing()
                    }
                    Button {
                        visible: root.diagnostics.functional_available
                        text: qsTr("Find ECUs")
                        enabled: root.ready
                        Layout.fillWidth: true
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Send a functional TesterPresent (0x7DF, or 0x18DB33F1 with 29-bit identifiers) and list the ECUs that answer")
                        onClicked: root.report(root.diagnostics.discover_ecus())
                    }

                    FieldLabel { text: qsTr("NAD"); visible: root.addressing === "lin" }
                    TextField {
                        id: nadField
                        text: "01"
                        visible: root.addressing === "lin"
                        font.family: "monospace"
                        Layout.preferredWidth: 80
                        onEditingFinished: root.applyAddressing()
                    }

                    FieldLabel { text: qsTr("Target address"); visible: root.addressing === "doip" }
                    TextField {
                        id: targetField
                        text: root.addressing === "doip" ? root.diagnostics.target : ""
                        visible: root.addressing === "doip"
                        font.family: "monospace"
                        Layout.preferredWidth: 100
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Logical address of the ECU (hex), or of the DoIP entity itself")
                        onEditingFinished: root.applyAddressing()
                    }

                    FieldLabel { text: qsTr("Diagnostic description") }
                    Button {
                        Layout.fillWidth: true
                        text: root.diagnostics.description_file
                            ? qsTr("Unload %1").arg(root.fileName(root.diagnostics.description_file))
                            : qsTr("Load description (.cdd)…")
                        ToolTip.visible: hovered
                        ToolTip.text: root.diagnostics.description_file
                            ? qsTr("ECU description loaded; click to unload")
                            : qsTr("Load the ECU's diagnostic description (CANdelaStudio .cdd) to name and decode its DIDs")
                        onClicked: root.diagnostics.description_file
                            ? root.diagnostics.unload_description()
                            : descriptionDialog.open()
                    }
                }

                Section {
                    title: qsTr("Session")

                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: sessionBox
                            model: root.diagnostics.sessions
                            textRole: "name"
                            valueRole: "value"
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Change")
                            enabled: root.ready
                            Layout.preferredWidth: 90
                            onClicked: root.report(root.diagnostics.session_control(sessionBox.currentValue))
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: resetBox
                            model: root.diagnostics.reset_types
                            textRole: "name"
                            valueRole: "value"
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Reset")
                            enabled: root.ready
                            Layout.preferredWidth: 90
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("ECU reset (0x11)")
                            onClicked: root.report(root.diagnostics.ecu_reset(resetBox.currentValue))
                        }
                    }
                    Switch {
                        text: qsTr("Tester present")
                        checked: root.diagnostics.tester_present
                        enabled: root.connected
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Periodically send TesterPresent (0x3E) to keep a non-default session open")
                        onToggled: root.diagnostics.set_tester_present(checked)
                    }
                }

                Section {
                    title: qsTr("Data identifiers")

                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: didBox
                            editable: true
                            model: root.diagnostics.known_dids.map(entry => `${entry.did}  ${entry.name}`)
                            currentIndex: model.findIndex(text => text.startsWith("F190"))
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Read")
                            enabled: root.ready
                            Layout.preferredWidth: 90
                            onClicked: root.report(root.diagnostics.read_did(root.selectedDid()))
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: didDataField
                            placeholderText: qsTr("Data to write, hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Write")
                            enabled: root.ready
                            Layout.preferredWidth: 90
                            onClicked: root.report(root.diagnostics.write_did(root.selectedDid(), didDataField.text))
                        }
                    }
                }

                Section {
                    title: qsTr("Periodic data")
                    visible: root.diagnostics.listening_available

                    FieldLabel { text: qsTr("Define a DID from other DIDs or memory (0x2C)") }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: dddidField
                            text: "F201"
                            font.family: "monospace"
                            Layout.preferredWidth: 64
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The dynamically defined DID (hex), usually F200-F3FF")
                        }
                        TextField {
                            id: sourcesField
                            placeholderText: qsTr("0100:1:2 F190:1:4 or @20001000:4")
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Sources: DID:position:size (DID hex, position from 1 and size in bytes) or memory @address:size (address hex)")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            text: qsTr("Define")
                            enabled: root.ready
                            Layout.fillWidth: true
                            onClicked: root.report(root.diagnostics.define_dynamic(dddidField.text, sourcesField.text))
                        }
                        Button {
                            text: qsTr("Clear")
                            enabled: root.ready
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Clear this dynamically defined DID (all of them when the DID field is empty)")
                            onClicked: root.report(root.diagnostics.clear_dynamic(dddidField.text))
                        }
                    }

                    FieldLabel { text: qsTr("Send periodically (0x2A)") }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: periodicIdsField
                            text: "F201"
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The periodic DIDs (F200-F2FF, hex, separated by spaces)")
                        }
                        ComboBox {
                            id: rateBox
                            model: root.diagnostics.periodic_rates
                            textRole: "name"
                            valueRole: "value"
                            currentIndex: 2
                            Layout.preferredWidth: 100
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Frames on CAN ID") }
                        TextField {
                            id: periodicCanField
                            placeholderText: qsTr("diagnostic")
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The CAN ID of the ECU's periodic frames (hex). Empty: they come on the response ID (0x6A messages), which turns on listening between requests")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            text: qsTr("Start")
                            enabled: root.ready && !root.diagnostics.periodic_running
                            Layout.fillWidth: true
                            onClicked: root.report(root.diagnostics.start_periodic(periodicIdsField.text, rateBox.currentValue, periodicCanField.text))
                        }
                        Button {
                            text: qsTr("Stop")
                            enabled: root.ready && root.diagnostics.periodic_running
                            Layout.fillWidth: true
                            onClicked: root.report(root.diagnostics.stop_periodic())
                        }
                    }
                    Repeater {
                        model: root.diagnostics.periodic_rows
                        delegate: ColumnLayout {
                            required property var modelData
                            spacing: 0
                            Layout.fillWidth: true
                            RowLayout {
                                Layout.fillWidth: true
                                Label {
                                    text: modelData.did
                                    color: Theme.text
                                    font.family: "monospace"
                                    font.bold: true
                                }
                                Label {
                                    text: modelData.decoded || modelData.data
                                    color: Theme.text
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                            }
                            Label {
                                text: qsTr("%1 messages, %2, %3").arg(modelData.count).arg(modelData.rate).arg(modelData.source)
                                color: Theme.muted
                                font.pixelSize: 11
                            }
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
                            to: 0x7F
                            stepSize: 2
                            value: 1
                        }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Unlock")
                            enabled: root.ready
                            Layout.preferredWidth: 90
                            onClicked: root.report(root.diagnostics.security_access(levelBox.value))
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            Layout.fillWidth: true
                            text: root.diagnostics.key_file
                                ? qsTr("Key algorithm: %1").arg(root.fileName(root.diagnostics.key_file))
                                : qsTr("Choose key algorithm…")
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Python file defining compute_key(level: int, seed: bytes) -> bytes")
                            onClicked: keyFileDialog.open()
                        }
                        SeedKeyButton { onChosen: source => root.diagnostics.set_key_source(source) }
                    }
                    // Testing the ECU's security access (benches you are authorised to test)
                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: seedRenewalBox
                            model: [
                                { value: "session", text: qsTr("New session per seed") },
                                { value: "reset", text: qsTr("ECU reset per seed") },
                                { value: "none", text: qsTr("Repeated requests") },
                            ]
                            textRole: "text"
                            valueRole: "value"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("How the ECU's seed is renewed between requests: many ECUs keep a seed until a key is sent")
                        }
                        Button {
                            text: qsTr("Analyse Seeds")
                            enabled: root.ready
                            onClicked: root.report(root.diagnostics.analyse_seeds(levelBox.value, 32, seedRenewalBox.currentValue))
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Request 32 seeds of the level and look for repeats, bytes that never change, predictable steps and short seeds")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        CheckBox {
                            id: lockoutResetBox
                            text: qsTr("Then reset the ECU")
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("After the lockout, reset the ECU and check whether the delay survives")
                        }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Test Lockout")
                            enabled: root.ready
                            onClicked: root.report(root.diagnostics.test_lockout(levelBox.value, 10, lockoutResetBox.checked))
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Send up to 10 wrong keys and record when the ECU locks out (exceedNumberOfAttempts) and refuses seeds (requiredTimeDelayNotExpired). The ECU stays locked for its delay time")
                        }
                    }
                }

                // Authentication (0x29) by certificate exchange or by challenge-response, with keys and certificates from
                // the Security Manager or a security profile
                Section {
                    id: authSection
                    title: qsTr("Authentication")
                    readonly property var entries: connectionManager.security.entries
                    readonly property var profiles: connectionManager.security.security_profiles
                    readonly property bool challengeResponse: authMethodBox.currentIndex === 1
                    function entriesOf(purpose) {
                        return entries.filter(entry => entry.purpose === purpose)
                            .map(entry => ({ value: entry.name, text: entry.name, symmetric: purpose === "uds-auth-key" }))
                    }
                    function profilesWith(capabilities) {
                        return profiles.filter(profile => capabilities(profile.capabilities))
                            .map(profile => ({ value: "profile:" + profile.name, text: qsTr("%1 (profile)").arg(profile.name), symmetric: false }))
                    }
                    // Certificate exchange: a client certificate with its key, or a profile with a certificate that signs
                    readonly property var certificates: entriesOf("tls-client")
                        .concat(profilesWith(caps => caps.indexOf("certificate") >= 0 && caps.indexOf("sign") >= 0))
                    // Challenge-response: a shared key, a private key, or a profile proving challenges (or signing)
                    readonly property var provers: entriesOf("uds-auth-key").concat(entriesOf("tls-client"))
                        .concat(profilesWith(caps => caps.indexOf("prove_challenge") >= 0 || caps.indexOf("sign") >= 0))
                    readonly property var credentials: challengeResponse ? provers : certificates
                    readonly property var credential: authCredentialBox.currentIndex >= 0 && credentials.length > 0
                        ? credentials[authCredentialBox.currentIndex] : null
                    readonly property var trustedSets: entries.filter(entry => entry.purpose === "tls-ca").map(entry => entry.name)

                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Method") }
                        ComboBox {
                            id: authMethodBox
                            model: [qsTr("Certificate exchange"), qsTr("Challenge-response")]
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Certificate exchange (sub-functions 0x01–0x03) or challenge-response without certificates (0x05–0x07); authenticationConfiguration (0x08) tells which one the ECU offers")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: authSection.challengeResponse ? qsTr("Key") : qsTr("Certificate") }
                        ComboBox {
                            id: authCredentialBox
                            model: authSection.credentials.length > 0 ? authSection.credentials : [{ value: "", text: qsTr("none in the Security Manager") }]
                            textRole: "text"
                            valueRole: "value"
                            enabled: authSection.credentials.length > 0
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: authSection.challengeResponse
                                ? qsTr("A shared authentication key (the proof is a MAC), a client certificate's private key (a signature), or a security profile, from the Security Manager (unlocked)")
                                : qsTr("A client certificate with its private key from the Security Manager (unlocked), or a security profile's certificate")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: !(authSection.credential && authSection.credential.symmetric)
                        FieldLabel { text: qsTr("Proof") }
                        ComboBox {
                            id: authSchemeBox
                            model: root.diagnostics.signature_schemes
                            textRole: "name"
                            valueRole: "value"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("How the proof of ownership (the signature over the ECU's challenge) is made; the ECU's configuration decides which one it expects")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: authSection.challengeResponse
                        FieldLabel { text: qsTr("Algorithm") }
                        TextField {
                            id: authAlgorithmField
                            placeholderText: qsTr("hex or OID, empty for zeros")
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The algorithm asked for in requestChallengeForAuthentication: 16 bytes in hex, or a dotted OID (DER encoded). The proof is made for the algorithm the ECU answers with")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: authSection.challengeResponse
                        FieldLabel { text: qsTr("Additional") }
                        TextField {
                            id: authAdditionalField
                            placeholderText: qsTr("additional parameter, hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Sent when the ECU asks for an additional parameter; what it holds is manufacturer specific")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        CheckBox {
                            id: authBidirectionalBox
                            text: qsTr("Bidirectional")
                            ToolTip.visible: hovered
                            ToolTip.text: authSection.challengeResponse
                                ? qsTr("The ECU also proves owning its key over a challenge of ours; checked with the shared key, the profile or trusted certificates")
                                : qsTr("The ECU also sends its certificate and proves owning it; check it against trusted certificates")
                        }
                        ComboBox {
                            id: authTrustedBox
                            visible: authBidirectionalBox.checked && !(authSection.credential && authSection.credential.symmetric)
                            model: [qsTr("Not checked")].concat(authSection.trustedSets)
                            Layout.fillWidth: true
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Session key") }
                        TextField {
                            id: authConfigurationField
                            text: "00"
                            font.family: "monospace"
                            Layout.preferredWidth: 60
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The communicationConfiguration (hex): 00 asks for no session key; the values asking an ECU for one are defined by its manufacturer. The session key can then protect secured transmission (0x84)")
                        }
                        Label {
                            text: root.diagnostics.session_key || qsTr("none")
                            color: root.diagnostics.session_key ? Theme.success : Theme.muted
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                    }
                    Label {
                        visible: root.diagnostics.authenticated
                        text: qsTr("Authenticated")
                        color: Theme.success
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Configuration")
                            enabled: root.ready
                            onClicked: root.report(root.diagnostics.authentication_configuration())
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Ask the ECU which authentication it offers (authenticationConfiguration)")
                        }
                        Button {
                            text: qsTr("De-authenticate")
                            enabled: root.ready
                            onClicked: root.report(root.diagnostics.deauthenticate())
                        }
                        Button {
                            text: qsTr("Authenticate")
                            enabled: root.ready && authSection.credentials.length > 0
                            onClicked: {
                                const trusted = authBidirectionalBox.checked && authTrustedBox.visible && authTrustedBox.currentIndex > 0
                                    ? authTrustedBox.currentText : ""
                                if (authSection.challengeResponse)
                                    root.report(root.diagnostics.authenticate_challenge_response({
                                        source: authCredentialBox.currentValue,
                                        scheme: authSection.credential.symmetric ? "" : authSchemeBox.currentValue,
                                        bidirectional: authBidirectionalBox.checked,
                                        trusted: trusted,
                                        algorithm: authAlgorithmField.text,
                                        additional: authAdditionalField.text,
                                        configuration: authConfigurationField.text,
                                    }))
                                else
                                    root.report(root.diagnostics.authenticate({
                                        certificate: authCredentialBox.currentValue,
                                        scheme: authSchemeBox.currentValue,
                                        bidirectional: authBidirectionalBox.checked,
                                        trusted: trusted,
                                        configuration: authConfigurationField.text,
                                    }))
                            }
                        }
                    }
                }

                // SecuredDataTransmission (0x84): the requests of this view in signed and/or encrypted envelopes, with a
                // secured data key of the Security Manager (CANExplorer's reference scheme) or a provider file with the
                // manufacturer's scheme
                Section {
                    id: securedSection
                    title: qsTr("Secured transmission (0x84)")
                    property string providerFile: ""
                    readonly property var keys: authSection.entries.filter(entry => entry.purpose === "uds-sdt-key")
                        .map(entry => ({ value: "keystore:" + entry.name, text: entry.name }))
                    readonly property var sources: [{ value: "", text: qsTr("None (unsigned)") }]
                        .concat([{ value: "session", text: qsTr("Session key (Authentication)") }]).concat(keys)
                        .concat([{ value: "file", text: providerFile ? qsTr("Provider: %1").arg(root.fileName(providerFile)) : qsTr("Provider file…") }])
                    readonly property string source: securedSourceBox.currentValue === "file" ? providerFile : (securedSourceBox.currentValue || "")
                    readonly property bool keyStoreKey: source.startsWith("keystore:")
                    readonly property bool sessionKey: source === "session"

                    function apply(counter) {
                        const options = {
                            enabled: securedEnabledBox.checked,
                            source: source,
                            sign: securedSignBox.checked,
                            encrypt: securedEncryptBox.checked,
                            signed_response: securedResponseBox.checked,
                            algorithm: securedAlgorithmField.text,
                            length: keyStoreKey || sessionKey ? securedLengthBox.value : 0,
                        }
                        if (counter !== undefined)
                            options.counter = counter
                        const error = root.diagnostics.set_secured(options)
                        if (error)
                            securedEnabledBox.checked = false
                        root.report(error)
                    }

                    CheckBox {
                        id: securedEnabledBox
                        text: qsTr("Send requests secured")
                        checked: root.diagnostics.secured
                        onToggled: securedSection.apply()
                        // Clicking replaces the binding: follow the controller, which ends requests secured with a
                        // session key when the authentication ends
                        Connections {
                            target: root.diagnostics
                            function onChanged() { securedEnabledBox.checked = root.diagnostics.secured }
                        }
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Wrap every request of this page in a SecuredDataTransmission envelope (ISO 14229-1:2020) and check and unwrap the responses. Tester present and flashing stay plain")
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Key") }
                        ComboBox {
                            id: securedSourceBox
                            model: securedSection.sources
                            textRole: "text"
                            valueRole: "value"
                            Layout.fillWidth: true
                            onActivated: {
                                if (currentValue === "file")
                                    securedProviderDialog.open()
                                else
                                    securedSection.apply()
                            }
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The session key of the last authentication (from the shared key with CANExplorer's reference derivation, or from the security profile's session_provider), a secured data key of the Security Manager (CANExplorer's reference scheme: a MAC over the message, no encryption), or a Python provider file implementing the manufacturer's scheme (signature_length, sign, verify, encrypt, decrypt)")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Algorithm") }
                        TextField {
                            id: securedAlgorithmField
                            text: "00"
                            font.family: "monospace"
                            Layout.preferredWidth: 60
                            onEditingFinished: securedSection.apply()
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The Signature/Encryption Calculation byte (hex): 00–7F vehicle manufacturer, 80–8F system supplier specific; it tells the ECU which algorithm protects the message")
                        }
                        FieldLabel { text: qsTr("MAC bytes"); visible: securedSection.keyStoreKey || securedSection.sessionKey }
                        SpinBox {
                            id: securedLengthBox
                            visible: securedSection.keyStoreKey || securedSection.sessionKey
                            from: 0
                            to: 32
                            value: 0
                            onValueModified: securedSection.apply()
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Truncate the MAC to this many bytes (0: the full MAC, 32 bytes for HMAC-SHA-256, 16 for AES-CMAC)")
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Flow {
                        Layout.fillWidth: true
                        spacing: 8
                        CheckBox {
                            id: securedSignBox
                            text: qsTr("Sign")
                            checked: true
                            enabled: securedSection.source !== ""
                            onToggled: securedSection.apply()
                        }
                        CheckBox {
                            id: securedEncryptBox
                            text: qsTr("Encrypt")
                            enabled: securedSection.source !== "" && !securedSection.keyStoreKey
                            onToggled: securedSection.apply()
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Encrypt the internal message; needs a provider file with encrypt and decrypt (the reference scheme does not encrypt)")
                        }
                        CheckBox {
                            id: securedResponseBox
                            text: qsTr("Signed response")
                            enabled: securedSection.source !== ""
                            onToggled: securedSection.apply()
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("Ask the ECU to sign its response, and refuse unsigned ones")
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FieldLabel { text: qsTr("Counter") }
                        SpinBox {
                            id: securedCounterBox
                            from: 0
                            to: 65535
                            editable: true
                            value: root.diagnostics.secured_counter
                            onValueModified: securedSection.apply(value)
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("The anti-replay counter of the next request; it increases by one per request. ECUs refuse counters they have already seen")
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                Section {
                    title: qsTr("Routine control")

                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: routineTypeBox
                            model: root.diagnostics.routine_types
                            textRole: "name"
                            valueRole: "value"
                            Layout.fillWidth: true
                        }
                        TextField {
                            id: routineIdField
                            placeholderText: qsTr("ID, hex")
                            font.family: "monospace"
                            Layout.preferredWidth: 90
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: routineDataField
                            placeholderText: qsTr("Parameters, hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                        }
                        Button {
                            text: qsTr("Run")
                            enabled: root.ready
                            Layout.preferredWidth: 90
                            onClicked: root.report(root.diagnostics.routine_control(routineTypeBox.currentValue, routineIdField.text, routineDataField.text))
                        }
                    }
                }

                Section {
                    title: qsTr("Raw request")

                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: requestField
                            text: "22 F1 90"
                            placeholderText: qsTr("UDS request, hex")
                            font.family: "monospace"
                            Layout.fillWidth: true
                            onAccepted: if (root.ready) root.report(root.diagnostics.raw_request(text))
                        }
                        Button {
                            text: qsTr("Send")
                            enabled: root.ready
                            Layout.preferredWidth: 90
                            onClicked: root.report(root.diagnostics.raw_request(requestField.text))
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
                        text: String(root.diagnostics.dtcs.length)
                        tint: root.diagnostics.dtcs.length > 0 ? Theme.warning : Theme.muted
                    }
                    Item { Layout.fillWidth: true }
                    ComboBox {
                        id: reportBox
                        model: root.diagnostics.dtc_reports
                        textRole: "name"
                        valueRole: "value"
                        Layout.preferredWidth: 220
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("The ReadDTCInformation (0x19) report to read")
                    }
                    FieldLabel {
                        visible: reportBox.currentValue === "status" || reportBox.currentValue === "memory"
                        text: qsTr("Status mask")
                    }
                    TextField {
                        id: maskField
                        visible: reportBox.currentValue === "status" || reportBox.currentValue === "memory"
                        text: "FF"
                        font.family: "monospace"
                        Layout.preferredWidth: 56
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Only report DTCs with one of these status bits set (hex)")
                    }
                    FieldLabel { visible: reportBox.currentValue === "memory"; text: qsTr("Memory") }
                    TextField {
                        id: memoryField
                        visible: reportBox.currentValue === "memory"
                        text: "01"
                        font.family: "monospace"
                        Layout.preferredWidth: 44
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("The memory selection of the user-defined DTC memory (hex)")
                    }
                    Button {
                        text: qsTr("Read")
                        enabled: root.ready
                        onClicked: root.report(root.diagnostics.read_dtc_report(reportBox.currentValue, maskField.text, memoryField.text))
                    }
                    Button {
                        text: qsTr("Clear")
                        enabled: root.ready
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Clear all diagnostic information in the ECU (0x14)")
                        onClicked: root.report(root.diagnostics.clear_dtcs())
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
                    model: root.diagnostics.dtcs

                    delegate: Rectangle {
                        id: dtcRow
                        required property var modelData
                        readonly property bool confirmed: modelData.flags.indexOf("confirmedDTC") >= 0
                        readonly property bool pending: modelData.flags.indexOf("pendingDTC") >= 0

                        width: ListView.view.width - 10
                        implicitHeight: dtcColumn.implicitHeight + 12
                        radius: Theme.smallRadius
                        color: dtcHover.hovered ? Theme.alpha(Theme.accent, 0.08) : "transparent"

                        HoverHandler { id: dtcHover }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 10
                            spacing: 10

                            StatusDot {
                                status: dtcRow.confirmed ? "error" : dtcRow.pending ? "warning" : "idle"
                                Layout.alignment: Qt.AlignTop
                                Layout.topMargin: 4
                            }
                            ColumnLayout {
                                id: dtcColumn
                                spacing: 2
                                Layout.fillWidth: true

                                RowLayout {
                                    spacing: 10
                                    Label {
                                        text: dtcRow.modelData.code
                                        color: Theme.text
                                        font.family: "monospace"
                                        font.bold: true
                                    }
                                    Label {
                                        text: dtcRow.modelData.status
                                        color: Theme.muted
                                        font.family: "monospace"
                                    }
                                    Label {
                                        visible: text.length > 0
                                        text: dtcRow.modelData.extra
                                        color: Theme.muted
                                    }
                                    Item { Layout.fillWidth: true }
                                    Pill {
                                        visible: dtcRow.confirmed || dtcRow.pending
                                        text: dtcRow.confirmed ? qsTr("Confirmed") : qsTr("Pending")
                                        tint: dtcRow.confirmed ? Theme.danger : Theme.warning
                                    }
                                    Button {
                                        text: qsTr("Details")
                                        flat: true
                                        enabled: root.ready
                                        ToolTip.visible: hovered
                                        ToolTip.text: qsTr("Read the snapshot (freeze frame) and extended data records of this DTC (0x19 0x04, 0x06)")
                                        onClicked: root.report(root.diagnostics.read_dtc_details(dtcRow.modelData.value))
                                    }
                                }
                                Label {
                                    visible: text.length > 0
                                    text: dtcRow.modelData.flags
                                    color: Theme.muted
                                    font.pixelSize: 11
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                                Label {
                                    visible: text.length > 0
                                    text: dtcRow.modelData.details
                                    color: Theme.text
                                    font.family: "monospace"
                                    font.pixelSize: 11
                                    wrapMode: Text.WrapAnywhere
                                    Layout.fillWidth: true
                                }
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
                    CheckBox {
                        visible: root.diagnostics.listening_available
                        text: qsTr("Listen between requests")
                        checked: root.diagnostics.listening
                        enabled: root.connected
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Also show what the ECU sends between requests (event-triggered responses, late answers). It answers flow control on the response ID: turn it off while another tool (e.g. the OBD page) uses the same IDs")
                        onToggled: {
                            root.report(root.diagnostics.set_listening(checked))
                            checked = Qt.binding(() => root.diagnostics.listening)
                        }
                    }
                    Button {
                        text: qsTr("Clear")
                        flat: true
                        enabled: history.count > 0
                        onClicked: root.diagnostics.clear_history()
                    }
                }

                EmptyHint {
                    visible: history.count === 0
                    text: qsTr("Requests and the ECU's responses appear here.")
                }
                ListView {
                    id: history
                    visible: count > 0
                    clip: true
                    spacing: 6
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ScrollBar.vertical: ScrollBar {}
                    model: root.diagnostics.history
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
                                Label {
                                    text: entry.modelData.unsolicited ? "↯" : "→"
                                    color: entry.modelData.unsolicited ? Theme.accent : Theme.muted
                                    font.family: "monospace"
                                }
                                Label {
                                    text: entry.modelData.request_description || entry.modelData.request
                                    color: entry.modelData.unsolicited ? Theme.accent : Theme.text
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
                                visible: entry.modelData.request !== ""
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
        id: descriptionDialog
        title: qsTr("ECU diagnostic description")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("CANdelaStudio descriptions (*.cdd)"), qsTr("All files (*)")]
        onAccepted: root.report(root.diagnostics.load_description(selectedFile))
    }

    FileChooser {
        id: securedProviderDialog
        title: qsTr("Secured transmission provider")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: {
            securedSection.providerFile = root.diagnostics.secured_provider_path(selectedFile)
            securedSection.apply()
        }
    }

    FileChooser {
        id: keyFileDialog
        title: qsTr("Seed/key algorithm")
        fileMode: FileChooser.OpenFile
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: root.diagnostics.set_key_file(selectedFile)
    }
}
