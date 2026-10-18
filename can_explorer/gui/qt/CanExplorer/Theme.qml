pragma Singleton

import QtQuick 2.15

// Colours, sizes and spacing of the user interface. Every component reads from here, so changing `name`
// re-themes the whole application through property bindings. The names must match app_state.THEMES.
QtObject {
    id: theme

    property string name: "Dark"

    readonly property var palettes: ({
        "Dark": {
            background: "#171827", surface: "#1d1e31", card: "#24263d", cardAlt: "#1b1c2e", border: "#2e3150",
            text: "#e6e8f2", muted: "#8b8fa8", accent: "#3498db", success: "#2ecc71", warning: "#f39c12",
            danger: "#e74c3c", chart: ["#3498db", "#2ecc71", "#f39c12", "#e74c3c", "#9b59b6", "#1abc9c"]
        },
        "Purple": {
            background: "#1c1428", surface: "#221932", card: "#2b1f3f", cardAlt: "#21182f", border: "#3b2b54",
            text: "#f0e8f8", muted: "#a796ba", accent: "#9b59b6", success: "#00bcd4", warning: "#ff9800",
            danger: "#e91e63", chart: ["#9b59b6", "#00bcd4", "#ff9800", "#e91e63", "#7e57c2", "#26c6da"]
        },
        "Nord": {
            background: "#2b303b", surface: "#323846", card: "#3b4252", cardAlt: "#343a48", border: "#4c566a",
            text: "#eceff4", muted: "#a3acbf", accent: "#88c0d0", success: "#a3be8c", warning: "#ebcb8b",
            danger: "#bf616a", chart: ["#88c0d0", "#a3be8c", "#ebcb8b", "#bf616a", "#b48ead", "#81a1c1"]
        },
        "Ocean": {
            background: "#0d1a2b", surface: "#112238", card: "#162e48", cardAlt: "#102236", border: "#21405f",
            text: "#e3f2fd", muted: "#8fb3cf", accent: "#29b6f6", success: "#26a69a", warning: "#ffca28",
            danger: "#ef5350", chart: ["#29b6f6", "#26a69a", "#ffca28", "#ef5350", "#7e57c2", "#66bb6a"]
        },
        "Forest": {
            background: "#121c16", surface: "#17241c", card: "#1d3025", cardAlt: "#16241b", border: "#294332",
            text: "#e8f5e9", muted: "#9bbfa4", accent: "#4caf50", success: "#8bc34a", warning: "#ffb300",
            danger: "#e57373", chart: ["#4caf50", "#8bc34a", "#ffb300", "#e57373", "#26a69a", "#9ccc65"]
        },
        "Light": {
            background: "#eef1f7", surface: "#ffffff", card: "#ffffff", cardAlt: "#f3f5fa", border: "#dde2ee",
            text: "#1f2437", muted: "#69708c", accent: "#3b82f6", success: "#16a34a", warning: "#d97706",
            danger: "#dc2626", chart: ["#3b82f6", "#16a34a", "#d97706", "#dc2626", "#9333ea", "#0891b2"]
        }
    })
    readonly property var current: palettes[name] || palettes["Dark"]
    readonly property bool dark: name !== "Light"

    readonly property color background: current.background   // Window
    readonly property color surface: current.surface         // Sidebar and top bar
    readonly property color card: current.card               // Cards
    readonly property color cardAlt: current.cardAlt         // Table headers, inputs, alternating rows
    readonly property color border: current.border
    readonly property color text: current.text
    readonly property color muted: current.muted
    readonly property color accent: current.accent
    readonly property color success: current.success
    readonly property color warning: current.warning
    readonly property color danger: current.danger
    readonly property var chart: current.chart

    readonly property int radius: 14
    readonly property int smallRadius: 8
    readonly property int spacing: 16
    readonly property int sidebarWidth: 232
    readonly property int titleSize: 17
    readonly property int valueSize: 26

    function alpha(color, opacity) {
        return Qt.rgba(color.r, color.g, color.b, opacity)
    }
}
