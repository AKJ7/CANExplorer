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
        // Bumblebee: a bright honey-yellow light theme with black-and-amber accents and dark text.
        "Bumblebee": {
            background: "#ffe873", surface: "#f6cf3f", card: "#fff6cc", cardAlt: "#ffee9e", border: "#b8860b",
            text: "#241a00", muted: "#7c6514", accent: "#111111", success: "#2e7d32", warning: "#e65100",
            danger: "#b71c1c", chart: ["#111111", "#f59e0b", "#c2410c", "#b8860b", "#2e7d32", "#b71c1c"]
        },
        // Pink Gold: a soft rose-pink light theme with gold accents and deep-wine text.
        "Pink Gold": {
            background: "#ffdce8", surface: "#ffc9dc", card: "#fff2f6", cardAlt: "#ffe6ee", border: "#c99a2e",
            text: "#3a0f24", muted: "#9c5a74", accent: "#b8860b", success: "#2e7d32", warning: "#cc7a00",
            danger: "#c2185b", chart: ["#d4af37", "#e75480", "#b76e79", "#8e44ad", "#2e7d32", "#c2185b"]
        },
        // Amethyst: a vivid deep-violet background with a full-spectrum chart palette and a magenta accent.
        "Amethyst": {
            background: "#2a1a5e", surface: "#34207a", card: "#3d2a8f", cardAlt: "#2f2270", border: "#6a4bc7",
            text: "#f6f0ff", muted: "#bbaaf0", accent: "#ff5ec7", success: "#3bf0a3", warning: "#ffd23f",
            danger: "#ff6b6b", chart: ["#ff5d5d", "#ff9f40", "#ffd23f", "#4fd675", "#4fa3ff", "#b36bff"]
        },
        // --- Themes derived from the "36 beautiful colour palettes" collection (Color Hex). The chart arrays are
        // deliberately broadened to six distinguishable hues even where the source palette is near-monochrome, so
        // overlaid signals stay legible; the base/accent colours stay true to each palette. ---
        // Beautiful Blues: a deep navy workspace that fades up to a soft sky-blue accent.
        "Beautiful Blues": {
            background: "#011f4b", surface: "#03305f", card: "#073b70", cardAlt: "#042c57", border: "#005b96",
            text: "#eaf2fb", muted: "#9db8d4", accent: "#6aa9d6", success: "#4fcf8f", warning: "#e8c15a",
            danger: "#e86a6a", chart: ["#6aa9d6", "#b3cde0", "#5fd3c0", "#f3c969", "#e68a8a", "#a99be0"]
        },
        // Blueberry: deep blueberry-navy panels (the palette's #283655/#1e1f26) under a lifted blue accent.
        "Blueberry": {
            background: "#1b2440", surface: "#223052", card: "#2a3b63", cardAlt: "#1f2a4a", border: "#3d4f7a",
            text: "#eef2fb", muted: "#a4b2cf", accent: "#7aa0d8", success: "#4fcf8f", warning: "#e8c15a",
            danger: "#e86a6a", chart: ["#7aa0d8", "#d0e1f9", "#5fd3c0", "#e8c15a", "#e07aa0", "#9b8be0"]
        },
        // Midnight Plum: the palette's navy-to-wine plums as the panels (#251e3e…#451e3e) with a berry accent.
        "Midnight Plum": {
            background: "#231836", surface: "#2e1d40", card: "#3a2550", cardAlt: "#281a3c", border: "#562f58",
            text: "#ece6f2", muted: "#a596b0", accent: "#c74b7d", success: "#4bd6a0", warning: "#e8b84f",
            danger: "#ff6b6b", chart: ["#c74b7d", "#6aa0e0", "#9b6bd6", "#e8b84f", "#4bd6a0", "#ff8a6b"]
        },
        // Purple Skyline: deep indigo panels rising to a lavender skyline accent.
        "Purple Skyline": {
            background: "#200830", surface: "#2c1140", card: "#3a1e5a", cardAlt: "#261038", border: "#4d2f72",
            text: "#efe9f7", muted: "#b2a3c8", accent: "#b388ff", success: "#5fd6a0", warning: "#e8c15a",
            danger: "#ff6b8a", chart: ["#b388ff", "#8874a3", "#e4dcf1", "#5fd6a0", "#ffb36b", "#6aa9e0"]
        },
        // Moonlight: deep teal-slate panels (the palette is teal-led) with gold and coral accents.
        "Moonlight": {
            background: "#16282a", surface: "#1d3234", card: "#243e40", cardAlt: "#1a2e30", border: "#30504f",
            text: "#eef2f1", muted: "#9aa6a4", accent: "#0e9aa7", success: "#3da4ab", warning: "#f6cd61",
            danger: "#fe8a71", chart: ["#0e9aa7", "#f6cd61", "#fe8a71", "#3da4ab", "#8fd0d6", "#b6a3e0"]
        },
        // Lollipop: a fresh teal/mint workspace with a seafoam accent.
        "Lollipop": {
            background: "#0c1f1d", surface: "#123430", card: "#184a44", cardAlt: "#0f2825", border: "#1f5f57",
            text: "#e4f5f2", muted: "#8fc2bb", accent: "#54b2a9", success: "#83d0c9", warning: "#e8c15a",
            danger: "#ff7a6b", chart: ["#54b2a9", "#83d0c9", "#f6cd61", "#fe8a71", "#6aa9e0", "#b388d9"]
        },
        // Space Gray: a clean neutral slate theme with a cool blue accent.
        "Space Gray": {
            background: "#2a3139", surface: "#343d46", card: "#3e4852", cardAlt: "#2f3740", border: "#4f5b66",
            text: "#eceef2", muted: "#a7adba", accent: "#6fb3d6", success: "#7cc47c", warning: "#e3b457",
            danger: "#e07a6a", chart: ["#6fb3d6", "#c0c5ce", "#7cc47c", "#e3b457", "#cf8fd0", "#65b0a0"]
        },
        // Cappuccino: a cosy espresso theme with a toasted-tan accent and cream text.
        "Cappuccino": {
            background: "#2a2120", surface: "#342a28", card: "#3f3331", cardAlt: "#2d2423", border: "#4b3832",
            text: "#f4ead9", muted: "#bda58f", accent: "#be9b7b", success: "#8bbb6a", warning: "#e0a85a",
            danger: "#c56a56", chart: ["#be9b7b", "#c56a56", "#e0a85a", "#8bbb6a", "#6aa0c0", "#a98bbf"]
        },
        // Ember: warm gold, orange and brick accents over a roasted-brown base (the "program catalog" palette).
        "Ember": {
            background: "#1c1613", surface: "#241c18", card: "#2e241f", cardAlt: "#201915", border: "#4f372d",
            text: "#f4ebe2", muted: "#bda594", accent: "#00a0b0", success: "#7fb04a", warning: "#edc951",
            danger: "#cc2a36", chart: ["#edc951", "#eb6841", "#00a0b0", "#cc2a36", "#7fb04a", "#b08bd0"]
        },
        // Metro: vivid tile colours (red, green, cyan, orange, yellow) on a deep cyan-blue drawn from the palette.
        "Metro": {
            background: "#0b1b2e", surface: "#112740", card: "#17304d", cardAlt: "#0f2238", border: "#244a73",
            text: "#f2f3f6", muted: "#9aa6b8", accent: "#00aedb", success: "#00b159", warning: "#ffc425",
            danger: "#d11141", chart: ["#00aedb", "#d11141", "#00b159", "#f37735", "#ffc425", "#9b59b6"]
        },
        // Prism: a bright five-colour palette (red, orange, yellow, green, blue) on a deep blue drawn from its blue.
        "Prism": {
            background: "#06243a", surface: "#0b2f49", card: "#103a58", cardAlt: "#082a42", border: "#1a5075",
            text: "#f2f4f8", muted: "#9ab2c4", accent: "#0392cf", success: "#7bc043", warning: "#f6c945",
            danger: "#ee4035", chart: ["#ee4035", "#f37736", "#f6c945", "#7bc043", "#0392cf", "#9b59b6"]
        },
        // Rainbow: the seven canonical rainbow colours (ROYGBIV), in order, as the chart palette, on a deep violet
        // base (the end of the spectrum) so each band stays saturated. The chart has seven entries, one per colour.
        "Rainbow": {
            background: "#1a0b2e", surface: "#241041", card: "#2e1552", cardAlt: "#200c38", border: "#45277a",
            text: "#f4f2fa", muted: "#a99dc8", accent: "#1e90ff", success: "#3cb043", warning: "#ffd500",
            danger: "#e81416",
            chart: ["#e81416", "#ff8c00", "#ffd500", "#3cb043", "#1e90ff", "#4b0082", "#9b30ff"]
        },
        // Beach Towels: a sunlit pale-teal light theme (from the palette's teal) with a coral/gold chart.
        "Beach Towels": {
            background: "#dff1f4", surface: "#ecf8fa", card: "#f5fcfd", cardAlt: "#cfe9ed", border: "#b4dbe0",
            text: "#1f333a", muted: "#577981", accent: "#1792a3", success: "#2e9e5b", warning: "#c98a00",
            danger: "#e8403f", chart: ["#1792a3", "#e8403f", "#c98a00", "#4a8fc0", "#8a5cc0", "#e0692c"]
        },
        // Beach: a breezy pale-mint light theme (from the palette's mint) with coral and sunshine-yellow accents.
        "Beach": {
            background: "#ddf1e7", surface: "#ecf9f2", card: "#f4fcf8", cardAlt: "#cdebdd", border: "#b6e0cd",
            text: "#20332b", muted: "#5d7d6f", accent: "#279c7b", success: "#2e9e6a", warning: "#cf8e00",
            danger: "#f0574f", chart: ["#279c7b", "#f0574f", "#cf8e00", "#4a8fc0", "#8a5cc0", "#e0692c"]
        },
        // Pastel Rainbow: a soft pastel-pink light theme (from the palette's coral-pink) with gentle accents.
        "Pastel Rainbow": {
            background: "#ffe6e1", surface: "#fff2ef", card: "#fff8f6", cardAlt: "#ffd7ce", border: "#f3bfb6",
            text: "#3a3338", muted: "#8a7279", accent: "#e85f6c", success: "#3fa87d", warning: "#d98f3a",
            danger: "#e04651", chart: ["#e85f6c", "#3fa87d", "#e09a3a", "#5a93d0", "#a86fd0", "#39b0a0"]
        },
        // Aquarius: a soft aqua light theme (the "water bearer" palette's aqua) with a slate-blue secondary.
        "Aquarius": {
            background: "#d2ebee", surface: "#e6f5f7", card: "#f0fafb", cardAlt: "#c0e4e9", border: "#a2d2d9",
            text: "#223038", muted: "#587079", accent: "#2f8a96", success: "#2e9e6a", warning: "#cf9100",
            danger: "#d9534f", chart: ["#2f8a96", "#58668b", "#cf9100", "#c75d62", "#5a8f4f", "#8a6fb0"]
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
    // A theme is "dark" when its background is dark (by relative luminance), so light-background themes such as
    // Bumblebee and Pink Gold get the light-mode adjustments (hover lightening, glass tints) automatically.
    readonly property bool dark: {
        const c = Qt.color(current.background)
        return (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) < 0.5
    }

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
