# Preferences and settings

## Preferences

**Settings** in the sidebar (or **⋮ → Preferences…**) opens the preferences:

![Preferences](../assets/screenshots/preferences.png)

| Setting | Default | Range | Description |
|---|---|---|---|
| Theme | Dark | System, Amethyst, Aquarius, Beach, Beach Towels, Beautiful Blues, Blueberry, Bumblebee, Cappuccino, Catppuccin Latte, Catppuccin Mocha, Colour-Safe Dark, Colour-Safe Light, Cyberpunk, Dark, Dracula, Ember, Forest, Gruvbox Dark, Gruvbox Light, High Contrast, High Contrast Light, Light, Lollipop, Metro, Midnight Plum, Moonlight, Night Vision, Nord, Nord Light, Ocean, One Dark, Pastel Rainbow, Phosphor, Pink Gold, Prism, Purple, Purple Skyline, Rainbow, Rosé, Solarized Dark, Solarized Light, Space Gray, Tokyo Night | Colours of the user interface, shown with swatches. The picked theme is previewed at once; **OK** keeps it, **Cancel** goes back. **System** follows the light or dark mode of the operating system |
| In light mode, In dark mode | Light, Dark | The themes with a light (dark) background | Shown only with the System theme: the theme it uses while the operating system is in light (dark) mode |
| Language | System language | English, Deutsch, Français, 中文, 日本語 | Language of the user interface; applies immediately. Texts without a translation stay in English; see [Translations](../development/translations.md) to add a language |
| Trace in memory (frames) | 100 000 | 1 000 – 2 000 000 | The most recent frames of the trace, kept in memory |
| Trace history (frames) | 5 000 000 | 0 – 50 000 000 | Frames of the trace kept in total, the older ones on disk (about 160 bytes each); 0 keeps only those in memory. See [long recordings](trace-and-overview.md#long-recordings) |
| Trace history directory | the cache directory | | Where the temporary files of the history go, e.g. `~/.cache/AKJ7/CANExplorer/trace` on Linux |
| GPS map: show map tiles | Off | | Draw GPS tracks on map tiles downloaded from the tile server, which then sees the map area shown; off, the track is drawn on a plain background. See [Video and GPS map](video-and-map.md#map-tiles) |
| Map tile URL | OpenStreetMap | | A tile server URL with `{z}`, `{x}` and `{y}`; **Default** restores OpenStreetMap's |
| Updates: check at start | On | | Look for new releases on GitHub each time CANExplorer starts; see [Updates](updates.md) |
| Updates: pre-releases | Off | | Also offer release candidates and other pre-releases |
| View refresh interval (ms) | 33 | 16 – 1 000 | How often new frames are shown; longer intervals reduce the CPU load on busy buses |

The trace settings apply to connections opened afterwards.

**Clear recent connections and files** empties the recent lists (connections, log files, workspaces) of the **⋮**
menu and the welcome page.

## Stored state

CANExplorer remembers the recent connections and log files, the preferences, the theme, the width of the sidebar and the window position and size. They
are stored with Qt's settings mechanism:

| Platform | Location |
|---|---|
| Linux | `~/.config/AKJ7/CANExplorer.conf` |
| Windows | Registry, `HKEY_CURRENT_USER\Software\AKJ7\CANExplorer` |
| macOS | `~/Library/Preferences/com.akj7.CANExplorer.plist` |

## Application settings

Logging and other application settings are read from `CANEXPLORER_*` environment variables, a `.env` file in the
working directory and the command line; see [Command line](../reference/command-line.md).
