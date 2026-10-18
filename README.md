# CANExplorer
A CANbus-Analyse Tool for all platforms.

## Features
- Live monitoring of any [python-can](https://python-can.readthedocs.io/) interface (SocketCAN, PCAN, Vector, Kvaser, slcan, gs_usb, virtual, …)
- **Trace** view (chronological, bounded to 100 000 rows) and **Overview** per identifier (count, cycle time, changed bytes in bold)
- Display filters, e.g. `100-1FF !123 ext rx fd`: hex IDs and ranges, `!` excludes, keywords restrict frame type and direction
- Recording to `.asc`, `.blf`, `.csv`, `.log`, `.trc` and `.db`, and replay of those files with or without the original timing
- Signal decoding with DBC/KCD/SYM/ARXML databases
- Transmission of single or periodic frames
- Passive protocol decoding: ISO-TP (reassembly), J1939 (PGNs, BAM and RTS/CTS transport), CANopen (NMT, SDO, PDO, EMCY, heartbeat)

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

## Development
```bash
uv run pytest            # tests (Qt runs headless, buses are python-can virtual buses)
uv run ruff check .      # lint
uv run black .           # format
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
uv run python packaging/build.py --archive
```
builds a standalone bundle for the current platform in `dist/`. The `installer` workflow builds Linux,
Windows and macOS bundles, and attaches them to a GitHub release when a `v*` tag is pushed.


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