import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import CanExplorer.Plots 1.0


SplitView {
    id: root
    required property var connection
    readonly property var colors: ["#4e9af1", "#f1a14e", "#5cc46a", "#e05d5d", "#a56ee0", "#4ec9c9"]

    ColumnLayout {
        SplitView.preferredWidth: 280
        SplitView.minimumWidth: 180

        Label {
            text: root.connection.database_file
                ? qsTr("Signals — check to plot")
                : qsTr("Load a signal database to plot signals")
            padding: 4
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
        TextField {
            id: signalFilter
            placeholderText: qsTr("Search signals")
            enabled: root.connection.database_file !== ""
            Layout.fillWidth: true
        }
        ListView {
            id: signalList
            clip: true
            Layout.fillWidth: true
            Layout.fillHeight: true
            ScrollBar.vertical: ScrollBar {}
            model: root.connection.database_signals.filter(
                info => info.key.toLowerCase().includes(signalFilter.text.toLowerCase()))
            delegate: CheckDelegate {
                required property var modelData
                width: signalList.width
                text: modelData.key + (modelData.unit ? ` [${modelData.unit}]` : "")
                checked: root.connection.tracked_signals.includes(modelData.key)
                onToggled: checked
                    ? root.connection.track_signal(modelData.message, modelData.signal, modelData.unit)
                    : root.connection.untrack_signal(modelData.message, modelData.signal)
            }
        }
    }

    ColumnLayout {
        SplitView.fillWidth: true

        RowLayout {
            Layout.leftMargin: 4
            Label { text: qsTr("Time window") }
            ComboBox {
                id: windowBox
                model: [5, 10, 30, 60, 300]
                currentIndex: 1
                displayText: qsTr("%1 s").arg(currentValue)
            }
            Item { Layout.fillWidth: true }
        }

        ScrollView {
            id: plotScroll
            clip: true
            Layout.fillWidth: true
            Layout.fillHeight: true

            Column {
                width: plotScroll.availableWidth
                spacing: 6

                Label {
                    visible: root.connection.tracked_signals.length === 0
                    text: qsTr("No signals selected")
                    padding: 8
                }

                Repeater {
                    model: root.connection.tracked_signals
                    SignalPlot {
                        required property string modelData
                        required property int index
                        width: parent.width
                        height: 150
                        connection: root.connection
                        signal_key: modelData
                        window: windowBox.currentValue
                        line_color: root.colors[index % root.colors.length]
                        text_color: palette.text
                        grid_color: palette.mid
                    }
                }
            }
        }
    }
}
