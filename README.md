# CANExplorer
A CANbus-Analyse Tool for all platforms.

## Features
- Live monitoring of any [python-can](https://python-can.readthedocs.io/) interface (SocketCAN, PCAN, Vector, Kvaser, slcan, gs_usb, virtual, …), including CAN FD with a separate data bitrate, and channel detection
- **Trace** view (chronological, buffer size configurable) and **Overview** per identifier (count, cycle time, changed bytes in bold)
- Row selection, copy to clipboard (tab separated) and export of the trace to a log file
- Display filters, e.g. `100-1FF !123 ext rx fd`: hex IDs and ranges, `!` excludes, keywords restrict frame type and direction
- Bus statistics: Rx/Tx rates, estimated bus load, error frames and bus state
- Recording to `.asc`, `.blf`, `.csv`, `.log`, `.trc` and `.db`, and replay of those files with or without the original timing
- Signal decoding with DBC, BUSMASTER DBF, KCD, SYM, ARXML and LIN LDF databases and live **signal plots**
- Conversion between signal database formats, including **DBC ↔ DBF** (File → Convert Signal Database, or `can-db-convert`)
- Transmission of single or periodic frames, timed by the application or by the interface
- **LIN** (through a SocketCAN LIN driver such as [sllin](https://github.com/lin-bus/linux-lin), or the virtual interface): headers and responses in the trace, LDF signal decoding and plots, LIN master schedule tables with editable master signals, header-only transmission
- **UDS diagnostics** over ISO-TP (CAN) and the LIN transport layer: sessions, ECU reset, tester present, read/write data identifiers, security access with a user supplied seed/key algorithm (a Python file defining `compute_key(level, seed)`), DTCs, routines and raw requests
- Passive protocol decoding: ISO-TP (reassembly), UDS, J1939 (PGNs, BAM and RTS/CTS transport), CANopen (NMT, SDO, PDO, EMCY, heartbeat), LIN (including diagnostic transport frames)
- Simulated ECUs and LIN slaves (`can_explorer.core.simulation`) for trying things out without hardware
- Recent connections and log files, preferences and window geometry are remembered

## Usage
- Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install dependencies
  ```bash
  uv sync
  ```
- Run main application
  ```bash
  uv run --env-file .env main <ARGS>
  ```
  Settings can be overridden on the command line (e.g. `--log_level DEBUG`) or with `CANEXPLORER_*` environment variables.
- Convert a signal database (format from the file extension: `.dbc`, `.dbf`, `.kcd`, `.sym`; `.arxml` as input)
  ```bash
  uv run can-db-convert vehicle.dbc vehicle.dbf
  uv run can-db-convert vehicle.dbf vehicle.dbc
  ```

## Development
```bash
uv run pytest            # tests (Qt runs headless, buses are python-can virtual buses)
uv run ruff check .      # lint
uv run black .           # format
QT_QPA_PLATFORM=offscreen uv run python tools/benchmark.py   # pipeline and model throughput
```

### Architecture
- `can_explorer/core`: Qt-independent asyncio core. A `BusSession` pumps messages from a source
  (`LiveSource` for a bus, `FileSource` for replay) into a ring buffer, per-ID statistics, an optional
  recorder and the decoders in `core/protocols` and `core/decoding.py`.
- `can_explorer/gui`: thin Qt adapters (`ConnectionManager`, `CanConnection`, table models) that push
  batched updates to the views about 30 times per second.
- `can_explorer/gui/qt`: the QML user interface.

Qt and asyncio share one event loop through [qasync](https://github.com/CabbageDevelopment/qasync).

### Packaging
```bash
uv run python packaging/build.py --archive                              # PyInstaller (default)
uv run python packaging/build.py --backend nuitka --archive             # compiled with Nuitka
uv run python packaging/build.py --backend nuitka --onefile             # single executable (Linux, Windows)
```
builds a standalone bundle for the current platform in `dist/`. Nuitka compiles the code to C and takes
considerably longer; it needs a C compiler (gcc or clang on Linux, the Xcode command line tools on macOS,
MSVC or a MinGW that Nuitka downloads on Windows). The `installer` workflow builds Linux, Windows and macOS
bundles (Nuitka can be selected when starting it manually), and attaches them to a GitHub release when a
`v*` tag is pushed.

See [CHANGELOG.md](CHANGELOG.md) for the history of changes.


## Roadmap
1. Project Setup
    - [x] Initialize git project  
    - [x] Set basic project structure 
    - [x] Set package manager and install packages
    - [x] Set docker and vscode devcontainer
    - [x] Add parsing and transmission of CAN frame as proof of concept
    - [x] Release 0.1.0
2. Transception of CAN frames.
    - [ ] Implement IsoCan transport and protocol
    - [ ] Implement J1939 transport and protocol
    - [ ] Implement ISOTP transport and protocol
    - [ ] Implement CanOpen transport and protocol
    - [ ] Release 0.2.0
3. Move UI to QML
    - [ ] Add main UI