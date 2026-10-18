# Changelog

All notable changes to CANExplorer are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Only `0.0.1` has been tagged so far. Later development stages are listed by date and commit, newest first.

## [Unreleased]

### Added
- **LIN** support through SocketCAN LIN drivers following the sllin convention (header = RTR frame,
  response = data frame), selectable as bus type in the connection dialog:
  - Passive LIN decoder: headers and responses with protected identifiers, diagnostic transport frames
    (0x3C/0x3D) with reassembly, LIN node configuration services and UDS.
  - LDF (LIN description file) signal decoding and signal plots, via `ldfparser`.
  - LIN master: runs LDF schedule tables, with editable values of the signals the master publishes.
  - Header-only transmission, LIN bus load estimation, protected identifier and checksum helpers.
- **UDS diagnostics** (ISO 14229) over ISO-TP on CAN and over the LIN transport layer: diagnostic session
  control, ECU reset, tester present keep-alive, read/write data identifiers with names and readable
  values for the standard identifiers, security access with a user supplied seed/key algorithm (a Python file
  defining `compute_key(level, seed)`), reading DTCs with status flags and clearing them, routine control,
  raw requests and a request history. Handles "response pending" and suppressed positive responses.
- Passive "UDS" protocol decoder describing the services carried in ISO-TP messages.
- Active ISO-TP client (ISO 15765-2) with flow control, block size, separation time and CAN FD.
- Simulated UDS ECU and LIN slaves (`can_explorer.core.simulation`) to try the application without hardware.
- **BUSMASTER DBF** signal database reader and writer, and conversion between DBC, DBF, KCD, SYM and ARXML
  (input only), in the application (File → Convert Signal Database) and as the `can-db-convert` command.
  DBF and LDF files can be loaded as signal databases.
- Live **signal plots** of database signals over a selectable time window.
- **Bus statistics**: Rx/Tx frame rates, estimated bus load, error frames and bus state in the status bar.
- CAN FD data bitrate setting.
- Channel detection for all python-can interfaces, run in the background.
- Recent connections and log files, a preferences dialog (trace buffer size, view refresh interval) and
  remembered window geometry.
- Row selection in the tables, Edit → Copy (tab separated) and Select All, and export of the selected or
  displayed trace rows to a log file.
- Hardware timed periodic transmission (python-can cyclic tasks) as an alternative to the application timer.
- Nuitka as an alternative packaging backend (`packaging/build.py --backend nuitka [--onefile]`), selectable
  in the installer workflow.
- Throughput benchmark of the receive pipeline and the table models (`tools/benchmark.py`).

### Changed
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
- Docker image installs its packages in a single layer.
- Builds without package metadata (Nuitka) carry the application version in a generated module.

### Removed
- The unused `can_explorer/transport` package (IsoCAN, ISO-TP, J1939 and CANopen transports), superseded by
  the decoders and clients in `can_explorer/core`, together with its helpers (`util/worker.py`,
  `util/validator.py`) and tests.
- Obsolete version helpers (`util/version.py`, `sys/version.py`), the empty `database` package, the crash
  handler stub, the placeholder `CustomComponent.qml` and the trivial demo test.
- Empty `publish`, `release` and `security` workflows.

### Fixed
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

[Unreleased]: https://github.com/AKJ7/CANExplorer/compare/0.0.1...HEAD
[0.0.1]: https://github.com/AKJ7/CANExplorer/releases/tag/0.0.1
