<p align="center"><img src="docs/assets/logo-banner.png" alt="CANExplorer logo" width="360"></p>

<h1 align="center">CANExplorer</h1>

<p align="center">
  CANBus monitoring for the modern man!  
  
  Monitor, analyse, diagnose, simulate and test <b>CAN</b>, <b>CAN FD</b> and <b>LIN</b> buses — on Linux,
  Windows and macOS, with any adapter python-can supports, or without hardware on a virtual bus.
</p>

<p align="center">
  <a href="https://akj7.github.io/CANExplorer/">Documentation</a> ·
  <a href="https://akj7.github.io/CANExplorer/getting-started/quick-start/">Quick start</a> ·
  <a href="https://github.com/AKJ7/CANExplorer/releases">Downloads</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

![CANExplorer dashboard](docs/assets/screenshots/dashboard.png)

## Features

**Monitoring and analysis**
- Live view of any [python-can](https://python-can.readthedocs.io/) interface: SocketCAN, PCAN, Vector, Kvaser,
  gs_usb, slcan and other serial adapters, virtual buses; CAN FD with a separate data bitrate; channel detection
- Dashboard with frame rates, bus load, error frames, bus activity, traffic breakdown and the busiest identifiers
- Trace and per-identifier overview (cycle times, changed bytes highlighted, sortable columns), display filters
  such as `100-1FF !123 ext rx fd`, search by text, data pattern or signal condition, and bookmarks
- Millions of frames per session: older frames are paged to disk
- Bus health: error state timeline, error frames, TEC/REC and bus load over up to an hour
- Remote buses: `can-server` shares the interfaces of e.g. a Raspberry Pi in a vehicle over the network
- MQTT: publish decoded signals (and frames) to a broker for dashboards and home automation, from the application or
  `can-server`, and optionally send frames requested over MQTT
- Signal plots with zoom and pan over the whole buffer, two cursors with values and statistics, several signals per
  chart and CSV export
- Reverse engineering: bit and byte statistics per identifier and a notch; automatic detection of checksums,
  counters, multiplexers and values; matching fields to database signals, CSV/GPS logs or polled OBD-II values;
  event markers; and bit fields to plot without a database or add to one

**Recording and replay**
- ASC, BLF, CSV, candump LOG, PEAK TRC, SQLite, ASAM MF4 and gzip compressed text logs
- Splitting into numbered files by size or time, and triggered recording with conditions such as
  `EngineData.EngineSpeed > 6000`, keeping the time before and after the event
- Replay at 0.25× to 10× or as fast as possible, with pause and jumping to any position
- Replay of a recording onto a real bus, filtered and repeated
- Several buses on one timeline, and simulated gateways forwarding frames between them
- Comparison of two recordings: identifiers only in one of them, changed cycle times and payloads, and signal
  values, aligned by time, by a trigger identifier or by frame sequence

**Signal databases**
- Vector DBC, BUSMASTER DBF, AUTOSAR ARXML (including container PDUs), Kayak KCD, PCAN SYM and LIN LDF
- Database editor for CAN and LIN databases (frames, signals, nodes, encodings, schedule tables) with a bit layout
  view and consistency checks, and conversion between the formats

**Diagnostics and protocols**
- UDS over ISO-TP and the LIN transport layer: sessions, reset, data identifiers, security access with your own
  seed/key algorithm, DTCs, routines; CANdelaStudio CDD and ASAM ODX/PDX descriptions for named and decoded identifiers
- ECU flashing over UDS (RequestDownload/TransferData) from Intel HEX, S-record or binary files, with progress
- DoIP (Diagnostics over IP, ISO 13400-2): finds DoIP entities, connects with routing activation (optionally over
  TLS) and runs diagnostics and flashing over Ethernet; a simulated entity works without a vehicle
- UDS discovery: scan an ECU for the services, data identifiers, subfunctions and routines it exposes, classifying
  the answers, for reverse engineering diagnostics
- J1939: nodes with their address claims, DM1/DM2 trouble codes and lamps, clearing codes, requests and sending
  parameter groups by name with BAM or RTS/CTS
- CANopen master: NMT, SDO access to object dictionaries from EDS/DCF files, PDO mappings with live values,
  emergency messages
- CANopen over EtherCAT (CoE): EtherCAT slaves with their states, object dictionaries (SDO information or EDS),
  PDO assignment and emergencies; real adapters need the `ethercat` extra, a simulated network works without
- Physical I/O: switch the digital outputs of a USB I/O adapter (aio_usb) and watch its inputs and analogue values,
  from the tool or test scripts; needs the `io` extra, a simulated device works without
- Decoding of ISO-TP, UDS, J1939, CANopen and LIN diagnostic frames in the trace
- LIN master schedule tables

**Simulation and test automation**
- Simulated ECUs written in Python, controlled from QML panels
- ECU test scripts with checks, timing measurements and UDS requests; pass/fail verdicts in the Tests view or
  headless with `can-test`, with HTML, JUnit and Markdown reports for CI; parametrised test cases and requirement
  traceability

**Workspace**
- Connections, databases, filters, plots and scripts saved in one workspace file
- Dashboard-style interface with six colour themes, a command palette (Ctrl+K) and English and German texts

See the [documentation](https://akj7.github.io/CANExplorer/) for a guide to every feature.

## Installation

### Installers

Download the installer for your platform from the [releases](https://github.com/AKJ7/CANExplorer/releases): a
`setup.exe` for Windows, an AppImage or `.deb` for Linux and a `.dmg` for macOS (Apple silicon). Portable zip
archives are attached as well. No Python installation is needed.

On Linux, CANExplorer is also a snap: `sudo snap install canexplorer`, then connect the hardware interfaces with
`sudo snap connect canexplorer:can-bus` (SocketCAN) and `sudo snap connect canexplorer:raw-usb` (USB adapters).

### From source

With [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.10 or newer:

```bash
git clone https://github.com/AKJ7/CANExplorer.git
cd CANExplorer
uv sync                  # Add --extra mf4 for ASAM MF4 log files (installs asammdf, numpy and pandas),
                         # --extra ethercat for EtherCAT adapters (installs pysoem),
                         # --extra io for USB I/O adapters (installs pyusb and libusb)
uv run canexplorer
```

On Linux, `uv run canexplorer --install-desktop-entry` adds CANExplorer with its icon to the application menu.

### Interfaces

CANExplorer supports every interface of [python-can](https://python-can.readthedocs.io/en/stable/interfaces.html)
4.6. Install the vendor's driver for your adapter; the
[supported interfaces](https://akj7.github.io/CANExplorer/reference/interfaces/) page lists the drivers, CAN FD
support and the Linux kernel drivers.

| Interface | Hardware | Platforms |
|---|---|---|
| SocketCAN (`socketcan`) | Any adapter with a Linux kernel driver: PEAK, Kvaser, candleLight/gs_usb (CANable), ETAS ES58x, 8devices, EMS, esd, MCP2515/MCP251xFD (Raspberry Pi hats), virtual `vcan`; LIN through sllin | Linux |
| PEAK PCAN (`pcan`) | PCAN-USB, PCAN-USB FD, PCAN-PCI(e), PCAN-LAN | Windows, Linux, macOS |
| Vector (`vector`) | VN16xx, VN56xx and other XL Driver Library devices, Vector virtual channels | Windows |
| Kvaser (`kvaser`) | Leaf, U100, Memorator, USBcan | Windows, Linux |
| IXXAT (`ixxat`) | HMS IXXAT USB-to-CAN, CAN-IB | Windows |
| ETAS (`etas`) | ES581, ES582, ES584, ES891 (BOA) | Windows |
| Intrepid neoVI (`neovi`) ¹ | neoVI, ValueCAN, RAD-Galaxy | Windows, Linux, macOS |
| NI-XNET (`nixnet`) ¹ | National Instruments NI-XNET devices | Windows |
| NI-CAN (`nican`) | National Instruments NI-CAN devices | Windows |
| SYS TEC (`systec`) | USB-CANmodul | Windows |
| 8devices USB2CAN (`usb2can`) | USB2CAN (Korlan) | Windows |
| Thorsis isCAN (`iscan`) | isCAN | Windows |
| Neousys (`neousys`) | CAN ports of Neousys industrial computers | Windows, Linux |
| CANalyst-II (`canalystii`) ¹ | Chuangxin CANalyst-II | Windows, Linux, macOS |
| CANtact (`cantact`) ¹ | CANtact, CANable | Windows, Linux, macOS |
| gs_usb (`gs_usb`) ¹ | candleLight/gs_usb firmware adapters through libusb | Windows, Linux, macOS |
| slcan (`slcan`) | Lawicel CANUSB, CANable (slcan firmware), USBtin and other serial slcan adapters | Windows, Linux, macOS |
| GVRET (`gvret`) | ESP32RET boards, Macchina M2 (M2RET), CANDue: SavvyCAN's GVRET protocol over USB serial or WiFi | Windows, Linux, macOS |
| J2534 (`j2534`) | J2534 pass-thru devices: Mongoose, CarDAQ, OpenPort 2.0, Bosch, Autel …; 32-bit drivers through a bridge process | Windows |
| ELM327 / OBDLink (`elm327`) | OBDLink MX+, LX, SX, EX and ELM327 compatible OBD adapters over USB, Bluetooth or WiFi, for diagnostics | Windows, Linux, macOS |
| python-can serial (`serial`) | Adapters with python-can's serial protocol | Windows, Linux, macOS |
| Robotell (`robotell`) | Robotell USB-CAN | Windows, Linux, macOS |
| Seeed Studio (`seeedstudio`) | USB-CAN Analyzer | Windows, Linux, macOS |
| socketcand (`socketcand`) | SocketCAN buses of another Linux computer, over the network | Windows, Linux, macOS |
| CANExplorer remote (`canexplorer_remote`) | Buses shared by `can-server` on another computer, e.g. a Raspberry Pi | Windows, Linux, macOS |
| UDP multicast (`udp_multicast`) ¹ | A software bus between computers over UDP multicast | Windows, Linux, macOS |
| Virtual (`virtual`) | A software bus inside CANExplorer, no hardware needed | Windows, Linux, macOS |

¹ Needs Python packages that the installers do not include: run from source with `uv sync --extra drivers`.

## Try it without hardware

Run the example ECU tests against the example engine simulation on a virtual bus:

```bash
uv run can-test examples/testing/vehicle_tests.py \
    --database examples/simulation/vehicle.dbc \
    --simulation examples/simulation/engine_simulation.py \
    --report report.html
```

In the application, connect to a *VirtualBus*, load `examples/simulation/vehicle.dbc` and run
`engine_simulation.py` in the Simulation view; the
[quick start](https://akj7.github.io/CANExplorer/getting-started/quick-start/) walks through it.

## Command line

| Command | Purpose |
|---|---|
| `uv run canexplorer` | The application. Options such as `--log_level DEBUG`, or `CANEXPLORER_*` environment variables |
| `uv run can-test SCRIPT …` | Run ECU test scripts on a bus; exit code and HTML/JUnit reports for CI |
| `uv run can-db-convert IN OUT` | Convert signal databases, e.g. `vehicle.dbc vehicle.dbf`, or one bus with `system.arxml powertrain.dbc --bus Powertrain` |

## Development

```bash
uv run pytest                                  # Tests (headless Qt, python-can virtual buses)
uv run ruff check . && uv run black --check .  # Lint and formatting
uv run --group docs mkdocs serve               # Documentation preview on http://127.0.0.1:8000
uv run python packaging/build.py --archive     # Standalone bundle in dist/ (--backend nuitka to compile)
uv run python packaging/installer.py           # Installers of the platform in dist/installers
```

The core (`can_explorer/core`) is Qt-independent asyncio code; `can_explorer/gui` adapts it to the QML interface
in `can_explorer/gui/qt`, and Qt and asyncio share one event loop through
[qasync](https://github.com/CabbageDevelopment/qasync). The
[development guide](https://akj7.github.io/CANExplorer/development/) covers the architecture, testing,
packaging and contributing.

## License

MIT, as declared in [pyproject.toml](pyproject.toml).
