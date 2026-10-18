<p align="center"><img src="docs/assets/logo-banner.png" alt="CANExplorer logo" width="360"></p>

<h1 align="center">CANExplorer</h1>

<p align="center">
  CANBus monitoring for the modern man!  
  
  Monitor, analyse, diagnose, simulate and test <b>CAN</b>, <b>CAN FD</b> and <b>LIN</b> buses — on Linux,
  Windows and macOS, with any adapter python-can supports, or without hardware on a virtual bus.
</p>

<p align="center">
  <a href="https://akj7.github.com/canexplorer/">Documentation</a> ·
  <a href="https://akj7.github.com/canexplorer/getting-started/quick-start/">Quick start</a> ·
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
- Signal plots with zoom and pan over the whole buffer, two cursors with values and statistics, several signals per
  chart and CSV export

**Recording and replay**
- ASC, BLF, CSV, candump LOG, PEAK TRC, SQLite, ASAM MF4 and gzip compressed text logs
- Splitting into numbered files by size or time, and triggered recording with conditions such as
  `EngineData.EngineSpeed > 6000`, keeping the time before and after the event
- Replay at 0.25× to 10× or as fast as possible, with pause and jumping to any position
- Replay of a recording onto a real bus, filtered and repeated
- Comparison of two recordings: identifiers only in one of them, changed cycle times and payloads, and signal
  values, aligned by time, by a trigger identifier or by frame sequence

**Signal databases**
- Vector DBC, BUSMASTER DBF, AUTOSAR ARXML (including container PDUs), Kayak KCD, PCAN SYM and LIN LDF
- Database editor for CAN and LIN databases (frames, signals, nodes, encodings, schedule tables) with a bit layout
  view and consistency checks, and conversion between the formats

**Diagnostics and protocols**
- UDS over ISO-TP and the LIN transport layer: sessions, reset, data identifiers, security access with your own
  seed/key algorithm, DTCs, routines; CANdelaStudio CDD descriptions for named and decoded identifiers
- ECU flashing over UDS (RequestDownload/TransferData) from Intel HEX, S-record or binary files, with progress
- J1939: nodes with their address claims, DM1/DM2 trouble codes and lamps, clearing codes, requests and sending
  parameter groups by name with BAM or RTS/CTS
- CANopen master: NMT, SDO access to object dictionaries from EDS/DCF files, PDO mappings with live values,
  emergency messages
- Decoding of ISO-TP, UDS, J1939, CANopen and LIN diagnostic frames in the trace
- LIN master schedule tables

**Simulation and test automation**
- Simulated ECUs written in Python, controlled from QML panels
- ECU test scripts with checks, timing measurements and UDS requests; pass/fail verdicts in the Tests view or
  headless with `can-test`, with HTML and JUnit reports for CI

**Workspace**
- Connections, databases, filters, plots and scripts saved in one workspace file
- Dashboard-style interface with six colour themes

See the [documentation](https://akj7.github.com/canexplorer/) for a guide to every feature.

## Installation

### Standalone bundles

Download the archive for your platform from the [releases](https://github.com/AKJ7/CANExplorer/releases), unpack
it and start `CANExplorer` (`CANExplorer.app` on macOS). No Python installation is needed.

### From source

With [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.10 or newer:

```bash
git clone https://github.com/AKJ7/CANExplorer.git
cd CANExplorer
uv sync                  # Add --extra mf4 for ASAM MF4 log files (installs asammdf, numpy and pandas)
uv run main
```

On Linux, `uv run main --install-desktop-entry` adds CANExplorer with its icon to the application menu.

### Interfaces

Install the driver of your adapter; CANExplorer talks to it through python-can. On Linux most adapters are
SocketCAN interfaces, configured with `ip link`; serial adapters (slcan, python-can's serial protocol) need no
driver. The *virtual* interface works everywhere without hardware. The
[installation guide](https://akj7.github.com/canexplorer/getting-started/installation/) has the details.

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
[quick start](https://akj7.github.com/canexplorer/getting-started/quick-start/) walks through it.

## Command line

| Command | Purpose |
|---|---|
| `uv run main` | The application. Options such as `--log_level DEBUG`, or `CANEXPLORER_*` environment variables |
| `uv run can-test SCRIPT …` | Run ECU test scripts on a bus; exit code and HTML/JUnit reports for CI |
| `uv run can-db-convert IN OUT` | Convert signal databases, e.g. `vehicle.dbc vehicle.dbf`, or one bus with `system.arxml powertrain.dbc --bus Powertrain` |

## Development

```bash
uv run pytest                                  # Tests (headless Qt, python-can virtual buses)
uv run ruff check . && uv run black --check .  # Lint and formatting
uv run --group docs mkdocs serve               # Documentation preview on http://127.0.0.1:8000
uv run python packaging/build.py --archive     # Standalone bundle in dist/ (--backend nuitka to compile)
```

The core (`can_explorer/core`) is Qt-independent asyncio code; `can_explorer/gui` adapts it to the QML interface
in `can_explorer/gui/qt`, and Qt and asyncio share one event loop through
[qasync](https://github.com/CabbageDevelopment/qasync). The
[development guide](https://akj7.github.com/canexplorer/development/) covers the architecture, testing,
packaging and contributing.

## License

MIT, as declared in [pyproject.toml](pyproject.toml).
