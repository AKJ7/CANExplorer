# Changelog

All notable changes to CANExplorer are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

CANExplorer follows [semantic versioning](https://semver.org/) from 1.0.0 on. Between `0.0.1` and `1.0.0`, the
development stages are listed by date and commit, newest first.

## [Unreleased]

## [1.1.1] – 2026-10-20

### Fixed

- The installers contain the ETAS BOA (`etas`), IXXAT (`ixxat`) and USB2CAN (`usb2can`) interfaces. These
  backends load their vendor driver when imported, so a build machine without the driver left them out, and their
  devices were neither detected nor usable although the interfaces were listed.
- The Physical I/O tool was shown in the middle of the window, header included, until a device was opened.

## [1.1.0] – 2026-10-20

### Added

- **Snap** (`snap/snapcraft.yaml`): the release workflow builds a strictly confined snap from the Linux bundle and
  publishes it to the Snap Store for release tags (stable; candidate for `-rc` tags, beta for other pre-releases).
  SocketCAN and USB adapters work after `snap connect canexplorer:can-bus` and `canexplorer:raw-usb`. The update
  dialog leaves snaps to snapd.
- **MQTT**: publishes the decoded signals of a connection (`PREFIX/CONNECTION/signals/MESSAGE/SIGNAL`, when they
  change, at most once per interval) and optionally its frames to an MQTT broker, with an online/offline status
  and automatic reconnection (**MQTT…** in the connection's toolbar). Optionally sends the frames that other
  programs publish to `…/send`, limited to an allow list. `can-server` publishes its buses the same way
  (`--mqtt`, `--database`, `--mqtt-frames`, `--mqtt-send`). Needs the `mqtt` extra (aiomqtt), which the installers
  contain.
- **EtherCAT (CoE) tool**: an EtherCAT master for configuring slaves through CANopen over EtherCAT. It finds the
  slaves of a network adapter, brings them to Init, Pre-Op or Safe-Op (reporting the AL status of slaves that
  refuse), reads and writes their objects over SDO with the object dictionary read from the slave (SDO
  information) or an EDS file, reads the PDO assignment and mapping with the mapped objects' values, and collects
  emergency messages. Real adapters need the new `ethercat` extra (pysoem); the installers do not include it and
  offer the simulated network, which works without hardware. Cyclic process data (Op) is not exchanged.
- `BIT1` to `BIT8` data types in object dictionaries.
- **Physical I/O tool**: switches the digital outputs of a USB I/O adapter and shows its digital inputs and
  analogue values (in volts with a configurable reference, with a short chart). Lines are made inputs (no pull,
  pull-up, pull-down, active low) or outputs. Supports aio_usb adapters (the I/O interface of STM32 CAN/LIN/I/O
  adapter firmware) through the new `io` extra (pyusb, libusb), and a simulated device that works without hardware.
  Test scripts use the same devices through `can_explorer.core.physical_io`.
- **GVRET interface** (`gvret`, class `GvretBus`) for devices with SavvyCAN's GVRET protocol: ESP32RET boards,
  the Macchina M2 (M2RET) and the CANDue. The channel is the serial port, with `@1` for the device's second bus,
  or `socket://IP:23` for ESP32RET on WiFi; the bitrate (and listen only mode) of the bus is set when it opens,
  leaving the other bus as it is. Classic CAN only.
- **J2534 interface** (`j2534`, class `J2534Bus`) for vehicle interfaces with an SAE J2534 04.04 driver, such as
  the Drew Technologies Mongoose and CarDAQ and the Tactrix OpenPort 2.0 (Windows). The channel is the registered
  device name or the driver's path; detection lists the registered drivers with CAN. 32-bit drivers, which the
  64-bit application cannot load, run in the J2534 bridge, a 32-bit helper program in the Windows installer (or a
  32-bit Python when run from source, or the program set in `CANEXPLORER_J2534_BRIDGE`).
- **ELM327 interface** (`elm327`, class `Elm327Bus`) for OBD adapters with the ELM327 command set, such as the
  OBDLink MX+, LX, SX and EX, over USB, Bluetooth (the paired adapter's serial port) or WiFi (`socket://IP:35000`).
  Made for diagnostics: it monitors the bus and sends frames between monitoring, at 500 kbit/s divided by a whole
  number, and loses frames on busy buses. Detection lists the USB and Bluetooth serial ports.

### Fixed

- Loading a signal database while a simulation runs restarts the simulation. The nodes look up their messages when
  they start, so a simulation started before the database stayed broken (*Load a signal database to send message
  'VehicleSpeed' by name*) until it was restarted by hand. `can-test` warns when `--simulation` is used without
  `--database`.
- The installers contain the `canexplorer_remote` interface (remote buses of `can-server`) and pyserial's URL
  handlers such as `socket://`; python-can imports them by name, so the bundles left them out.
- Channel detection probes each interface on its own: an error in one driver (python-can lets it end the whole
  detection) no longer hides the channels of all others, and slow drivers such as ETAS BOA get 15 seconds instead
  of 5, so ETAS devices are listed on Windows.

### Changed

- Builds of `master` published to GitHub Packages are packaged with PyInstaller again instead of Nuitka;
  Nuitka stays selectable when starting the `release` workflow manually.

## [1.0.0] – 2026-10-18

The first stable release. Highlights since 0.0.1:

- A dashboard-style interface (QML) on an asyncio core, with six themes, a command palette (Ctrl+K) and German
  texts, handling hundreds of thousands of frames per second and recordings of millions of frames.
- Recording in every replayable format (ASC, BLF, CSV, LOG, TRC, SQLite, MF4, gzip), triggered and split
  recordings, replays with seeking and replay onto a bus, and comparing two recordings.
- Signal databases (DBC, DBF, KCD, SYM, ARXML, LDF) with an editor for CAN and LIN databases, signal plots with
  cursors, and the multi-bus trace for several buses on one timeline.
- Diagnostics and protocol tools: UDS over ISO-TP and LIN, ECU flashing, J1939 and CANopen tools, bus health.
- Simulation and test automation: Python nodes, gateways and QML panels; test scripts with requirements, run in
  the application or headless in CI with `can-test`.
- Remote buses through `can-server`, installers for Windows, Linux and macOS, and a documentation site.

From 1.0.0 on, CANExplorer follows semantic versioning; the public interfaces are listed under
[Stability and versioning](https://akj7.github.io/CANExplorer/reference/#stability-and-versioning).

### Added

#### Connections and buses

- **All CAN interfaces of python-can 4.6** (SocketCAN, PEAK, Vector, Kvaser, IXXAT, ETAS, neoVI, NI-XNET, NI-CAN,
  SYS TEC, USB2CAN, isCAN, Neousys, CANalyst-II, CANtact, gs_usb, slcan, serial, Robotell, Seeed Studio,
  socketcand, UDP multicast, virtual) and the new remote bus, listed with their hardware, platforms, drivers and
  CAN FD support in the *Supported interfaces* reference and the README. The `drivers` extra
  (`uv sync --extra drivers`) installs the Python packages the neoVI, NI-XNET, CANalyst-II, CANtact, gs_usb and
  UDP multicast interfaces need.
- Serial CAN adapters (slcan, python-can's serial protocol) work out of the box: `python-can[serial]` installs
  pyserial.
- CAN FD data bitrate setting.
- Channel detection for all python-can interfaces, run in the background.
- **Remote buses**: `can-server` shares the CAN interfaces of a computer (e.g. a Raspberry Pi in a vehicle) over
  WebSocket, with an optional token; CANExplorer connects with the new *RemoteBus* interface (python-can interface
  `canexplorer_remote`, also usable by `can-test` and scripts).
- **Several buses in one view**: the *Multi-bus trace* tool merges the traces of chosen connections on one
  timeline with a Bus column, each frame decoded with its own connection's protocol and database. Simulation nodes
  receive and send on other connections (`bus='Body'` in `@on_message`, `@on_signal`, `send` and `message`), and
  the new `Gateway` node forwards frames between two buses, optionally changed by `transform`, without loops.
- Hardware timed periodic transmission (python-can cyclic tasks) as an alternative to the application timer.

#### Monitoring and analysis

- **Dashboard user interface** modelled on the learnqt.guide dashboards: a sidebar with the connections and the
  views of the selected connection, a top bar with breadcrumb, filter field, connect button, notifications and
  the application menu, rounded cards throughout and a welcome page with quick actions and recent connections.
  The new **Dashboard** view shows Rx/Tx frame rates, bus load and error frames as cards with one-minute
  sparklines and trends, a bus activity area chart, a traffic breakdown donut chart and the busiest identifiers.
  Six colour themes (Dark, Purple, Nord, Ocean, Forest, Light) are selectable in the preferences. The menu bar
  and the connection tabs are replaced by the sidebar and the **⋮** menu, and errors appear as notifications
  instead of modal dialogs. The window is frameless: the top bar moves it and holds the minimize, maximize and
  close buttons, and the edges resize it.
  A custom Qt Quick Controls style (`CanExplorerStyle`) draws all buttons, text fields, combo boxes, spin boxes,
  check boxes, switches, dialogs, menus, tooltips, tabs, scroll bars and sliders in the theme colours.
- **Bus health** view: error state (active, warning, passive, bus off) with a timeline, error frames, TEC/REC
  error counters and bus load over the last minute, ten minutes or hour, and the decoded error events. Error frames
  (SocketCAN layout) are decoded in the trace as well.
- **Bus statistics**: Rx/Tx frame rates, estimated bus load, error frames and bus state in the status bar.
- **Trace search** (Ctrl+F, F3/Shift+F3) for text, identifiers, data patterns (`data=01 ?? FF`) and signal
  conditions (`EngineData.EngineSpeed > 3500`), in the background through millions of frames; **bookmarks**
  (Ctrl+B) with notes, a bookmark list and markers; a context menu on trace rows.
- **Overview sorting**: clicking a column header sorts the overview by that column (ascending, descending, back to
  identifier order); the order is kept while frames arrive, and selections follow their rows. Bytes that differ
  from the previous frame of an identifier are highlighted in colour as well as bold.
- **Long recordings**: the trace keeps the most recent frames in memory and pages older ones to temporary files
  on disk (5 million frames by default, in the user's cache directory), so millions of frames can be scrolled,
  filtered, searched and exported; preferences for both limits and the directory.
- Live **signal plots** of database signals over a selectable time window.
- **Signal plot analysis**: zoom (mouse wheel) and pan (drag) over the whole signal buffer (200,000 points per
  signal), fixed live windows of 5 s to 5 min and *Fit all*; two **cursors** (click, Shift+click) with the values
  at both cursors, the difference and the minimum, maximum and mean between them; several signals per chart,
  each with its own scale or a shared one; **CSV export** of all or the visible range.

#### Recording and replay

- **More log formats** for recording, export, replay and replay on the bus: ASAM **MF4** (with the optional `mf4`
  extra, which installs asammdf) and gzip compressed text logs (`.asc.gz`, `.csv.gz`, `.log.gz`, `.trc.gz`). One
  list of formats (`core/log_formats.py`) serves all of them, so every recordable format can be replayed; the file
  dialogs name each format and add the extension of the chosen one.
- **Recording options**: splitting into numbered files by size and/or time, and **triggered recording** with start
  and optional stop conditions, time kept before and recorded after the trigger, and re-arming.
- **Replay controls**: replays of log files can be paused and resumed and their speed changed while they run
  (0.25× to 10×, or as fast as possible), with a playback bar showing position, length and frames. The replay
  dialog asks for the speed instead of "reproduce original timing".
- **Jumping within replays**: the playback bar has a position slider and 10 s back/forward buttons; "Replay from
  here" in the context menu of a trace row continues a replay at that frame. Forward jumps pass the frames in
  between at once (statistics stay complete), backward jumps read the recording again; replays on the bus skip
  the frames jumped over.
- **Replay on the bus**: "Replay to bus…" transmits the frames of a log file on a connected bus with their recorded
  timing, at 0.25× to 10× or as fast as possible, optionally filtered and repeated, with a playback bar (progress,
  pause, speed, stop). Errors stop it with a notification.
- **Comparing recordings** (*Tools* → *Compare recordings*): two log files side by side — identifiers only in
  one of them, changed cycle times, lengths and payloads, paired by start time, by the first frame of an
  identifier or by frame sequence; with a signal database, value ranges, means and differences of every signal;
  CSV export.

#### Signal databases

- **Database editor** (Tools → Database editor, ⋮ → Edit Signal Database…, or "Edit database" on a connection):
  create and edit DBC, DBF, KCD and SYM databases, and ARXML databases saved as one of them. Messages
  (name, identifier, length, CAN FD, cycle time, senders, comment), signals (position, byte order, sign, factor,
  offset, range, unit, initial value, receivers, value descriptions, comment) and nodes; a colour coded bit layout;
  checks for overlapping or out-of-frame signals and duplicate identifiers; notes on what the target format cannot
  store; confirmation before discarding unsaved changes. Connections decoding with a saved file reload it.
- **LIN database editing**: LDF files open in the database editor with tabs for frames and signals, nodes with
  their node attributes, encoding types and schedule tables. A new LDF writer saves all sections of LIN 2.x files
  (diagnostic frames, sporadic and event triggered frames, signal groups, all schedule commands); saved files are
  checked by reading them back. **New ▾** creates CAN or LIN databases.
- **BUSMASTER DBF** signal database reader and writer, and conversion between DBC, DBF, KCD, SYM and ARXML
  (input only), in the application (File → Convert Signal Database) and as the `can-db-convert` command.
  DBF and LDF files can be loaded as signal databases.
- **ARXML** container frames (container I-PDUs) are decoded into their contained PDUs, whose signals can be plotted.
- Databases describing several buses (ARXML clusters, KCD buses): the bus of a connection is selected from its
  bitrate, or chosen in the toolbar, so identifiers reused on other buses are decoded correctly.
- `can-db-convert --bus` converts one bus of a multi-bus database.

#### Diagnostics and protocols

- **UDS diagnostics** (ISO 14229) over ISO-TP on CAN and over the LIN transport layer: diagnostic session
  control, ECU reset, tester present keep-alive, read/write data identifiers with names and readable
  values for the standard identifiers, security access with a user supplied seed/key algorithm (a Python file
  defining `compute_key(level, seed)`), reading DTCs with status flags and clearing them, routine control,
  raw requests and a request history. Handles "response pending" and suppressed positive responses.
- **CDD** (Vector CANdelaStudio) diagnostic descriptions, loaded in the Diagnostics tab or with "Load database…": the
  ECU's data identifiers are listed by name and ReadDataByIdentifier responses are decoded into fields (scaling,
  units, text tables, text and float encodings, structures and unions) in the Diagnostics tab and in the UDS and LIN
  trace decoders. Uses an own reader, as cantools' CDD support is deprecated.
- Passive "UDS" protocol decoder describing the services carried in ISO-TP messages.
- Active ISO-TP client (ISO 15765-2) with flow control, block size, separation time and CAN FD.
- **ECU flashing**: the **Flashing** view programs ECUs over UDS from Intel HEX, S-record or binary files:
  programming session, security access, erasing, RequestDownload/TransferData/RequestTransferExit per segment
  with the ECU's block size, a check routine and an ECU reset, with progress, transfer rate and cancelling. The
  simulated ECU accepts downloads; `examples/flashing` has an example firmware and the simulator's key file.
- **J1939 tools**: the **J1939** view of J1939 connections lists the nodes with their address claims (NAME
  fields), lamps and trouble codes; shows the DM1 and DM2 codes of a node with SPN names and FMI texts; requests
  address claims and DM2, clears codes with DM11/DM3; and sends any parameter group by name, number or database
  message from hex data or signal values, with BAM or RTS/CTS for more than 8 bytes. The trace decodes address
  claims and DM1/DM2, also after transport protocol reassembly.
- **CANopen master tools**: the **CANopen** view of CANopen connections lists the nodes with their NMT state and
  emergency messages; sends NMT commands and SYNC; loads object dictionaries from EDS and DCF files; reads and
  writes objects over SDO (expedited and segmented, with abort codes explained) in their data types; and shows the
  PDO mappings, from the dictionary or read from the node, with the received values. The trace names SDO objects
  and decodes PDO values with the dictionary. `CanOpenNodeSimulator` simulates a node from an EDS file, and
  `examples/canopen/io_module.eds` describes an example I/O module.
- **LIN** support through SocketCAN LIN drivers following the sllin convention (header = RTR frame,
  response = data frame), selectable as bus type in the connection dialog:
  - Passive LIN decoder: headers and responses with protected identifiers, diagnostic transport frames
    (0x3C/0x3D) with reassembly, LIN node configuration services and UDS.
  - LDF (LIN description file) signal decoding and signal plots, via `ldfparser`.
  - LIN master: runs LDF schedule tables, with editable values of the signals the master publishes.
  - Header-only transmission, LIN bus load estimation, protected identifier and checksum helpers.

#### Simulation and test automation

- **Node simulation and scripting** (Simulation tab): Python scripts define simulated nodes (`Node` subclasses)
  with handlers for start/stop, received messages (by identifier or database name), signal changes and shared
  variables, timers, sending raw frames or database messages by signal name, and a console. Script errors are
  contained and reported. QML panels get a `panel` object with live signal and variable values, sending and
  logging, and drive the nodes through shared variables. Documentation in `docs/scripting.md`, example in
  `examples/simulation`.
- Simulated UDS ECU and LIN slaves (`can_explorer.core.simulation`) to try the application without hardware.
- **Test automation**: test scripts (`TestModule` classes with `@testcase` methods) check ECUs on the bus with a
  test context offering checks (soft and required), sending by database name or identifier, waiting for frames and
  signal values, cycle time measurement, UDS requests and the simulation's variables. Verdicts passed, failed,
  error (exceptions, timeouts, failing setup) and skipped. They run in the new **Tests** view of a connection (select,
  run, stop, live results and logs) or headless with `can-test` (exit code for CI, `--simulation` to test against
  simulated nodes), with HTML and JUnit XML reports. Example in `examples/testing`.
- **Test automation for CI**: parametrised test cases (`@testcase(params=…, ids=…)`), requirement identifiers per
  test case or module with a requirements table in the HTML report, JUnit properties and `can-test -r REQ`,
  Markdown summaries (`--markdown`, e.g. `$GITHUB_STEP_SUMMARY`), and the example test suite runs against the
  example simulation in the build workflow.

#### User interface

- **Command palette** (Ctrl+K, Ctrl+Shift+P): every command, view, tool, connection, theme and language by typing
  a few letters.
- **Translations**: the interface can be switched to German in the preferences or the command palette, at once
  without restarting; translation files are Qt Linguist `.ts` files kept up to date by
  `tools/update_translations.py`.
- **Workspaces** (`.cxw`): connections with their bus settings or replayed files, databases, filters, plotted
  signals, simulation scripts and panels and test scripts, saved in one file with relative paths and reopened
  with one click (Ctrl+S, Ctrl+Shift+O, recent workspaces on the welcome page).
- **Glass dialogs**: dialogs, menus, popups, combo box lists and the notifications are frosted glass — the window
  content behind them blurred under a translucent theme tint, with a light edge, a sheen and a soft shadow. With
  Qt's software renderer they fall back to opaque panels. The documentation screenshots are rendered with OpenGL.
- **Movable dialogs**: dragging the title of a dialog moves it, within the window; it opens centred again.
- **Resizable sidebar**: drag its right edge (180–420 pixels, at most half the window), double-click to restore the
  default; the width is remembered. The top bar's breadcrumb and filter field shrink in narrow windows instead of
  pushing the window buttons out.
- **Logo** with a transparent background: shown in the sidebar, on the welcome page and in the About dialog, as
  window icon, as icon of the PyInstaller and Nuitka bundles (`.ico`, `.icns`, `.png`) and as logo and favicon of
  the documentation, all generated from the artwork `docs/assets/logo.png` by `tools/make_icons.py`.
- **Desktop icon on Linux**: `--install-desktop-entry` adds CANExplorer with its icon to the desktop's application
  menu (`--uninstall-desktop-entry` removes it), and the window is matched to that entry.
- Recent connections and log files, a preferences dialog (trace buffer size, view refresh interval) and
  remembered window geometry.
- Row selection in the tables, Edit → Copy (tab separated) and Select All, and export of the selected or
  displayed trace rows to a log file.

#### Packaging, documentation and development

- **Updates**: CANExplorer looks for new GitHub releases at start (at most daily, can be turned off, optionally
  including pre-releases) or on request (⋮ → Check for Updates…), shows the release notes, and downloads,
  verifies (`SHA256SUMS.txt`) and installs the update for the way it was installed: Windows setup, AppImage,
  Debian package or macOS app; it then restarts. Versions can be skipped.
- **Installers**: `packaging/installer.py` creates a Windows setup (Inno Setup), a Linux AppImage and `.deb`
  package and a macOS disk image from the bundle. The new `release` workflow (replacing `installer`) builds them
  on the three platforms, installs each to check it starts, stores them as artifacts and publishes them with
  checksums as a GitHub release for `v*` tags, after checking the tag matches the project version.
  Builds of `master` are compiled with Nuitka and published to GitHub Packages
  (`ghcr.io/akj7/canexplorer-installers`, via ORAS).
- Nuitka as an alternative packaging backend (`packaging/build.py --backend nuitka [--onefile]`), selectable
  in the installer workflow.
- **Documentation site** built with MkDocs and Material for MkDocs (`docs/`, `mkdocs.yml`): installation, quick start,
  a user guide for every part of the application, reference pages (scripting API generated from the docstrings, QML
  panel API, command line, file formats, keyboard shortcuts) and development guides, with screenshots generated
  from the running application (`tools/docs_screenshots.py`). The `docs` workflow builds it strictly and publishes
  it to GitHub Pages.
- Documentation in the **Dracula** colour scheme (`docs/stylesheets/dracula.css`, on top of Material for MkDocs).
- Throughput benchmark of the receive pipeline and the table models (`tools/benchmark.py`).
- Test fixtures and tests for ARXML, KCD, SYM and CDD files.

### Changed
- **The application command is now `canexplorer`** (was `main`, which clashed with other tools on the PATH):
  `uv run canexplorer`, `canexplorer --install-desktop-entry`.
- The default log level is `INFO` (was `DEBUG`); the unused `debug_mode` setting was removed.
- `can-server` and `RemoteBus` check that they speak the same protocol version and say which side to update.
- The Docker setup (`Dockerfile`, `docker-compose.yml`) provides a development shell, the test suite and a
  `can-server` service sharing the host's CAN interfaces.
- Project metadata: description, classifiers and project URLs; `pydantic` is no longer a direct dependency (it
  comes with `pydantic-settings`).
- Receive pipeline about 4–7× faster (70 000 → 300 000 frames/s raw, 46 000 → 280 000 frames/s with J1939
  and DBC decoding): frames are handed from the receive thread to the event loop in batches, log replay is
  read in chunks, signal databases are only decoded for displayed rows and the result is cached, and the
  changed-byte highlighting is computed on demand.
- The overview only refreshes the rows that changed; rebuilding an unfiltered trace is about 20× faster.
- Diagnostics moved into a dedicated controller with a reworked Diagnostics tab.
- `MainApplication` simplified; `canutils` reduced to the helpers in use.
- Runtime dependencies trimmed to what is imported: removed `matplotlib`, `numpy`, `qtrio`, `schema` and
  `pytest-schema`; `pyinstaller` and `nuitka` moved to a `packaging` dependency group and `rust-just` to the
  `dev` group. Added `ldfparser` (pinned) for LIN.
- Builds without package metadata (Nuitka) carry the application version in a generated module.

### Removed
- The unused `can_server` heartbeat module, which needed the undeclared `aiofile` package.
- The AsciiDoc documentation template (`docs/full_doc.adoc`) and the outdated `docs/todo.md`; the references moved
  to the documentation site.
- The unused `can_explorer/transport` package (IsoCAN, ISO-TP, J1939 and CANopen transports), superseded by
  the decoders and clients in `can_explorer/core`, together with its helpers (`util/worker.py`,
  `util/validator.py`) and tests.
- Obsolete version helpers (`util/version.py`, `sys/version.py`), the empty `database` package, the crash
  handler stub, the placeholder `CustomComponent.qml` and the trivial demo test.
- Empty `publish`, `release` and `security` workflows.

### Fixed
- ISO-TP transfers with separation times below the system timer resolution (e.g. 100 µs) took about 15 ms per
  frame on Windows; frames are now paced with a precise clock.
- Table columns (trace, overview, multi-bus trace) can be resized by dragging the header edges.
- Drop-down lists of combo boxes with named entries (e.g. the language) showed empty rows.
- The About dialog named the license as GPL3; CANExplorer is MIT licensed.
- Connecting to an unreachable `can-server` reported different messages on Windows and Linux.
- A binding loop in the dialog style (an elided dialog title).
- Converting a multi-bus database or one with container frames lost information silently; it is now reported,
  and the signal count includes the signals of contained PDUs.
- The QML window test fixture could crash the test process at exit.
- The GUI could be starved under sustained traffic because the session never yielded to the event loop.
- Default bitrate and data bitrate in the connection dialog were not preselected.
- Channel detection printed a warning for every missing driver and was left pending when quitting.
- `.vscode/extensions,json` renamed to `extensions.json`.

## 2026-10-18 – Asyncio core, QML user interface and tooling (`5bc1953`)

### Added
- Qt-independent asyncio core (`can_explorer/core`): message sources for live buses (`LiveSource`) and log
  file replay (`FileSource`), `BusSession` with a bounded trace buffer, per-identifier statistics, message
  filters, recording and decoding.
- Qt and asyncio run on one event loop (qasync); views are refreshed in batches about 30 times per second.
- Trace and per-identifier overview views (count, cycle time, changed bytes highlighted).
- Display filters: hex identifiers and ranges, exclusions and `std`/`ext`/`rx`/`tx`/`fd` keywords.
- Recording to `.asc`, `.blf`, `.csv`, `.log`, `.trc` and `.db`, and replay with or without the original timing.
- DBC/KCD/SYM/ARXML signal decoding with cantools.
- Transmission of single and periodic frames.
- Passive protocol decoders: ISO-TP (with reassembly), J1939 (PGNs, requests, BAM and RTS/CTS transport)
  and CANopen (NMT, SYNC, EMCY, PDO, SDO, heartbeat).
- Basic UDS requests over ISO-TP in a Diagnostics tab.
- PyInstaller packaging (`packaging/build.py`) and an installer workflow building Linux, Windows and macOS
  bundles and attaching them to GitHub releases for `v*` tags.
- CI workflow running ruff, black and pytest on Linux, Windows and macOS with Python 3.10 and 3.14.
- Test suite for the core, the decoders, the Qt models and the QML window, using python-can's virtual bus.

### Changed
- Package management moved from Poetry to uv (PEP 621 metadata, hatchling build backend, `uv.lock`).
- User interface fully moved from Qt Widgets (`.ui` files) to QML: main window, new connection dialog,
  about dialog with copy to clipboard, trace table.
- Settings read `CANEXPLORER_*` environment variables and parse command line arguments only when the
  application starts.
- The application version is read from the package metadata instead of `git describe`.
- Code formatted with black, linted with ruff.

### Removed
- Qt Widgets user interface (`.ui` files, widget classes, stylesheet) and the thread-pool workers.

### Fixed
- Resource paths depended on the working directory.
- The application aborted when started from the VS Code snap terminal (GSettings schema error caused by
  the snap's GTK environment).
- `Settings` parsed foreign command line arguments when imported (e.g. by pytest).
- Recording to BLF could fail for channel names ending in digits and stopped the whole session; a failing
  recording now only stops the recording.
- Closing a bus blocked the GUI for up to one second.
- Import-time asyncio usage that fails on Python 3.14.
- `CanMessage.from_can` copied `is_fd` into `is_extended_id`.

## 2025-11-30 – QML support (`c6342fc`)

### Added
- QML main window (`ApplicationWindow`) with File, Edit and Help menus, shared actions and an about dialog.
- `MainApplication` hosting the QML engine; QML warnings and Qt messages routed to the Python logger.
- Application metadata (name, version, build information) exposed to QML.
- Configuration with pydantic-settings and coloured console logging.

## 2025-10-13 – Resume project (`392563d`)

### Added
- J1939 addressing and controller applications in the J1939 transport.
- Asyncio based worker base class.
- IsoCAN transport tests on python-can's virtual bus.

### Changed
- IsoCAN transport reworked on top of asyncio.

## 2025-02-10 – J1939 transport (`f0e30fb`)

### Added
- J1939 PDU parsing and frame information (identifier length, arbitration type).

## 2025-02-08 – Refactoring (`8ee4442`)

### Added
- First QML main window.
- J1939 and CANopen transport skeletons and a J1939 test.
- `can_server` package (aiohttp based server).

### Changed
- Transports reorganised into packages (`isocan`, `isotp`, `j1939`, `canopen`).

## 2025-02-08 – Poetry (`589ec5f`)

### Changed
- Package management moved from Pipenv to Poetry.
- Docker based development container and docker-compose setup; justfile for development tasks.

## 2024-11-10 – ISO-TP (`16b66cb`)

### Added
- ISO-TP processing according to ISO 15765-2 (work in progress): addressing modes, PDU parsing, error
  codes and the transport state machine.
- `CanMessage` model.

## [0.0.1] – 2024-10-21

Called "Milestone 0.1.0" in the commit message and the README roadmap.

### Added
- IsoCAN frames are received through python-can and displayed in the GUI.
- Environment based configuration (`.env`).

## 2024-10-16 – Initial commit (`a8d624b`)

### Added
- Qt Widgets user interface: main window, new connection dialog, about dialog and a raw CAN frame table.
- Pipenv based project setup, VS Code development container, Dockerfile and a CI workflow skeleton.

[Unreleased]: https://github.com/AKJ7/CANExplorer/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/AKJ7/CANExplorer/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/AKJ7/CANExplorer/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/AKJ7/CANExplorer/releases/tag/v1.0.0
[0.0.1]: https://github.com/AKJ7/CANExplorer/releases/tag/0.0.1
