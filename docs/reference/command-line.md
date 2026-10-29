# Command line

## canexplorer

`uv run canexplorer` (or the `CANExplorer` executable of a bundle) starts the application. Settings can be given as
options, as `CANEXPLORER_*` environment variables or in a `.env` file in the working directory, in decreasing order
of precedence.

| Option | Environment variable | Default | Description |
|---|---|---|---|
| `--log_level` | `CANEXPLORER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `--log_format` | `CANEXPLORER_LOG_FORMAT` | `%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)-35s: %(message)s` | Python logging format |
| `--date_format` | `CANEXPLORER_DATE_FORMAT` | `%Y-%m-%d %H:%M:%S` | Date format of log messages |
| `--console_log_file` | `CANEXPLORER_CONSOLE_LOG_FILE` | `stdout` | `stdout` or `stderr` |
| `--production_build` | `CANEXPLORER_PRODUCTION_BUILD` | `False` | Plain (uncoloured) log output |
| `--app_name` | `CANEXPLORER_APP_NAME` | `CANExplorer` | Name shown in the title and the about dialog |

Use `--log_level DEBUG` to diagnose problems. The `.env` file of the repository sets development settings; use
`uv run --env-file .env canexplorer` to also pass its Qt debugging variables. `canexplorer --help` lists all options.

```bash
uv run canexplorer --log_level WARNING
CANEXPLORER_LOG_LEVEL=DEBUG uv run canexplorer
```

On Linux, `canexplorer --install-desktop-entry` adds CANExplorer with its icon to the desktop's application menu and
`canexplorer --uninstall-desktop-entry` removes it, see
[Installation](../getting-started/installation.md#desktop-menu-entry-linux).

## can-test

Runs [test scripts](../user-guide/test-automation.md) on a bus without the user interface.

```text
can-test [-h] [--interface INTERFACE] [--channel CHANNEL] [--bitrate BITRATE] [--fd] [--data-bitrate DATA_BITRATE]
         [--database DATABASE] [--simulation SIMULATION] [-k FILTER] [-r REQUIREMENT] [--report REPORT]
         [--junit JUNIT] [--markdown MARKDOWN] [--list] [--title TITLE] scripts [scripts ...]
```

| Argument | Description |
|---|---|
| `scripts` | Test scripts (Python files with `TestModule` classes) |
| `--interface` | python-can interface, e.g. `socketcan`, `pcan`, `vector`; default `virtual` |
| `--channel` | Channel, e.g. `can0` or `PCAN_USBBUS1`; default `can-test` |
| `--bitrate`, `--fd`, `--data-bitrate` | Bus timing; default 500000 bit/s, classic CAN |
| `--database` | Signal database for messages and signals by name |
| `--simulation` | Simulation script to run on the same bus (repeatable), e.g. to test without hardware |
| `-k` | Run only the test cases whose `Module.case` contains this text |
| `-r`, `--requirement` | Run only the test cases verifying this requirement (repeatable) |
| `--report` | Write an HTML report |
| `--junit` | Write JUnit XML results |
| `--markdown` | Append a Markdown summary to this file, e.g. `$GITHUB_STEP_SUMMARY` |
| `--list` | List the test cases (with their requirements) and exit |
| `--title` | Title of the reports |

Exit code: 0 when all test cases passed (or were skipped), 1 when any failed or had an error, 2 when the scripts,
the database or the bus could not be loaded.

## can-server

Shares CAN interfaces of this computer with CANExplorer on other computers, see [Remote buses](../user-guide/remote-buses.md).

```text
can-server --bus NAME=INTERFACE:CHANNEL[:BITRATE[:DATA_BITRATE]] [--bus ...] [--host HOST] [--port PORT]
           [--token TOKEN] [-v]
```

| Argument | Description |
|---|---|
| `--bus` | A bus to share, e.g. `vehicle=socketcan:can0:500000` (repeatable); a data bitrate enables CAN FD |
| `--host` | Address to listen on; default `0.0.0.0` |
| `--port` | TCP port; default 8765 |
| `--token` | Token clients must present; also from `CAN_SERVER_TOKEN` |
| `-v`, `--verbose` | Log every client connection and bus event |

## can-mcp

Exposes one bus to an AI agent over the [Model Context Protocol](../user-guide/mcp.md) (stdio). Needs the `mcp`
extra. Read-only unless `--allow-send` is given.

```text
can-mcp --interface INTERFACE --channel CHANNEL [--bitrate BITRATE] [--fd] [--data-bitrate DATA_BITRATE]
        [--database FILE] [--protocol {Raw,ISO-TP,UDS,J1939,CANopen,LIN}] [--allow-send]
```

| Argument | Description |
|---|---|
| `-i`, `--interface` | python-can interface, e.g. `socketcan`, `pcan`, `virtual`, `gvret` (required) |
| `-c`, `--channel` | Channel, e.g. `can0`, `PCAN_USBBUS1`, `COM7` (required) |
| `-b`, `--bitrate` | Nominal bitrate; default 500000 |
| `--fd`, `--data-bitrate` | CAN FD; a data bitrate enables it |
| `-d`, `--database` | Signal database (DBC, ARXML, KCD, SYM, DBF) for decoding |
| `--protocol` | Protocol decoder for the frames; default `Raw` |
| `--allow-send` | Allow the agent to transmit (`send_frame`, `query_obd`) |

## can-db-convert

Converts signal databases; the formats follow the file extensions.

```text
can-db-convert [-h] [--protocol {CAN,J1939}] [--bus BUS] input output
```

| Argument | Description |
|---|---|
| `input` | Database to read: `.dbc`, `.dbf`, `.kcd`, `.sym`, `.arxml` |
| `output` | Database to write: `.dbc`, `.dbf`, `.kcd`, `.sym` |
| `--protocol` | Protocol of a DBF output. Default: `J1939` if the DBC declares the attribute `ProtocolType "J1939"`, else `CAN` |
| `--bus` | Convert only this bus of a database describing several (ARXML cluster, KCD bus) |

The summary lists the number of messages and signals and everything the output format cannot hold. The exit status
is 0 on success and 1 on errors.

```bash
uv run can-db-convert vehicle.dbc vehicle.dbf
uv run can-db-convert vehicle.dbf vehicle.dbc
uv run can-db-convert system.arxml powertrain.kcd --bus Powertrain
```
