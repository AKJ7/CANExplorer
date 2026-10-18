import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs 6.2
import QtQuick.Layouts 1.15


SplitView {
    id: root
    required property var connection
    readonly property var simulation: connection.simulation

    orientation: Qt.Vertical

    function report(error) {
        if (error)
            root.simulation.panel_error("simulation", error)
    }

    SplitView {
        SplitView.fillHeight: true

        ColumnLayout {
            SplitView.preferredWidth: 340
            SplitView.minimumWidth: 240
            spacing: 4

            RowLayout {
                Layout.margins: 4
                Button {
                    text: root.simulation.running ? qsTr("Stop simulation") : qsTr("Start simulation")
                    highlighted: !root.simulation.running
                    enabled: root.connection.connected
                    onClicked: root.simulation.running ? root.simulation.stop() : root.report(root.simulation.start())
                }
                Button {
                    text: qsTr("Load script…")
                    onClicked: scriptDialog.open()
                }
            }

            Label { text: qsTr("Scripts"); font.bold: true; leftPadding: 4 }
            ListView {
                id: scriptList
                clip: true
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(contentHeight, 220)
                model: root.simulation.scripts
                delegate: ItemDelegate {
                    required property var modelData
                    required property int index
                    width: scriptList.width
                    contentItem: RowLayout {
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0
                            Label { text: modelData.name; font.bold: true }
                            Label {
                                text: modelData.error || qsTr("Nodes: %1").arg(modelData.nodes)
                                color: modelData.error ? "tomato" : palette.text
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                        ToolButton {
                            text: qsTr("Reload")
                            onClicked: root.simulation.reload_script(index)
                        }
                        ToolButton {
                            text: "✕"
                            onClicked: root.simulation.remove_script(index)
                        }
                    }
                }
            }

            Label { text: qsTr("Variables"); font.bold: true; leftPadding: 4 }
            RowLayout {
                Layout.leftMargin: 4
                Layout.rightMargin: 4
                TextField { id: variableName; placeholderText: qsTr("name"); Layout.preferredWidth: 110 }
                TextField {
                    id: variableValue
                    placeholderText: qsTr("value")
                    Layout.fillWidth: true
                    onAccepted: root.report(root.simulation.set_variable(variableName.text, text))
                }
                Button {
                    text: qsTr("Set")
                    onClicked: root.report(root.simulation.set_variable(variableName.text, variableValue.text))
                }
            }
            ListView {
                id: variableList
                clip: true
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.simulation.variables
                delegate: ItemDelegate {
                    required property var modelData
                    width: variableList.width
                    text: `${modelData.name} = ${modelData.value}`
                    font.family: "monospace"
                    onClicked: {
                        variableName.text = modelData.name
                        variableValue.text = modelData.value
                    }
                }
            }
        }

        ColumnLayout {
            SplitView.fillWidth: true
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 4
                TabBar {
                    id: panelTabs
                    Layout.fillWidth: true
                    Repeater {
                        model: root.simulation.panels
                        TabButton {
                            required property var modelData
                            text: modelData.name
                            width: implicitWidth
                        }
                    }
                }
                Button {
                    text: qsTr("Add panel…")
                    onClicked: panelDialog.open()
                }
                Button {
                    text: qsTr("Reload panels")
                    enabled: root.simulation.panels.length > 0
                    onClicked: root.simulation.reload_panels(root)
                }
                Button {
                    text: qsTr("Close panel")
                    enabled: root.simulation.panels.length > 0
                    onClicked: root.simulation.remove_panel(panelTabs.currentIndex)
                }
            }

            Label {
                visible: root.simulation.panels.length === 0
                text: qsTr("Panels are QML files with a 'property var panel'. See examples/simulation.")
                padding: 8
            }

            StackLayout {
                currentIndex: panelTabs.currentIndex
                Layout.fillWidth: true
                Layout.fillHeight: true

                Repeater {
                    model: root.simulation.panels
                    Item {
                        id: host
                        required property var modelData
                        property var item: null

                        Component.onCompleted: {
                            const component = Qt.createComponent(modelData.url)
                            if (component.status === Component.Ready) {
                                item = component.createObject(host, { panel: root.simulation.panel })
                                if (item)
                                    item.anchors.fill = host
                                else
                                    root.simulation.panel_error(modelData.name, component.errorString())
                            } else {
                                root.simulation.panel_error(modelData.name, component.errorString())
                            }
                        }
                        Component.onDestruction: if (item) item.destroy()
                    }
                }
            }
        }
    }

    ColumnLayout {
        SplitView.preferredHeight: 160
        SplitView.minimumHeight: 60
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Label { text: qsTr("Console"); font.bold: true; leftPadding: 4 }
            Item { Layout.fillWidth: true }
            Button {
                text: qsTr("Clear")
                flat: true
                onClicked: root.simulation.clear_console()
            }
        }
        ListView {
            id: consoleList
            clip: true
            Layout.fillWidth: true
            Layout.fillHeight: true
            ScrollBar.vertical: ScrollBar {}
            model: root.simulation.console
            onCountChanged: positionViewAtEnd()
            delegate: Label {
                required property var modelData
                width: consoleList.width
                text: `${modelData.time} [${modelData.source}] ${modelData.text}`
                color: modelData.level === "error" ? "tomato" : palette.text
                font.family: "monospace"
                wrapMode: Text.WrapAnywhere
                leftPadding: 4
            }
        }
    }

    FileDialog {
        id: scriptDialog
        title: qsTr("Load simulation script")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("Python scripts (*.py)")]
        onAccepted: root.simulation.load_script(selectedFile)
    }

    FileDialog {
        id: panelDialog
        title: qsTr("Add panel")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("QML panels (*.qml)")]
        onAccepted: root.report(root.simulation.add_panel(selectedFile))
    }
}
