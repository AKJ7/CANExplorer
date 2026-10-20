import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer 1.0


// Physical I/O: switch the digital outputs of an I/O adapter and watch its inputs and analogue values.
ColumnLayout {
    id: root
    readonly property var io: connectionManager.physical_io
    readonly property string pageTitle: io.device_name

    function report(error) {
        if (error)
            errorLabel.text = error
    }

    spacing: 4

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 4

        Label { text: qsTr("Device"); font.bold: true }
        ComboBox {
            id: deviceBox
            enabled: !root.io.is_open
            model: root.io.devices
            textRole: "description"
            valueRole: "id"
            Layout.preferredWidth: 280
        }
        ToolButton {
            text: "⟳"
            enabled: !root.io.is_open
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Look for devices again")
            onClicked: root.io.refresh_devices()
        }
        Button {
            text: root.io.is_open ? qsTr("Close") : qsTr("Open")
            enabled: !root.io.busy
            onClicked: {
                errorLabel.text = ""
                if (root.io.is_open)
                    root.io.close_device()
                else
                    root.report(root.io.open_device(deviceBox.currentValue))
            }
        }
        Button {
            text: qsTr("Identify")
            enabled: root.io.is_open
            ToolTip.visible: hovered
            ToolTip.text: qsTr("Make the device show itself, e.g. by blinking its LED")
            onClicked: root.report(root.io.identify())
        }
        Label {
            text: root.io.device_details
            color: Theme.muted
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Label { text: qsTr("Poll every") }
        SpinBox {
            from: 20
            to: 1000
            stepSize: 10
            editable: true
            value: root.io.poll_interval
            onValueModified: root.io.set_poll_interval(value)
            Layout.preferredWidth: 120
        }
        Label { text: qsTr("ms") }
        BusyIndicator {
            running: root.io.busy
            visible: running
            Layout.preferredHeight: 28
            Layout.preferredWidth: 28
        }
    }

    Label {
        visible: !root.io.available
        text: qsTr("Only the simulated device is available: USB I/O adapters need the pyusb package (the \"io\" extra).")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    Label {
        id: errorLabel
        color: Theme.danger
        visible: text.length > 0
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.leftMargin: 4
    }

    Label {
        visible: !root.io.is_open
        text: qsTr("Open an I/O device to switch its outputs and watch its inputs and analogue values.")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        Layout.margins: 4
    }

    SplitView {
        visible: root.io.is_open
        Layout.fillWidth: true
        Layout.fillHeight: true

        // Digital lines
        ColumnLayout {
            SplitView.preferredWidth: 560
            SplitView.minimumWidth: 420
            spacing: 2

            RowLayout {
                Layout.fillWidth: true
                Label { text: qsTr("Digital lines"); color: Theme.muted; padding: 4 }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("All outputs low")
                    flat: true
                    onClicked: root.report(root.io.set_all_outputs(false))
                }
                Button {
                    text: qsTr("All outputs high")
                    flat: true
                    onClicked: root.report(root.io.set_all_outputs(true))
                }
            }
            ListView {
                id: lineList
                clip: true
                spacing: 2
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.io.lines
                delegate: Rectangle {
                    id: lineItem
                    required property var modelData
                    width: lineList.width - 10
                    height: 44
                    radius: Theme.smallRadius
                    color: Theme.alpha(Theme.card, 0.6)

                    function configure(output, pull, activeLow) {
                        root.report(root.io.configure_line(modelData.line, output, pull, activeLow))
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10
                        spacing: 10

                        Label {
                            text: qsTr("Line %1").arg(lineItem.modelData.line)
                            font.bold: true
                            Layout.preferredWidth: 56
                        }
                        // Level indicator
                        Rectangle {
                            width: 14
                            height: 14
                            radius: 7
                            color: lineItem.modelData.level ? Theme.success : "transparent"
                            border.color: lineItem.modelData.level ? Theme.success : Theme.muted
                            border.width: 2
                            ToolTip.visible: levelArea.containsMouse
                            ToolTip.text: lineItem.modelData.level ? qsTr("High") : qsTr("Low")
                            MouseArea {
                                id: levelArea
                                anchors.fill: parent
                                hoverEnabled: true
                            }
                        }
                        ComboBox {
                            model: [qsTr("Input"), qsTr("Output")]
                            currentIndex: lineItem.modelData.output ? 1 : 0
                            enabled: lineItem.modelData.can_input && lineItem.modelData.can_output
                            Layout.preferredWidth: 110
                            onActivated: index => lineItem.configure(index === 1, lineItem.modelData.pull,
                                                                      lineItem.modelData.active_low)
                        }
                        ComboBox {
                            visible: !lineItem.modelData.output
                            model: root.io.pulls
                            displayText: currentText === "none" ? qsTr("No pull")
                                       : currentText === "up" ? qsTr("Pull-up") : qsTr("Pull-down")
                            currentIndex: root.io.pulls.indexOf(lineItem.modelData.pull)
                            Layout.preferredWidth: 120
                            onActivated: index => lineItem.configure(false, root.io.pulls[index],
                                                                      lineItem.modelData.active_low)
                        }
                        CheckBox {
                            visible: !lineItem.modelData.output
                            text: qsTr("Active low")
                            checked: lineItem.modelData.active_low
                            onToggled: lineItem.configure(false, lineItem.modelData.pull, checked)
                        }
                        Item { Layout.fillWidth: true }
                        Switch {
                            visible: lineItem.modelData.output
                            checked: lineItem.modelData.requested
                            text: checked ? qsTr("High") : qsTr("Low")
                            onToggled: root.report(root.io.set_output(lineItem.modelData.line, checked))
                        }
                    }
                }
            }
        }

        // Analogue inputs and log
        ColumnLayout {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 300
            spacing: 4

            RowLayout {
                Layout.fillWidth: true
                Label {
                    text: root.io.analog.length ? qsTr("Analogue inputs") : qsTr("No analogue inputs")
                    color: Theme.muted
                    padding: 4
                }
                Item { Layout.fillWidth: true }
                Label { text: qsTr("Reference") }
                TextField {
                    text: root.io.reference_voltage.toFixed(2)
                    validator: DoubleValidator { bottom: 0.1; top: 100 }
                    Layout.preferredWidth: 70
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("Voltage of the full ADC scale, to show the values in volts")
                    onEditingFinished: root.io.set_reference_voltage(Number(text.replace(",", ".")))
                }
                Label { text: "V" }
            }
            ListView {
                id: analogList
                clip: true
                spacing: 6
                Layout.fillWidth: true
                Layout.fillHeight: true
                ScrollBar.vertical: ScrollBar {}
                model: root.io.analog
                delegate: Rectangle {
                    id: analogItem
                    required property var modelData
                    width: analogList.width - 10
                    height: 74
                    radius: Theme.smallRadius
                    color: Theme.alpha(Theme.card, 0.6)

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 12
                        ColumnLayout {
                            spacing: 2
                            Layout.preferredWidth: 130
                            Label {
                                text: qsTr("AIN %1").arg(analogItem.modelData.channel)
                                color: Theme.muted
                            }
                            Label {
                                text: `${analogItem.modelData.volts.toFixed(3)} V`
                                font.pixelSize: 18
                                font.bold: true
                            }
                            Label {
                                text: qsTr("raw %1").arg(analogItem.modelData.raw)
                                color: Theme.muted
                                font.pixelSize: 11
                            }
                        }
                        Sparkline {
                            values: analogItem.modelData.history
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                        }
                    }
                }
            }
            RowLayout {
                visible: logList.count > 0
                Layout.fillWidth: true
                Label { text: qsTr("Log"); color: Theme.muted }
                Item { Layout.fillWidth: true }
                Button {
                    text: qsTr("Clear")
                    flat: true
                    onClicked: root.io.clear_log()
                }
            }
            ListView {
                id: logList
                visible: count > 0
                clip: true
                Layout.fillWidth: true
                Layout.preferredHeight: 100
                ScrollBar.vertical: ScrollBar {}
                model: root.io.log
                onCountChanged: positionViewAtEnd()
                delegate: Label {
                    required property var modelData
                    width: logList.width - 10
                    text: modelData.text
                    font.family: "monospace"
                    font.pixelSize: 12
                    color: modelData.error ? Theme.danger : Theme.text
                    elide: Text.ElideRight
                }
            }
        }
    }

    // The log also shows errors while closed (e.g. a device that could not be opened)
    ListView {
        visible: !root.io.is_open && count > 0
        clip: true
        Layout.fillWidth: true
        Layout.fillHeight: true
        model: root.io.log
        delegate: Label {
            required property var modelData
            text: modelData.text
            font.family: "monospace"
            font.pixelSize: 12
            color: modelData.error ? Theme.danger : Theme.text
        }
    }
}
