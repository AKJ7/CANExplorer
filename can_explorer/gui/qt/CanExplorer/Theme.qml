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
        },
        "Dracula": {
            background: "#282a36", surface: "#2d2f3d", card: "#343746", cardAlt: "#2b2d3a", border: "#44475a",
            text: "#f8f8f2", muted: "#6272a4", accent: "#bd93f9", success: "#50fa7b", warning: "#ffb86c",
            danger: "#ff5555", chart: ["#bd93f9", "#50fa7b", "#ffb86c", "#ff5555", "#ff79c6", "#8be9fd"]
        },
        "Solarized Dark": {
            background: "#002b36", surface: "#04313c", card: "#073642", cardAlt: "#053540", border: "#0f4a56",
            text: "#93a1a1", muted: "#586e75", accent: "#268bd2", success: "#859900", warning: "#b58900",
            danger: "#dc322f", chart: ["#268bd2", "#859900", "#b58900", "#dc322f", "#6c71c4", "#2aa198"]
        },
        // Colour-blind-safe and high contrast: the Okabe-Ito palette (distinguishable across common colour-vision
        // deficiencies) on a black background with white text and strong borders.
        "High Contrast": {
            background: "#000000", surface: "#0a0a0a", card: "#141414", cardAlt: "#1c1c1c", border: "#4d4d4d",
            text: "#ffffff", muted: "#c8c8c8", accent: "#56b4e9", success: "#009e73", warning: "#e69f00",
            danger: "#d55e00", chart: ["#56b4e9", "#009e73", "#e69f00", "#d55e00", "#cc79a7", "#f0e442"]
        },
        // High-contrast cyberpunk: neon accents on a near-black violet background.
        "Cyberpunk": {
            background: "#06010f", surface: "#0f0a1f", card: "#150f2b", cardAlt: "#0e0920", border: "#3a2a6a",
            text: "#eef2ff", muted: "#9d93c7", accent: "#00e5ff", success: "#00ff9f", warning: "#fde047",
            danger: "#ff2e88", chart: ["#00e5ff", "#00ff9f", "#fde047", "#ff2e88", "#b14aff", "#ff6ec7"]
        },
        // A pink-led theme: hot pink on a deep rosy background, with a clear red kept for danger.
        "Rosé": {
            background: "#1b0f16", surface: "#231521", card: "#2c1a28", cardAlt: "#21131e", border: "#4a2a40",
            text: "#fce7f1", muted: "#c58aa8", accent: "#ff4d94", success: "#34d399", warning: "#fbbf24",
            danger: "#ef4444", chart: ["#ff4d94", "#c084fc", "#38bdf8", "#fbbf24", "#34d399", "#fb7185"]
        },
        // Hidden "red diamond spirit" theme, unlocked from the About dialog (the seven havens easter egg):
        // a glowing crimson on near-black. Kept out of the normal theme list until it is awakened.
        "Spirit": {
            background: "#1a0d0f", surface: "#241315", card: "#2e171a", cardAlt: "#200f12", border: "#4a2328",
            text: "#f6e7e7", muted: "#b88a8e", accent: "#e23b4e", success: "#2ecc71", warning: "#f39c12",
            danger: "#ff6b6b", chart: ["#e23b4e", "#ff7a5c", "#ffb347", "#c0392b", "#9b59b6", "#f1c40f"]
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
