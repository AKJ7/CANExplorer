import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.3
import QtQuick.Layouts 1.3



MessageDialog {
    title: qsTr(`About ${metadata.name}`)
    buttons: MessageDialog.Ok
    text: qsTr(`<h2>${metadata.name} (v${metadata.version})</h2>`)
    informativeText: metadata.app_info
}

