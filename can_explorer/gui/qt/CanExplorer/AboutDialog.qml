import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


Dialog {
    id: dialog
    title: qsTr("About %1").arg(metadata.name)
    modal: true
    anchors.centerIn: parent
    standardButtons: Dialog.Ok

    // The seven havens easter egg: seven clicks on the logo awaken the hidden red diamond "Spirit" theme.
    property int spiritClicks: 0
    onOpened: spiritClicks = 0

    ColumnLayout {
        spacing: 12

        RowLayout {
            spacing: 16
            Logo {
                id: aboutLogo
                Layout.preferredWidth: 80
                Layout.preferredHeight: 80
                transformOrigin: Item.Center

                // A springy bounce on every click.
                SequentialAnimation {
                    id: bounce
                    NumberAnimation { target: aboutLogo; property: "scale"; to: 0.85; duration: 90; easing.type: Easing.OutQuad }
                    NumberAnimation { target: aboutLogo; property: "scale"; to: 1.0; duration: 260; easing.type: Easing.OutBack }
                }
                // A spin-and-pop flourish on the seventh click, when the spirit awakens.
                SequentialAnimation {
                    id: awaken
                    NumberAnimation { target: aboutLogo; property: "scale"; to: 1.3; duration: 180; easing.type: Easing.OutQuad }
                    ParallelAnimation {
                        RotationAnimation { target: aboutLogo; property: "rotation"; from: 0; to: 360; duration: 650; easing.type: Easing.InOutBack }
                        NumberAnimation { target: aboutLogo; property: "scale"; to: 1.0; duration: 650; easing.type: Easing.OutBack }
                    }
                    onStopped: aboutLogo.rotation = 0
                }

                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        if (connectionManager.app_state.spirit_unlocked) {
                            bounce.restart()
                        } else if (++dialog.spiritClicks >= 7) {
                            connectionManager.app_state.awaken_spirit()
                            awaken.restart()
                        } else {
                            bounce.restart()
                        }
                    }
                }
            }
            Label {
                text: qsTr("%1 (v%2)").arg(metadata.name).arg(metadata.version)
                font.pixelSize: 20
                font.bold: true
            }
        }

        Label {
            text: metadata.app_info
            textFormat: Text.PlainText
        }

        Label {
            visible: connectionManager.app_state.spirit_unlocked
            text: qsTr("The red diamond spirit awakens.")
            color: Theme.accent
            font.italic: true
        }

        Button {
            text: qsTr("Copy to Clipboard")
            onClicked: metadata.copy_app_info_to_clipboard()
        }
    }
}
