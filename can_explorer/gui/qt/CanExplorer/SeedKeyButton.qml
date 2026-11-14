import QtQuick 2.15
import QtQuick.Controls 2.15
import CanExplorer 1.0

// Chooses a seed/key algorithm kept in the Security Manager: one of its key store or a security profile computing
// keys (e.g. a key server). Emits `chosen` with the key source: "keystore:<entry>" or "profile:<name>".
Button {
    id: button
    signal chosen(string source)
    readonly property var sources: connectionManager.security.entries
        .filter(entry => entry.purpose === "seed-key")
        .map(entry => ({ source: "keystore:" + entry.name, text: entry.name }))
        .concat(connectionManager.security.security_profiles
            .filter(profile => profile.capabilities.indexOf("compute_key") >= 0)
            .map(profile => ({ source: "profile:" + profile.name, text: qsTr("%1 (profile)").arg(profile.name) })))

    text: qsTr("From Security Manager")
    enabled: sources.length > 0
    ToolTip.visible: hovered
    ToolTip.text: sources.length > 0
        ? qsTr("Use a seed/key algorithm kept in the Security Manager (unlocked), or a security profile computing the keys")
        : qsTr("Add seed/key algorithms or security profiles in the Security Manager to keep them encrypted")
    onClicked: menu.popup()

    Menu {
        id: menu
        Instantiator {
            model: button.sources
            delegate: MenuItem {
                required property var modelData
                text: modelData.text
                onTriggered: button.chosen(modelData.source)
            }
            onObjectAdded: (index, object) => menu.insertItem(index, object)
            onObjectRemoved: (index, object) => menu.removeItem(object)
        }
    }
}
