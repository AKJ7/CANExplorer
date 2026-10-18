import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0

// New releases on GitHub: the version, its release notes, and downloading and installing it.
Dialog {
    id: dialog
    readonly property var updates: connectionManager.updates
    readonly property string phase: updates.state
    readonly property bool busy: phase === "checking" || phase === "downloading" || phase === "installing"

    title: phase === "available" || phase === "downloading" || phase === "installing"
           ? qsTr("CANExplorer %1 is available").arg(updates.version) : qsTr("Updates")
    modal: true
    anchors.centerIn: parent
    width: Math.min(640, parent.width - 48)
    closePolicy: busy ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    onClosed: updates.dismiss()

    // Check, then show the result
    function checkNow() {
        updates.check()
        open()
    }

    contentItem: ColumnLayout {
        spacing: 12

        // Checking, or no update
        RowLayout {
            visible: dialog.phase === "checking" || dialog.phase === "current" || dialog.phase === "idle"
            spacing: 12
            BusyIndicator {
                running: dialog.phase === "checking"
                visible: running
                Layout.preferredWidth: 32
                Layout.preferredHeight: 32
            }
            Label {
                text: dialog.phase === "checking" ? qsTr("Looking for new releases…")
                      : qsTr("CANExplorer %1 is up to date.").arg(dialog.updates.current_version)
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }

        // An update
        ColumnLayout {
            visible: dialog.phase === "available" || dialog.phase === "downloading" || dialog.phase === "installing"
            spacing: 8
            Layout.fillWidth: true

            Label {
                text: qsTr("You have %1. Version %2 was released on %3%4.")
                    .arg(dialog.updates.current_version).arg(dialog.updates.version).arg(dialog.updates.published)
                    .arg(dialog.updates.prerelease ? qsTr(" (pre-release)") : "")
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Frame {
                Layout.fillWidth: true
                Layout.preferredHeight: 260
                padding: 0
                ScrollView {
                    anchors.fill: parent
                    clip: true
                    Label {
                        width: dialog.availableWidth - 24
                        padding: 12
                        text: dialog.updates.notes || qsTr("No release notes.")
                        textFormat: Text.MarkdownText
                        wrapMode: Text.Wrap
                        onLinkActivated: link => Qt.openUrlExternally(link)
                    }
                }
            }
            Label {
                visible: text.length > 0
                text: dialog.updates.install_hint
                color: Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            ColumnLayout {
                visible: dialog.phase === "downloading" || dialog.phase === "installing"
                Layout.fillWidth: true
                ProgressBar {
                    value: dialog.updates.progress
                    indeterminate: dialog.phase === "installing" || dialog.updates.progress === 0
                    Layout.fillWidth: true
                }
                Label {
                    text: dialog.phase === "installing" ? qsTr("Installing; CANExplorer restarts in a moment…")
                          : qsTr("Downloading %1").arg(dialog.updates.progress_text)
                    color: Theme.muted
                }
            }
        }

        Label {
            visible: dialog.phase === "error"
            text: dialog.updates.error
            color: Theme.danger
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        // Buttons
        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Button {
                text: qsTr("Skip this version")
                visible: dialog.phase === "available"
                flat: true
                onClicked: {
                    dialog.updates.skip()
                    dialog.close()
                }
            }
            Button {
                text: qsTr("Release page")
                visible: dialog.phase === "available" || dialog.phase === "error"
                flat: true
                onClicked: dialog.updates.open_release_page()
            }
            Item { Layout.fillWidth: true }
            Button {
                text: qsTr("Check again")
                visible: dialog.phase === "error" || dialog.phase === "current"
                onClicked: dialog.updates.check()
            }
            Button {
                text: qsTr("Cancel")
                visible: dialog.phase === "downloading"
                onClicked: dialog.updates.cancel()
            }
            Button {
                text: dialog.phase === "available" ? qsTr("Later") : qsTr("Close")
                visible: !dialog.busy
                onClicked: dialog.close()
            }
            Button {
                text: qsTr("Install and restart")
                visible: dialog.phase === "available" && dialog.updates.can_install
                highlighted: true
                onClicked: dialog.updates.install()
            }
        }
    }
}
