pragma Singleton

import QtQuick 2.15

// Colours, sizes and spacing of the user interface. Every component reads from here, so changing `name`
// re-themes the whole application through property bindings. The names must match app_state.THEMES (a unit test
// checks this), and every palette must meet the WCAG contrast minimums checked by tests/unit/gui/test_theme.py:
// text and muted 4.5:1, accent, status and chart colours 3:1 against each panel colour.
QtObject {
    id: theme

    // The chosen theme, or "System" to follow the light/dark colour scheme of the operating system
    property string name: "Dark"
    // The themes "System" shows in light and in dark mode (app_state.system_light_theme / system_dark_theme)
    property string systemLightName: "Light"
    property string systemDarkName: "Dark"
    // Shown temporarily while picked in the preferences (empty: none); not persisted
    property string preview: ""
    property string previewLight: ""
    property string previewDark: ""
    // The palette actually shown
    readonly property string effectiveName: resolve(preview || name)
    // Whether the operating system is in dark mode (unknown counts as dark, like the default theme)
    readonly property bool osDark: Qt.styleHints.colorScheme !== Qt.ColorScheme.Light

    function resolve(themeName) {
        if (themeName !== "System")
            return themeName
        return osDark ? (previewDark || systemDarkName) : (previewLight || systemLightName)
    }

    // Whether a theme has a dark background (WCAG relative luminance), e.g. to offer it for dark mode
    function isDark(themeName) {
        const palette = palettes[resolve(themeName)]
        return palette !== undefined && luminance(palette.background) < 0.18
    }

    readonly property var palettes: ({
        "Dark": {
            background: "#171827", surface: "#1d1e31", card: "#24263d", cardAlt: "#1b1c2e", border: "#2e3150",
            text: "#e6e8f2", muted: "#8b8fa8", accent: "#3498db", success: "#2ecc71", warning: "#f39c12",
            danger: "#e74c3c", chart: ["#3498db", "#2ecc71", "#f39c12", "#e74c3c", "#9b59b6", "#1abc9c"]
        },
        "Purple": {
            background: "#1c1428", surface: "#221932", card: "#2b1f3f", cardAlt: "#21182f", border: "#3b2b54",
            text: "#f0e8f8", muted: "#a796ba", accent: "#9b59b6", success: "#00bcd4", warning: "#ff9800",
            danger: "#e91e63", chart: ["#9b59b6", "#00bcd4", "#ff9800", "#e91e63", "#66bb6a", "#ffd54f"]
        },
        "Nord": {
            background: "#2b303b", surface: "#323846", card: "#3b4252", cardAlt: "#343a48", border: "#4c566a",
            text: "#eceff4", muted: "#a6afc1", accent: "#88c0d0", success: "#a3be8c", warning: "#ebcb8b",
            danger: "#c8777f", chart: ["#88c0d0", "#a3be8c", "#ebcb8b", "#c8777f", "#b48ead", "#81a1c1"]
        },
        "Ocean": {
            background: "#0d1a2b", surface: "#112238", card: "#162e48", cardAlt: "#102236", border: "#21405f",
            text: "#e3f2fd", muted: "#8fb3cf", accent: "#29b6f6", success: "#26a69a", warning: "#ffca28",
            danger: "#ef5350", chart: ["#29b6f6", "#26a69a", "#ffca28", "#ef5350", "#8864c7", "#66bb6a"]
        },
        "Forest": {
            background: "#121c16", surface: "#17241c", card: "#1d3025", cardAlt: "#16241b", border: "#294332",
            text: "#e8f5e9", muted: "#9bbfa4", accent: "#4caf50", success: "#8bc34a", warning: "#ffb300",
            danger: "#e57373", chart: ["#4caf50", "#8bc34a", "#ffb300", "#e57373", "#26a69a", "#64b5f6"]
        },
        "Light": {
            background: "#eef1f7", surface: "#ffffff", card: "#ffffff", cardAlt: "#f3f5fa", border: "#dde2ee",
            text: "#1f2437", muted: "#656b86", accent: "#3b82f6", success: "#159f48", warning: "#cf7206",
            danger: "#dc2626", chart: ["#3b82f6", "#159f48", "#cf7206", "#dc2626", "#9333ea", "#0891b2"]
        },
        "Dracula": {
            background: "#282a36", surface: "#2d2f3d", card: "#343746", cardAlt: "#2b2d3a", border: "#44475a",
            text: "#f8f8f2", muted: "#96a0c2", accent: "#bd93f9", success: "#50fa7b", warning: "#ffb86c",
            danger: "#ff5555", chart: ["#bd93f9", "#50fa7b", "#ffb86c", "#ff5555", "#ff79c6", "#8be9fd"]
        },
        "Solarized Dark": {
            background: "#002b36", surface: "#04313c", card: "#073642", cardAlt: "#053540", border: "#0f4a56",
            text: "#93a1a1", muted: "#879da5", accent: "#268bd2", success: "#859900", warning: "#b58900",
            danger: "#df413e", chart: ["#268bd2", "#859900", "#b58900", "#df413e", "#7075c5", "#2aa198"]
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
            text: "#241a00", muted: "#6d5812", accent: "#111111", success: "#2e7d32", warning: "#cc4800",
            danger: "#b71c1c", chart: ["#111111", "#b67507", "#c2410c", "#1d4ed8", "#2e7d32", "#b71c1c"]
        },
        // Pink Gold: a soft rose-pink light theme with gold accents and deep-wine text.
        "Pink Gold": {
            background: "#ffdce8", surface: "#ffc9dc", card: "#fff2f6", cardAlt: "#ffe6ee", border: "#c99a2e",
            text: "#3a0f24", muted: "#844c62", accent: "#9b7109", success: "#2e7d32", warning: "#ad6800",
            danger: "#c2185b", chart: ["#9b7e21", "#e54474", "#b66c77", "#8e44ad", "#2e7d32", "#1565c0"]
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
            text: "#ece6f2", muted: "#a596b0", accent: "#c84d7e", success: "#4bd6a0", warning: "#e8b84f",
            danger: "#ff6b6b", chart: ["#c84d7e", "#6aa0e0", "#9b6bd6", "#e8b84f", "#4bd6a0", "#ff8a6b"]
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
            danger: "#fe8a71", chart: ["#0e9aa7", "#f6cd61", "#fe8a71", "#8bc34a", "#8fd0d6", "#b6a3e0"]
        },
        // Lollipop: a fresh teal/mint workspace with a seafoam accent.
        "Lollipop": {
            background: "#0c1f1d", surface: "#123430", card: "#184a44", cardAlt: "#0f2825", border: "#1f5f57",
            text: "#e4f5f2", muted: "#8fc2bb", accent: "#54b2a9", success: "#83d0c9", warning: "#e8c15a",
            danger: "#ff7a6b", chart: ["#54b2a9", "#f48fb1", "#f6cd61", "#fe8a71", "#6aa9e0", "#b388d9"]
        },
        // Space Gray: a clean neutral slate theme with a cool blue accent.
        "Space Gray": {
            background: "#2a3139", surface: "#343d46", card: "#3e4852", cardAlt: "#2f3740", border: "#4f5b66",
            text: "#eceef2", muted: "#b1b6c2", accent: "#6fb3d6", success: "#7cc47c", warning: "#e3b457",
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
            danger: "#d42f3b", chart: ["#edc951", "#eb6841", "#00a0b0", "#d42f3b", "#7fb04a", "#b08bd0"]
        },
        // Metro: vivid tile colours (red, green, cyan, orange, yellow) on a deep cyan-blue drawn from the palette.
        "Metro": {
            background: "#0b1b2e", surface: "#112740", card: "#17304d", cardAlt: "#0f2238", border: "#244a73",
            text: "#f2f3f6", muted: "#9aa6b8", accent: "#00aedb", success: "#00b159", warning: "#ffc425",
            danger: "#ec174c", chart: ["#00aedb", "#ec174c", "#00b159", "#f37735", "#ffc425", "#9f60b9"]
        },
        // Prism: a bright five-colour palette (red, orange, yellow, green, blue) on a deep blue drawn from its blue.
        "Prism": {
            background: "#06243a", surface: "#0b2f49", card: "#103a58", cardAlt: "#082a42", border: "#1a5075",
            text: "#f2f4f8", muted: "#9ab2c4", accent: "#0392cf", success: "#7bc043", warning: "#f6c945",
            danger: "#ee4035", chart: ["#ee4035", "#f37736", "#f6c945", "#7bc043", "#0392cf", "#a66bbe"]
        },
        // Rainbow: the seven canonical rainbow colours (ROYGBIV), in order, as the chart palette, on a deep violet
        // base (the end of the spectrum) so each band stays saturated. The chart has seven entries, one per colour;
        // indigo is a lifted blue-violet, as true indigo (#4b0082) is invisible on the violet base.
        "Rainbow": {
            background: "#1a0b2e", surface: "#241041", card: "#2e1552", cardAlt: "#200c38", border: "#45277a",
            text: "#f4f2fa", muted: "#a99dc8", accent: "#1e90ff", success: "#3cb043", warning: "#ffd500",
            danger: "#e81416",
            chart: ["#e81416", "#ff8c00", "#ffd500", "#3cb043", "#1e90ff", "#6f5bff", "#9b30ff"]
        },
        // Beach Towels: a sunlit pale-teal light theme (from the palette's teal) with a coral/gold chart.
        "Beach Towels": {
            background: "#dff1f4", surface: "#ecf8fa", card: "#f5fcfd", cardAlt: "#cfe9ed", border: "#b4dbe0",
            text: "#1f333a", muted: "#4c6970", accent: "#168e9f", success: "#2b9254", warning: "#ad7700",
            danger: "#e8403f", chart: ["#168e9f", "#e8403f", "#ad7700", "#4086b8", "#8a5cc0", "#d35c1f"]
        },
        // Beach: a breezy pale-mint light theme (from the palette's mint) with coral and sunshine-yellow accents.
        "Beach": {
            background: "#ddf1e7", surface: "#ecf9f2", card: "#f4fcf8", cardAlt: "#cdebdd", border: "#b6e0cd",
            text: "#20332b", muted: "#506b5f", accent: "#249071", success: "#2b9262", warning: "#ab7600",
            danger: "#ee3e35", chart: ["#249071", "#ee3e35", "#ab7600", "#4086b8", "#8a5cc0", "#d35c1f"]
        },
        // Pastel Rainbow: a soft pastel-pink light theme (from the palette's coral-pink) with gentle accents.
        "Pastel Rainbow": {
            background: "#ffe6e1", surface: "#fff2ef", card: "#fff8f6", cardAlt: "#ffd7ce", border: "#f3bfb6",
            text: "#3a3338", muted: "#715d63", accent: "#e44251", success: "#358c68", warning: "#b16f22",
            danger: "#e04651", chart: ["#e44251", "#358c68", "#ad701b", "#3e81c8", "#a163cc", "#c2408f"]
        },
        // Aquarius: a soft aqua light theme (the "water bearer" palette's aqua) with a slate-blue secondary.
        "Aquarius": {
            background: "#d2ebee", surface: "#e6f5f7", card: "#f0fafb", cardAlt: "#c0e4e9", border: "#a2d2d9",
            text: "#223038", muted: "#4e646c", accent: "#2e8894", success: "#298c5e", warning: "#a47300",
            danger: "#d84d49", chart: ["#2e8894", "#58668b", "#a47300", "#c65b60", "#56884b", "#8a6fb0"]
        },
        // --- Light partners of dark themes, popular editor palettes, and themes for the bench and the field. Colours
        // come from each palette's published values; those short of the contrast minimums are lightened or darkened
        // (same hue), most often the accents of the light variants. ---
        // Solarized Light: the light half of Solarized (base3/base2 panels, base02 text).
        "Solarized Light": {
            background: "#eee8d5", surface: "#fdf6e3", card: "#fdf6e3", cardAlt: "#f5efdc", border: "#d9d2bd",
            text: "#073642", muted: "#566b72", accent: "#2588ce", success: "#7a8c00", warning: "#a67d00",
            danger: "#dc322f", chart: ["#2588ce", "#7a8c00", "#a67d00", "#dc322f", "#6c71c4", "#269189"]
        },
        // Nord Light: Nord's "Snow Storm" panels with Polar Night text and the Frost and Aurora colours.
        "Nord Light": {
            background: "#e5e9f0", surface: "#eceff4", card: "#f8f9fb", cardAlt: "#dfe4ec", border: "#c9d0dc",
            text: "#2e3440", muted: "#4c566a", accent: "#5e81ac", success: "#698a4e", warning: "#c3674a",
            danger: "#bf616a", chart: ["#5e81ac", "#698a4e", "#c3674a", "#bf616a", "#a17198", "#3f899f"]
        },
        // Catppuccin Latte: the light Catppuccin flavour, with its mauve accent.
        "Catppuccin Latte": {
            background: "#e6e9ef", surface: "#eff1f5", card: "#eff1f5", cardAlt: "#dce0e8", border: "#ccd0da",
            text: "#4c4f69", muted: "#5e6174", accent: "#8839ef", success: "#3a9027", warning: "#b07017",
            danger: "#d20f39", chart: ["#1e66f5", "#3a9027", "#b07017", "#d20f39", "#8839ef", "#168c92"]
        },
        // Catppuccin Mocha: the darkest Catppuccin flavour, with its mauve accent.
        "Catppuccin Mocha": {
            background: "#1e1e2e", surface: "#181825", card: "#313244", cardAlt: "#282839", border: "#45475a",
            text: "#cdd6f4", muted: "#a6adc8", accent: "#cba6f7", success: "#a6e3a1", warning: "#f9e2af",
            danger: "#f38ba8", chart: ["#89b4fa", "#a6e3a1", "#f9e2af", "#f38ba8", "#cba6f7", "#94e2d5"]
        },
        // Gruvbox Dark: warm, low-glare retro browns with the bright Gruvbox colours and an orange accent.
        "Gruvbox Dark": {
            background: "#282828", surface: "#1d2021", card: "#3c3836", cardAlt: "#32302f", border: "#504945",
            text: "#ebdbb2", muted: "#afa28e", accent: "#fe8019", success: "#b8bb26", warning: "#fabd2f",
            danger: "#fb4934", chart: ["#83a598", "#b8bb26", "#fabd2f", "#fb4934", "#d3869b", "#8ec07c"]
        },
        // Gruvbox Light: the cream Gruvbox panels with its faded (darker) colours.
        "Gruvbox Light": {
            background: "#f2e5bc", surface: "#fbf1c7", card: "#f9f5d7", cardAlt: "#ebdbb2", border: "#d5c4a1",
            text: "#3c3836", muted: "#685d54", accent: "#af3a03", success: "#79740e", warning: "#aa6f13",
            danger: "#9d0006", chart: ["#076678", "#79740e", "#aa6f13", "#9d0006", "#8f3f71", "#427b58"]
        },
        // Tokyo Night: deep blue-black panels (Night and Storm backgrounds) with the Tokyo Night neon colours.
        "Tokyo Night": {
            background: "#1a1b26", surface: "#16161e", card: "#24283b", cardAlt: "#1f2335", border: "#3b4261",
            text: "#c0caf5", muted: "#a9b1d6", accent: "#7aa2f7", success: "#9ece6a", warning: "#e0af68",
            danger: "#f7768e", chart: ["#7aa2f7", "#9ece6a", "#e0af68", "#f7768e", "#bb9af7", "#7dcfff"]
        },
        // One Dark: the Atom/VS Code One Dark greys with its blue accent.
        "One Dark": {
            background: "#282c34", surface: "#21252b", card: "#2c313c", cardAlt: "#252931", border: "#3e4451",
            text: "#abb2bf", muted: "#939aa5", accent: "#61afef", success: "#98c379", warning: "#e5c07b",
            danger: "#e06c75", chart: ["#61afef", "#98c379", "#e5c07b", "#e06c75", "#c678dd", "#56b6c2"]
        },
        // Colour-blind-safe at normal contrast: the Okabe-Ito colours (distinguishable across common colour-vision
        // deficiencies) on a soft dark and a light background; High Contrast and High Contrast Light are the
        // high-contrast versions.
        "Colour-Safe Dark": {
            background: "#1b1d23", surface: "#20232a", card: "#282c34", cardAlt: "#22252c", border: "#3a3f4b",
            text: "#e8eaf0", muted: "#a9afbc", accent: "#56b4e9", success: "#009e73", warning: "#e69f00",
            danger: "#d55e00", chart: ["#56b4e9", "#009e73", "#e69f00", "#d55e00", "#cc79a7", "#f0e442"]
        },
        "Colour-Safe Light": {
            background: "#f1f3f6", surface: "#ffffff", card: "#ffffff", cardAlt: "#f4f6f9", border: "#d5dae3",
            text: "#1d2129", muted: "#5a6170", accent: "#0072b2", success: "#009e73", warning: "#b87f00",
            danger: "#d55e00", chart: ["#0072b2", "#009e73", "#b87f00", "#d55e00", "#c76c9f", "#978e0c"]
        },
        // High Contrast Light: black on white with strong borders, for bright workshops and laptops in sunlight. The
        // Okabe-Ito colours are darkened to 4.5:1 or more on white.
        "High Contrast Light": {
            background: "#ffffff", surface: "#f2f2f2", card: "#ffffff", cardAlt: "#ebebeb", border: "#595959",
            text: "#000000", muted: "#333333", accent: "#005a8d", success: "#00704f", warning: "#8f5300",
            danger: "#b34700", chart: ["#005a8d", "#00704f", "#8f5300", "#b34700", "#a1457a", "#000000"]
        },
        // Night Vision: dim reds on black, for working in a vehicle or garage at night without losing the eyes' dark
        // adaptation (which blue and green light destroys). Deliberately monochrome: the status colours differ in
        // brightness rather than hue (danger brightest), and chart series are harder to tell apart than in the other
        // themes.
        "Night Vision": {
            background: "#000000", surface: "#070000", card: "#100000", cardAlt: "#0a0000", border: "#4a0d08",
            text: "#ff4d40", muted: "#e8392c", accent: "#ff3b30", success: "#b33c2a", warning: "#ff7b00",
            danger: "#ff1a1a", chart: ["#ff3030", "#ff8a00", "#c2185b", "#ff8f7a", "#d4502a", "#ffb08a"]
        },
        // Phosphor: green-phosphor oscilloscope screen; amber and red are kept for warnings and errors so states stay
        // distinguishable.
        "Phosphor": {
            background: "#020a04", surface: "#03100a", card: "#071a0d", cardAlt: "#051409", border: "#0f4020",
            text: "#33ff66", muted: "#22c14f", accent: "#39ff7a", success: "#33ff66", warning: "#ffb000",
            danger: "#ff5544", chart: ["#33ff66", "#ffb000", "#00e5c0", "#ff5544", "#b8ff4d", "#7dffb0"]
        },
        // Hidden "red diamond spirit" theme, unlocked from the About dialog (the seven havens easter egg):
        // a glowing crimson on near-black. Kept out of the normal theme list until it is awakened.
        "Spirit": {
            background: "#1a0d0f", surface: "#241315", card: "#2e171a", cardAlt: "#200f12", border: "#4a2328",
            text: "#f6e7e7", muted: "#b88a8e", accent: "#e23b4e", success: "#2ecc71", warning: "#f39c12",
            danger: "#ff6b6b", chart: ["#e23b4e", "#ff7a5c", "#ffb347", "#c0392b", "#9b59b6", "#f1c40f"]
        }
    })
    readonly property var current: palettes[effectiveName] || palettes["Dark"]
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
    // Text and icons drawn on a filled accent or danger background: white or near-black, whichever contrasts more
    readonly property color onAccent: onColor(accent)
    readonly property color onDanger: onColor(danger)

    readonly property int radius: 14
    readonly property int smallRadius: 8
    readonly property int spacing: 16
    readonly property int sidebarWidth: 232
    readonly property int titleSize: 17
    readonly property int valueSize: 26

    function alpha(color, opacity) {
        return Qt.rgba(color.r, color.g, color.b, opacity)
    }

    // WCAG relative luminance of a colour
    function luminance(color) {
        const c = Qt.color(color)
        const channel = v => v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
        return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b)
    }

    // The colour for text on a filled `background`: white, or black when that contrasts more (WCAG ratio). Pure
    // black, as mid-tone reds and purples reach 4.5:1 only with it.
    function onColor(background) {
        const l = luminance(background)
        return (1.05 / (l + 0.05)) >= ((l + 0.05) / 0.05) ? Qt.color("white") : Qt.color("black")
    }
}
