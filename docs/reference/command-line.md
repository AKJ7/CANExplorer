# Command line

## main

`uv run main` (or the `CANExplorer` executable of a bundle) starts the application. Settings can be given as
options, as `CANEXPLORER_*` environment variables or in a `.env` file in the working directory, in decreasing order
of precedence.

| Option | Environment variable | Default | Description |
|---|---|---|---|
| `--log_level` | `CANEXPLORER_LOG_LEVEL` | `DEBUG` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `--log_format` | `CANEXPLORER_LOG_FORMAT` | `%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)-35s: %(message)s` | Python logging format |
| `--date_format` | `CANEXPLORER_DATE_FORMAT` | `%Y-%m-%d %H:%M:%S` | Date format of log messages |
| `--console_log_file` | `CANEXPLORER_CONSOLE_LOG_FILE` | `stdout` | `stdout` or `stderr` |
| `--production_build` | `CANEXPLORER_PRODUCTION_BUILD` | `False` | Plain (uncoloured) log output |
| `--app_name` | `CANEXPLORER_APP_NAME` | `CANExplorer` | Name shown in the title and the about dialog |

The `.env` file of the repository sets the log level to `INFO`; use `uv run --env-file .env main` to also pass its
Qt debugging variables. `main --help` lists all options.

```bash
uv run main --log_level WARNING
CANEXPLORER_LOG_LEVEL=DEBUG uv run main
```

On Linux, `main --install-desktop-entry` adds CANExplorer with its icon to the desktop's application menu and
`main --uninstall-desktop-entry` removes it, see
[Installation](../getting-started/installation.md#desktop-menu-entry-linux).

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
