import QtQuick 2.15
import QtQuick.Controls 2.15
import CanExplorer 1.0

// Chooses how the trace tables show frame times; the choice is a preference shared by all traces.
ComboBox {
    id: box

    readonly property var appState: connectionManager.app_state
    // Width of a trace's time column for the chosen format
    readonly property int columnWidth: appState.trace_time_format === "unix" ? 140 : 110

    textRole: "text"
    valueRole: "value"
    model: [
        { value: "relative", text: qsTr("Relative time") },
        { value: "delta", text: qsTr("Delta time") },
        { value: "absolute", text: qsTr("Absolute time") },
        { value: "unix", text: qsTr("Unix time") }
    ]
    currentIndex: Math.max(0, indexOfValue(appState.trace_time_format))
    onActivated: appState.set_trace_time_format(currentValue)

    ToolTip.visible: hovered
    ToolTip.text: qsTr("Time column: seconds since the start (relative), since the previous row (delta), "
        + "the time of day (absolute) or seconds since 1970 (Unix)")
}
