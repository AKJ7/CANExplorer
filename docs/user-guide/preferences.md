# Preferences and settings

## Preferences

**Settings** in the sidebar (or **⋮ → Preferences…**) opens the preferences:

![Preferences](../assets/screenshots/preferences.png)

| Setting | Default | Range | Description |
|---|---|---|---|
| Theme | Dark | Dark, Purple, Nord, Ocean, Forest, Light | Colours of the user interface; applies immediately |
| Trace buffer (frames) | 100 000 | 1 000 – 2 000 000 | Frames kept per connection for the trace; applies to connections created afterwards |
| View refresh interval (ms) | 33 | 16 – 1 000 | How often new frames are shown; longer intervals reduce the CPU load on busy buses |

**Clear recent connections and files** empties the recent lists of the **⋮** menu and the welcome page.

## Stored state

CANExplorer remembers the recent connections and log files, the preferences, the theme and the window position and size. They
are stored with Qt's settings mechanism:

| Platform | Location |
|---|---|
| Linux | `~/.config/AKJ7/CANExplorer.conf` |
| Windows | Registry, `HKEY_CURRENT_USER\Software\AKJ7\CANExplorer` |
| macOS | `~/Library/Preferences/com.akj7.CANExplorer.plist` |

## Application settings

Logging and other application settings are read from `CANEXPLORER_*` environment variables, a `.env` file in the
working directory and the command line; see [Command line](../reference/command-line.md).
