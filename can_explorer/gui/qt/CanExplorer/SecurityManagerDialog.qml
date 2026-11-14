import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// The security manager: a password-protected key store with SecOC keys, TLS certificates for DoIP and the IPsec
// and MACsec associations that decrypt Ethernet captures. Secrets never reach QML: entries show metadata only.
Dialog {
    id: dialog
    readonly property var security: connectionManager.security
    // The kind of entry being added
    readonly property var purposes: [
        { value: "secoc", text: qsTr("SecOC key") },
        { value: "tls-client", text: qsTr("TLS client certificate") },
        { value: "tls-ca", text: qsTr("Trusted TLS certificates") },
        { value: "crl", text: qsTr("Revocation list") },
        { value: "ipsec", text: qsTr("IPsec ESP association") },
        { value: "macsec", text: qsTr("MACsec association") },
        { value: "seed-key", text: qsTr("Seed/key algorithm") },
        { value: "uds-auth-key", text: qsTr("UDS authentication key") },
        { value: "key-server", text: qsTr("Key server") },
    ]
    readonly property var trustedSets: security.entries.filter(entry => entry.purpose === "tls-ca").map(entry => entry.name)
    property string purpose: "secoc"
    property url certificateFile
    property url keyFile
    property url scriptFile
    property var selected: []  // Entry names chosen for export

    title: qsTr("Security Manager")
    modal: true
    anchors.centerIn: Overlay.overlay
    standardButtons: Dialog.Close
    width: Math.min(680, (Overlay.overlay ? Overlay.overlay.width : 680) - 40)
    height: Math.min(implicitHeight, (Overlay.overlay ? Overlay.overlay.height : 800) - 40)

    function report(error) {
        statusLabel.text = error
        statusLabel.color = Theme.danger
    }

    function done(error, message) {
        if (error)
            report(error)
        else {
            statusLabel.text = message
            statusLabel.color = Theme.muted
        }
        return !error
    }

    function clearSecrets() {
        for (const field of [passwordField, repeatField, oldPasswordField, newPasswordField, newRepeatField,
                             nameField, secocKeyField, keyPasswordField, encryptionKeyField, integrityKeyField,
                             sakField, sciField, spiField, authKeyField, serverTokenField])
            field.text = ""
        certificateFile = ""
        keyFile = ""
        scriptFile = ""
        unlockField.text = ""
        bundlePassphrase.text = ""
        bundleRepeat.text = ""
    }

    function addEntry() {
        const name = nameField.text
        let error = ""
        switch (purpose) {
        case "secoc":
            error = security.add_secoc_key(name, secocKeyField.text, scopeField.text,
                                           algorithmBox.currentIndex === 0 ? "" : algorithmBox.currentText)
            break
        case "tls-client":
            if (certificateFile.toString() === "" || keyFile.toString() === "")
                error = qsTr("Choose the certificate and its private key")
            else
                error = security.add_tls_client(name, certificateFile, keyFile, keyPasswordField.text)
            break
        case "tls-ca":
            error = certificateFile.toString() === "" ? qsTr("Choose the certificate file")
                                                      : security.add_tls_ca(name, certificateFile)
            break
        case "crl":
            error = certificateFile.toString() === "" ? qsTr("Choose the revocation list file")
                                                      : security.add_crl(name, certificateFile)
            break
        case "ipsec":
            error = security.add_ipsec_sa(name, spiField.text, encryptionBox.currentText, encryptionKeyField.text,
                                          integrityBox.currentText, integrityKeyField.text)
            break
        case "macsec":
            error = security.add_macsec_sa(name, sakField.text, sciField.text)
            break
        case "seed-key":
            error = scriptFile.toString() === "" ? qsTr("Choose the Python file defining compute_key(level, seed)")
                                                 : security.add_seed_key(name, scriptFile)
            break
        case "uds-auth-key":
            error = security.add_auth_key(name, authKeyField.text, authMacBox.currentText)
            break
        case "key-server":
            error = security.add_key_server(name, serverUrlField.text, serverTokenField.text,
                                            serverTrustedBox.currentIndex > 0 ? serverTrustedBox.currentText : "")
            break
        }
        if (done(error, qsTr("Added %1").arg(name)))
            clearSecrets()
    }

    onAboutToShow: {
        statusLabel.text = ""
        dialog.security.refresh()
    }
    onClosed: clearSecrets()

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: Theme.spacing

            Label {
                text: qsTr("Keys and certificates for SecOC, DoIP over TLS, UDS authentication and seed/key access, and decrypting IPsec and MACsec in Ethernet captures. They are stored encrypted with your password.")
                color: Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            // Create, unlock or lock
            RowLayout {
                visible: !dialog.security.exists
                spacing: 8
                Layout.fillWidth: true
                Label { text: qsTr("New password") }
                TextField { id: passwordField; echoMode: TextInput.Password; Layout.fillWidth: true }
                Label { text: qsTr("Repeat") }
                TextField {
                    id: repeatField
                    echoMode: TextInput.Password
                    Layout.fillWidth: true
                    onAccepted: createButton.clicked()
                }
                Button {
                    id: createButton
                    text: qsTr("Create Key Store")
                    highlighted: true
                    onClicked: {
                        if (dialog.done(dialog.security.create(passwordField.text, repeatField.text),
                                        qsTr("Key store created")))
                            dialog.clearSecrets()
                    }
                }
            }
            RowLayout {
                visible: dialog.security.exists && !dialog.security.unlocked
                spacing: 8
                Layout.fillWidth: true
                Label { text: qsTr("Password") }
                TextField {
                    id: unlockField
                    echoMode: TextInput.Password
                    Layout.fillWidth: true
                    onAccepted: unlockButton.clicked()
                }
                CheckBox {
                    id: rememberBox
                    visible: dialog.security.keychain_available
                    text: qsTr("Remember")
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Keep the password in the operating system's keychain, so the key store unlocks when CANExplorer starts")
                }
                Button {
                    id: unlockButton
                    text: qsTr("Unlock")
                    highlighted: true
                    onClicked: {
                        const ok = dialog.done(dialog.security.unlock_and_remember(unlockField.text, rememberBox.checked), qsTr("Unlocked"))
                        unlockField.text = ""
                        if (!ok)
                            unlockField.forceActiveFocus()
                    }
                }
            }
            RowLayout {
                visible: dialog.security.unlocked
                spacing: 8
                Layout.fillWidth: true
                Label {
                    text: dialog.security.remembered
                        ? qsTr("Unlocked; the keychain keeps the password, so the store unlocks at start.")
                        : qsTr("Unlocked: the keys are in use until you lock the store or quit.")
                    color: Theme.success
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
                Button {
                    text: qsTr("Forget Password")
                    visible: dialog.security.remembered
                    onClicked: { dialog.security.forget_password(); dialog.done("", qsTr("The keychain no longer keeps the password")) }
                }
                Button { text: qsTr("Change Password…"); onClicked: passwordRow.visible = !passwordRow.visible }
                Button { text: qsTr("Lock"); onClicked: { dialog.security.lock(); dialog.done("", qsTr("Locked")) } }
            }
            RowLayout {
                id: passwordRow
                visible: false
                spacing: 8
                Layout.fillWidth: true
                TextField { id: oldPasswordField; echoMode: TextInput.Password; placeholderText: qsTr("Current"); Layout.fillWidth: true }
                TextField { id: newPasswordField; echoMode: TextInput.Password; placeholderText: qsTr("New"); Layout.fillWidth: true }
                TextField { id: newRepeatField; echoMode: TextInput.Password; placeholderText: qsTr("Repeat new"); Layout.fillWidth: true }
                Button {
                    text: qsTr("Change")
                    onClicked: {
                        if (dialog.done(dialog.security.change_password(oldPasswordField.text, newPasswordField.text,
                                                                         newRepeatField.text), qsTr("Password changed"))) {
                            dialog.clearSecrets()
                            passwordRow.visible = false
                        }
                    }
                }
            }

            Label {
                id: statusLabel
                visible: text.length > 0
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            // Entries
            Label { text: qsTr("Entries"); font.bold: true; visible: dialog.security.exists }
            Label {
                visible: dialog.security.exists && dialog.security.entries.length === 0
                text: qsTr("No entries yet.")
                color: Theme.muted
            }
            Repeater {
                model: dialog.security.entries
                delegate: RowLayout {
                    required property var modelData
                    spacing: 8
                    Layout.fillWidth: true
                    CheckBox {
                        visible: dialog.security.unlocked
                        checked: dialog.selected.indexOf(modelData.name) >= 0
                        onToggled: dialog.selected = checked ? dialog.selected.concat([modelData.name])
                                                             : dialog.selected.filter(name => name !== modelData.name)
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("Select to export")
                    }
                    Label { text: modelData.kind; color: Theme.muted; Layout.preferredWidth: 170; elide: Text.ElideRight }
                    Label { text: modelData.name; font.bold: true; Layout.preferredWidth: 140; elide: Text.ElideRight }
                    Label {
                        text: modelData.details
                        color: modelData.expired ? Theme.danger : Theme.text
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        ToolTip.visible: detailsHover.hovered && truncated
                        ToolTip.text: modelData.details
                        HoverHandler { id: detailsHover }
                    }
                    Button {
                        text: qsTr("Remove")
                        flat: true
                        onClicked: dialog.done(dialog.security.remove(modelData.name), qsTr("Removed %1").arg(modelData.name))
                    }
                }
            }

            // Moving entries to another computer: a bundle encrypted with its own passphrase
            RowLayout {
                visible: dialog.security.unlocked
                spacing: 8
                Layout.fillWidth: true
                Button {
                    text: dialog.selected.length > 0 ? qsTr("Export %1…").arg(dialog.selected.length) : qsTr("Export…")
                    enabled: dialog.selected.length > 0
                    onClicked: { bundleRow.importing = false; bundleRow.visible = true }
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Write the selected entries to a file encrypted with a passphrase of its own, for another computer or a colleague")
                }
                Button { text: qsTr("Import…"); onClicked: { bundleRow.importing = true; bundleRow.visible = true } }
                Item { Layout.fillWidth: true }
                Button { text: qsTr("Create Test Certificates…"); onClicked: pkiGrid.visible = !pkiGrid.visible }
            }
            RowLayout {
                id: bundleRow
                property bool importing: false
                visible: false
                spacing: 8
                Layout.fillWidth: true
                TextField { id: bundlePassphrase; echoMode: TextInput.Password; placeholderText: qsTr("Passphrase"); Layout.fillWidth: true }
                TextField {
                    id: bundleRepeat
                    visible: !bundleRow.importing
                    echoMode: TextInput.Password
                    placeholderText: qsTr("Repeat")
                    Layout.fillWidth: true
                }
                CheckBox { id: replaceBox; visible: bundleRow.importing; text: qsTr("Replace existing") }
                Button {
                    text: bundleRow.importing ? qsTr("Choose File and Import") : qsTr("Choose File and Export")
                    highlighted: true
                    onClicked: bundleRow.importing ? importChooser.open() : exportChooser.open()
                }
            }

            // A test PKI: a CA with a tester and an ECU certificate
            GridLayout {
                id: pkiGrid
                visible: false
                columns: 4
                columnSpacing: 8
                rowSpacing: 6
                Layout.fillWidth: true
                Label { text: qsTr("Name") }
                TextField { id: pkiPrefix; text: "Test"; Layout.fillWidth: true }
                Label { text: qsTr("Key type") }
                ComboBox { id: pkiKeyType; model: dialog.security.test_key_types; Layout.fillWidth: true }
                Label { text: qsTr("Valid for (days)") }
                TextField { id: pkiDays; text: "365"; validator: IntValidator { bottom: 1; top: 36500 } Layout.fillWidth: true }
                Label { text: qsTr("ECU addresses") }
                TextField {
                    id: pkiAddresses
                    text: "127.0.0.1"
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("IP addresses or host names of the ECU certificate (subject alternative names), separated by commas")
                }
                Label {
                    text: qsTr("Adds '<name> CA' (trusted certificates), '<name> tester' and '<name> ECU' (certificates with their keys) — for benches and simulators, not for vehicles.")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.columnSpan: 3
                    Layout.fillWidth: true
                }
                Button {
                    text: qsTr("Create")
                    highlighted: true
                    onClicked: {
                        if (dialog.done(dialog.security.create_test_pki({
                            prefix: pkiPrefix.text, key_type: pkiKeyType.currentText, days: pkiDays.text,
                            addresses: pkiAddresses.text,
                        }), qsTr("Created the test certificates")))
                            pkiGrid.visible = false
                    }
                }
            }

            // Adding an entry
            Label { text: qsTr("Add"); font.bold: true; visible: dialog.security.unlocked }
            GridLayout {
                visible: dialog.security.unlocked
                columns: 2
                columnSpacing: 8
                rowSpacing: 6
                Layout.fillWidth: true

                Label { text: qsTr("Kind") }
                ComboBox {
                    id: purposeBox
                    model: dialog.purposes
                    textRole: "text"
                    valueRole: "value"
                    currentIndex: dialog.purposes.findIndex(item => item.value === dialog.purpose)
                    Layout.preferredWidth: 240
                    onActivated: dialog.purpose = currentValue
                }
                Label { text: qsTr("Name") }
                TextField { id: nameField; placeholderText: qsTr("e.g. Powertrain SecOC"); Layout.fillWidth: true }

                // SecOC
                Label { text: qsTr("Key"); visible: dialog.purpose === "secoc" }
                TextField {
                    id: secocKeyField
                    visible: dialog.purpose === "secoc"
                    echoMode: TextInput.Password
                    placeholderText: qsTr("hex, e.g. 000102030405060708090A0B0C0D0E0F")
                    font.family: "monospace"
                    Layout.fillWidth: true
                }
                Label { text: qsTr("Used for"); visible: dialog.purpose === "secoc" }
                TextField {
                    id: scopeField
                    visible: dialog.purpose === "secoc"
                    text: "*"
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("* for every secured PDU, or PDU names and Data IDs separated by commas (e.g. EngineData, 0x12). A key named for a PDU wins over one for its Data ID, which wins over *")
                }
                Label { text: qsTr("Algorithm"); visible: dialog.purpose === "secoc" }
                ComboBox {
                    id: algorithmBox
                    visible: dialog.purpose === "secoc"
                    model: [qsTr("As named in the ARXML")].concat(dialog.security.secoc_algorithms)
                    Layout.preferredWidth: 240
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("ARXML names containing CMAC are CMAC/AES, HMAC with SHA256 is HMAC-SHA-256. Choose one here for PDUs whose algorithm name is not recognised")
                }

                // TLS
                Label {
                    text: dialog.purpose === "crl" ? qsTr("Revocation list") : qsTr("Certificate")
                    visible: dialog.purpose === "tls-client" || dialog.purpose === "tls-ca" || dialog.purpose === "crl"
                }
                RowLayout {
                    visible: dialog.purpose === "tls-client" || dialog.purpose === "tls-ca" || dialog.purpose === "crl"
                    Layout.fillWidth: true
                    Button { text: qsTr("Choose…"); onClicked: certificateChooser.open() }
                    Label {
                        text: dialog.certificateFile.toString() ? decodeURIComponent(dialog.certificateFile.toString().replace(/^file:\/\//, "")) : qsTr("PEM file")
                        color: dialog.certificateFile.toString() ? Theme.text : Theme.muted
                        elide: Text.ElideMiddle
                        Layout.fillWidth: true
                    }
                }
                Label { text: qsTr("Private key"); visible: dialog.purpose === "tls-client" }
                RowLayout {
                    visible: dialog.purpose === "tls-client"
                    Layout.fillWidth: true
                    Button { text: qsTr("Choose…"); onClicked: keyChooser.open() }
                    Label {
                        text: dialog.keyFile.toString() ? decodeURIComponent(dialog.keyFile.toString().replace(/^file:\/\//, "")) : qsTr("PEM file")
                        color: dialog.keyFile.toString() ? Theme.text : Theme.muted
                        elide: Text.ElideMiddle
                        Layout.fillWidth: true
                    }
                }
                Label { text: qsTr("Key password"); visible: dialog.purpose === "tls-client" }
                TextField {
                    id: keyPasswordField
                    visible: dialog.purpose === "tls-client"
                    echoMode: TextInput.Password
                    placeholderText: qsTr("if the private key file is encrypted")
                    Layout.fillWidth: true
                }

                // IPsec
                Label { text: qsTr("SPI"); visible: dialog.purpose === "ipsec" }
                TextField {
                    id: spiField
                    visible: dialog.purpose === "ipsec"
                    placeholderText: qsTr("hex, e.g. 0x00001000")
                    font.family: "monospace"
                    Layout.preferredWidth: 220
                }
                Label { text: qsTr("Encryption"); visible: dialog.purpose === "ipsec" }
                RowLayout {
                    visible: dialog.purpose === "ipsec"
                    Layout.fillWidth: true
                    ComboBox { id: encryptionBox; model: dialog.security.esp_encryptions; Layout.preferredWidth: 120 }
                    TextField {
                        id: encryptionKeyField
                        visible: encryptionBox.currentText !== "null"
                        echoMode: TextInput.Password
                        placeholderText: encryptionBox.currentText === "aes-gcm" ? qsTr("key and 4-byte salt, hex") : qsTr("key, hex")
                        font.family: "monospace"
                        Layout.fillWidth: true
                    }
                }
                Label { text: qsTr("Integrity"); visible: dialog.purpose === "ipsec" }
                RowLayout {
                    visible: dialog.purpose === "ipsec"
                    Layout.fillWidth: true
                    ComboBox { id: integrityBox; model: dialog.security.esp_integrities; Layout.preferredWidth: 160 }
                    TextField {
                        id: integrityKeyField
                        visible: integrityBox.currentText !== "none"
                        echoMode: TextInput.Password
                        placeholderText: qsTr("key, hex")
                        font.family: "monospace"
                        Layout.fillWidth: true
                    }
                }

                // MACsec
                Label { text: qsTr("SAK"); visible: dialog.purpose === "macsec" }
                TextField {
                    id: sakField
                    visible: dialog.purpose === "macsec"
                    echoMode: TextInput.Password
                    placeholderText: qsTr("16 bytes (GCM-AES-128) or 32 (GCM-AES-256), hex")
                    font.family: "monospace"
                    Layout.fillWidth: true
                }
                Label { text: qsTr("SCI"); visible: dialog.purpose === "macsec" }
                TextField {
                    id: sciField
                    visible: dialog.purpose === "macsec"
                    placeholderText: qsTr("optional: MAC address and port, e.g. 0011223344550001")
                    font.family: "monospace"
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Leave empty to try this key on every secure channel. Needed for frames that carry no SCI and do not come from an end station")
                }

                // Seed/key
                Label { text: qsTr("Script"); visible: dialog.purpose === "seed-key" }
                RowLayout {
                    visible: dialog.purpose === "seed-key"
                    Layout.fillWidth: true
                    Button { text: qsTr("Choose…"); onClicked: scriptChooser.open() }
                    Label {
                        text: dialog.scriptFile.toString() ? decodeURIComponent(dialog.scriptFile.toString().replace(/^file:\/\//, "")) : qsTr("Python file defining compute_key(level, seed)")
                        color: dialog.scriptFile.toString() ? Theme.text : Theme.muted
                        elide: Text.ElideMiddle
                        Layout.fillWidth: true
                        ToolTip.visible: scriptHover.hovered
                        ToolTip.text: qsTr("The script is stored encrypted; the Diagnostics and GMLAN views can use it for security access without the file")
                        HoverHandler { id: scriptHover }
                    }
                }

                // UDS authentication by challenge-response
                Label { text: qsTr("Key"); visible: dialog.purpose === "uds-auth-key" }
                RowLayout {
                    visible: dialog.purpose === "uds-auth-key"
                    Layout.fillWidth: true
                    TextField {
                        id: authKeyField
                        echoMode: TextInput.Password
                        placeholderText: qsTr("hex, the key shared with the ECUs")
                        font.family: "monospace"
                        Layout.fillWidth: true
                    }
                    ComboBox {
                        id: authMacBox
                        model: dialog.security.auth_key_macs
                        Layout.preferredWidth: 140
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr("The proof of ownership is this MAC over the ECU's challenge (a choice: the standard leaves it to the manufacturer)")
                    }
                }

                // Key server
                Label { text: qsTr("URL"); visible: dialog.purpose === "key-server" }
                TextField {
                    id: serverUrlField
                    visible: dialog.purpose === "key-server"
                    placeholderText: qsTr("https://keys.example.com/api")
                    Layout.fillWidth: true
                }
                Label { text: qsTr("Token"); visible: dialog.purpose === "key-server" }
                TextField {
                    id: serverTokenField
                    visible: dialog.purpose === "key-server"
                    echoMode: TextInput.Password
                    placeholderText: qsTr("bearer token, kept encrypted (optional)")
                    Layout.fillWidth: true
                }
                Label { text: qsTr("Server certificate"); visible: dialog.purpose === "key-server" }
                ComboBox {
                    id: serverTrustedBox
                    visible: dialog.purpose === "key-server"
                    model: [qsTr("Checked against the system's certificates")].concat(dialog.trustedSets)
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Trusted certificates of the Security Manager to check the key server's HTTPS certificate against")
                }

                Item { width: 1; height: 1 }
                Button { text: qsTr("Add"); highlighted: true; onClicked: dialog.addEntry() }
            }

            // Security profiles: plug-ins and key servers supplying keys, seed/key, certificates and signatures
            RowLayout {
                spacing: 8
                Layout.fillWidth: true
                Label { text: qsTr("Security profiles"); font.bold: true; Layout.fillWidth: true }
                Button {
                    text: qsTr("Load Profile…")
                    onClicked: profileChooser.open()
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("A Python file defining profile or create_profile(); it is executed, so load only profiles you trust")
                }
            }
            Label {
                visible: dialog.security.security_profiles.length === 0
                text: qsTr("None. A profile supplies SecOC keys, freshness values, seed/key, certificates and signatures — from a manufacturer's plug-in, or a key server added above, so the secrets need not be on this computer.")
                color: Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Repeater {
                model: dialog.security.security_profiles
                delegate: RowLayout {
                    required property var modelData
                    readonly property bool keyServer: modelData.source.startsWith("key server ")
                    spacing: 8
                    Layout.fillWidth: true
                    Label { text: modelData.name; font.bold: true; Layout.preferredWidth: 140; elide: Text.ElideRight }
                    Label {
                        text: modelData.capabilities.join(", ") || qsTr("no capabilities")
                        color: modelData.capabilities.length > 0 ? Theme.text : Theme.warning
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        ToolTip.visible: profileHover.hovered
                        ToolTip.text: (modelData.description ? modelData.description + "\n" : "") + qsTr("From %1").arg(modelData.source)
                        HoverHandler { id: profileHover }
                    }
                    Button {
                        text: qsTr("Remove")
                        flat: true
                        visible: !keyServer && !modelData.source.startsWith("entry point ")
                        onClicked: { dialog.security.remove_profile(modelData.name); dialog.done("", qsTr("Removed %1").arg(modelData.name)) }
                    }
                }
            }

            // SecOC freshness values
            Label { text: qsTr("SecOC freshness values"); font.bold: true }
            RowLayout {
                spacing: 8
                Layout.fillWidth: true
                Label {
                    text: dialog.security.freshness_mode === "script"
                          ? qsTr("From %1").arg(dialog.security.freshness_script)
                          : dialog.security.freshness_mode === "trip-reset"
                          ? qsTr("AUTOSAR trip/reset counters, synchronised by %1").arg(dialog.security.trip_reset.sync_pdu || qsTr("(no synchronisation message)"))
                          : qsTr("Counters: the high bits continue from the last verified frame of each PDU")
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Button { text: qsTr("Script…"); onClicked: freshnessChooser.open() }
                Button {
                    text: qsTr("Trip/Reset…")
                    onClicked: tripResetGrid.visible = !tripResetGrid.visible
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Freshness values from a trip counter, a reset counter and a message counter, synchronised by a TripResetSyncMsg (AUTOSAR SecOC, Annex A)")
                }
                Button {
                    text: qsTr("Counters")
                    visible: dialog.security.freshness_mode !== "counter"
                    onClicked: dialog.done(dialog.security.set_freshness_script(""), qsTr("Using counters"))
                }
            }
            GridLayout {
                id: tripResetGrid
                visible: false
                columns: 4
                columnSpacing: 8
                rowSpacing: 6
                Layout.fillWidth: true
                Label { text: qsTr("Trip counter (bits)") }
                TextField { id: tripBits; text: String(dialog.security.trip_reset.trip_bits); validator: IntValidator { bottom: 1; top: 24 } Layout.fillWidth: true }
                Label { text: qsTr("Reset counter (bits)") }
                TextField { id: resetBits; text: String(dialog.security.trip_reset.reset_bits); validator: IntValidator { bottom: 1; top: 24 } Layout.fillWidth: true }
                Label { text: qsTr("Message counter (bits)") }
                TextField { id: messageBits; text: String(dialog.security.trip_reset.message_bits); validator: IntValidator { bottom: 1; top: 48 } Layout.fillWidth: true }
                Label { text: qsTr("Reset flag (bits)") }
                TextField { id: flagBits; text: String(dialog.security.trip_reset.flag_bits); validator: IntValidator { bottom: 0; top: 2 } Layout.fillWidth: true }
                Label { text: qsTr("Sync message") }
                TextField {
                    id: syncPdu
                    text: dialog.security.trip_reset.sync_pdu
                    placeholderText: qsTr("secured PDU name, e.g. TripResetSyncMsg")
                    Layout.fillWidth: true
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("The secured PDU of the database carrying the trip and reset counters (trip counter | reset counter | authenticator)")
                }
                Label { text: qsTr("Clear acceptance window") }
                TextField { id: clearWindow; text: String(dialog.security.trip_reset.clear_acceptance_window); validator: IntValidator { bottom: 0 } Layout.fillWidth: true }
                Item { Layout.columnSpan: 3; width: 1; height: 1 }
                Button {
                    text: qsTr("Use")
                    highlighted: true
                    onClicked: {
                        if (dialog.done(dialog.security.set_trip_reset({
                            trip_bits: tripBits.text, reset_bits: resetBits.text, message_bits: messageBits.text,
                            flag_bits: flagBits.text, sync_pdu: syncPdu.text, clear_acceptance_window: clearWindow.text,
                        }), qsTr("Using the AUTOSAR trip/reset counters")))
                            tripResetGrid.visible = false
                    }
                }
            }

            // Audit log: what the security manager was used for, chained with an HMAC
            Label { text: qsTr("Audit log"); font.bold: true }
            RowLayout {
                spacing: 8
                Layout.fillWidth: true
                Label {
                    id: auditStatus
                    text: qsTr("%1 recent records").arg(dialog.security.audit_records.length)
                    color: Theme.muted
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Button {
                    text: qsTr("Verify")
                    onClicked: auditStatus.text = dialog.security.verify_audit()
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Check that no record was changed, removed or reordered (needs the key store unlocked)")
                }
                Button { text: qsTr("Export…"); onClicked: auditChooser.open() }
            }
            Repeater {
                model: dialog.security.audit_records.slice(0, 8)
                delegate: RowLayout {
                    required property var modelData
                    spacing: 8
                    Layout.fillWidth: true
                    Label { text: modelData.time.replace("T", " ").slice(0, 19); font.family: "monospace"; font.pixelSize: 11; color: Theme.muted }
                    Label { text: modelData.action; font.pixelSize: 11; color: modelData.signed ? Theme.text : Theme.warning; Layout.preferredWidth: 200; elide: Text.ElideRight }
                    Label { text: modelData.details; font.pixelSize: 11; color: Theme.muted; elide: Text.ElideRight; Layout.fillWidth: true }
                }
            }

            Label {
                text: qsTr("Stored in %1").arg(dialog.security.path)
                color: Theme.muted
                font.pixelSize: 11
                elide: Text.ElideMiddle
                Layout.fillWidth: true
            }
        }
    }

    FileChooser {
        id: certificateChooser
        title: qsTr("Certificate")
        nameFilters: [qsTr("Certificates and revocation lists (*.pem *.crt *.cer *.crl *.der)"), qsTr("All files (*)")]
        onAccepted: dialog.certificateFile = selectedFile
    }
    FileChooser {
        id: keyChooser
        title: qsTr("Private key")
        nameFilters: [qsTr("PEM private keys (*.pem *.key)"), qsTr("All files (*)")]
        onAccepted: dialog.keyFile = selectedFile
    }
    FileChooser {
        id: scriptChooser
        title: qsTr("Seed/key algorithm")
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: dialog.scriptFile = selectedFile
    }
    FileChooser {
        id: auditChooser
        title: qsTr("Export the audit log")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "jsonl"
        nameFilters: [qsTr("JSON lines (*.jsonl)")]
        onAccepted: auditStatus.text = dialog.security.export_audit(selectedFile)
    }
    FileChooser {
        id: exportChooser
        title: qsTr("Export key store entries")
        fileMode: FileChooser.SaveFile
        defaultSuffix: "ckb"
        nameFilters: [qsTr("CANExplorer key bundles (*.ckb)")]
        onAccepted: {
            if (dialog.done(dialog.security.export_entries(selectedFile, dialog.selected, bundlePassphrase.text, bundleRepeat.text),
                            qsTr("Exported %1 entries").arg(dialog.selected.length))) {
                bundlePassphrase.text = ""
                bundleRepeat.text = ""
                bundleRow.visible = false
            }
        }
    }
    FileChooser {
        id: importChooser
        title: qsTr("Import key store entries")
        nameFilters: [qsTr("CANExplorer key bundles (*.ckb)"), qsTr("All files (*)")]
        onAccepted: {
            const result = dialog.security.import_entries(selectedFile, bundlePassphrase.text, replaceBox.checked)
            const message = qsTr("Imported %1").arg(result.imported.join(", ") || qsTr("nothing"))
                + (result.skipped.length > 0 ? "; " + qsTr("kept the existing %1").arg(result.skipped.join(", ")) : "")
            if (dialog.done(result.error, message)) {
                bundlePassphrase.text = ""
                bundleRow.visible = false
            }
        }
    }
    FileChooser {
        id: profileChooser
        title: qsTr("Security profile")
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: dialog.done(dialog.security.load_profile(selectedFile), qsTr("Loaded the security profile"))
    }
    FileChooser {
        id: freshnessChooser
        title: qsTr("Freshness value script")
        nameFilters: [qsTr("Python files (*.py)")]
        onAccepted: dialog.done(dialog.security.set_freshness_script(selectedFile), qsTr("Freshness values from the script"))
    }
}
