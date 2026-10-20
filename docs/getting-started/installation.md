# Installation

## Requirements

- Linux, Windows or macOS.
- For running from source: Python 3.10 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- A CAN interface supported by python-can with its driver (all of them are listed under
  [Supported interfaces](../reference/interfaces.md)), or none at all: the `virtual` interface simulates a bus
  inside the application.

## Installers

Every [release](https://github.com/AKJ7/CANExplorer/releases) has installers; no Python installation is needed.

| Platform | File | Installation |
|---|---|---|
| Windows 10/11 (x64) | `CANExplorer-<version>-windows-x86_64-setup.exe` | Run it. It installs for the current user without administrator rights (or for all users, when chosen), adds a start menu entry and can be removed under *Apps* |
| Linux, any distribution (x86-64) | `CANExplorer-<version>-linux-x86_64.AppImage` | `chmod +x CANExplorer-*.AppImage` and start it; `./CANExplorer-*.AppImage --install-desktop-entry` adds a menu entry |
| Debian, Ubuntu (x86-64) | `CANExplorer-<version>-linux-x86_64.deb` | `sudo apt install ./CANExplorer-*.deb`; starts from the menu or with `canexplorer` |
| macOS (Apple silicon) | `CANExplorer-<version>-macos-arm64.dmg` | Open it and drag CANExplorer to *Applications* |

On Linux distributions with snapd, CANExplorer is also in the Snap Store; see [Snap](#snap).

Installed copies update themselves: CANExplorer announces new releases and installs them on request, see
[Updates](../user-guide/updates.md).

The zip archives of the same release contain the application without installer: unpack and start `CANExplorer`
(`CANExplorer.app` on macOS). `SHA256SUMS.txt` lists the checksums of all files.

The Linux builds need glibc 2.35 or newer (Ubuntu 22.04, Debian 12, Fedora 36 and later). Intel Macs can use the
installation from source.

## Snap

```bash
sudo snap install canexplorer
sudo snap connect canexplorer:can-bus   # SocketCAN adapters: can0, vcan0 …
sudo snap connect canexplorer:raw-usb   # USB adapters: candleLight/gs_usb, slcan, ELM327, GVRET …
```

snapd updates the snap itself. Pre-releases are in the `candidate` (release candidates) and `beta` channels:
`sudo snap install canexplorer --candidate`.

The snap is confined: it reaches the hardware only through the interfaces connected above, and files only in
your home folder and on removable media. It cannot use driver libraries installed on the computer (PCAN-Basic,
Kvaser CANlib), so these adapters work through SocketCAN, and not Bluetooth serial ports (`/dev/rfcomm*`). Set up
SocketCAN interfaces (`ip link`) outside the snap, as described [below](#setting-up-interfaces). Use the AppImage or
the `.deb` when you need these.

!!! note "Unsigned installers"
    The installers are not code signed yet. Windows SmartScreen warns about an unknown publisher: choose *More info →
    Run anyway*. macOS refuses to open apps from unidentified developers: open CANExplorer with a right click →
    *Open* the first time (or allow it under *System Settings → Privacy & Security*).

## From source

```bash
git clone https://github.com/AKJ7/CANExplorer.git
cd CANExplorer
uv sync
uv run canexplorer
```

`uv sync` creates a virtual environment in `.venv` with all dependencies. `uv run canexplorer` starts the application;
settings can be passed on the command line, see [Command line](../reference/command-line.md).

!!! tip "Starting from the VS Code snap terminal"
    VS Code installed as a snap passes its own GTK libraries to programs started from its terminal, which
    makes Qt applications abort with *Settings schema 'org.gnome.settings-daemon.plugins.xsettings' does not
    contain a key named 'antialiasing'*. CANExplorer removes these variables at startup, so it starts normally.

## Desktop menu entry (Linux)

On Windows and macOS the application carries its icon, so it appears with the logo on the desktop, in the start
menu, the dock and the Finder. On Linux, add CANExplorer with its icon to the application menu of your desktop
with

```bash
uv run canexplorer --install-desktop-entry          # From source
./CANExplorer --install-desktop-entry        # Standalone bundle
```

This writes `~/.local/share/applications/io.github.akj7.CANExplorer.desktop` and the icons in
`~/.local/share/icons/hicolor/`, starting the application from where it was run. The desktop also shows the logo
for the running window. `--uninstall-desktop-entry` removes them again.

## Setting up interfaces

CANExplorer opens interfaces through python-can, so the driver of your adapter must be installed.
[Supported interfaces](../reference/interfaces.md) lists every interface with its hardware, platforms, driver and
CAN FD support. A few common cases:

=== "SocketCAN (Linux)"

    Most USB adapters on Linux (PEAK, Kvaser, gs_usb/candleLight, …) appear as SocketCAN network interfaces.
    Set the bitrate and bring the interface up before connecting:

    ```bash
    sudo ip link set can0 type can bitrate 500000
    sudo ip link set can0 up
    # CAN FD, e.g. 500 kbit/s nominal and 2 Mbit/s data bitrate
    sudo ip link set can0 type can bitrate 500000 dbitrate 2000000 fd on
    ```

    Choose the interface *SocketcanBus* and the channel `can0` in CANExplorer. With SocketCAN the bitrate is
    configured with `ip`, not in CANExplorer.

=== "Virtual CAN (Linux)"

    A `vcan` interface is a software bus shared by all programs on the computer, e.g. to connect CANExplorer
    with `candump`/`cansend` from can-utils:

    ```bash
    bash tools/can_create.sh --interface vcan0
    # For CAN FD frames, also:
    sudo ip link set vcan0 mtu 72
    ```

=== "virtual (all platforms)"

    The python-can `virtual` interface (*VirtualBus*) needs no setup. All connections using the same channel
    name inside CANExplorer share one bus, which is enough to try out the application, to replay logs and to run
    [simulations](../user-guide/simulation.md).

=== "Serial adapters (all platforms)"

    Adapters on a serial port need no driver. Lawicel/slcan compatible adapters (CANable with slcan firmware,
    USBtin, …) use the interface *slcanBus* with the serial port as channel, e.g. `/dev/ttyACM0` or `COM3`;
    a baud rate other than the default is given after `@`, e.g. `/dev/ttyUSB0@115200`. *SerialBus* speaks
    python-can's own serial protocol. The bitrate of the bus is set in the connection dialog.

=== "Windows and macOS"

    Install the driver of your adapter (PEAK PCAN-Basic, Kvaser CANlib, Vector XL Driver Library, …), then choose
    the matching interface in CANExplorer. Use **Detect** in the connection dialog to list the channels the
    drivers report.

### LIN

LIN is accessed through Linux LIN drivers that present the bus as a SocketCAN interface, such as
[sllin](https://rtime.felk.cvut.cz/gitweb/linux-lin.git) for UART based LIN transceivers. Following the sllin README,
the driver is loaded with the role and baud rate and attached to the serial port:

```bash
sudo insmod sllin.ko master=1 baudrate=19200   # master=0 for slave (listen) mode
sudo ldattach 25 /dev/ttyS0                    # 25 is the sllin line discipline
sudo ip link set sllin0 up
```

Then create a connection with bus type **LIN**, interface *SocketcanBus* and channel `sllin0`. See [LIN](../user-guide/lin.md).
