# MCP interface

`can-mcp` exposes a CAN bus to an AI agent over the [Model Context Protocol](https://modelcontextprotocol.io),
so an MCP client (for example Claude Desktop or an agent you build) can inspect the bus — and, when you allow it,
transmit — through a small set of tools.

It talks to one bus with python-can, keeps receiving into a trace, and answers the client's tool calls over stdio.
It is read-only unless you grant more with a permission flag, and it exposes the same data as MCP **resources** and
a few guided **prompts** for the common workflows.

## Permission tiers

The server is read-only by default. Each flag grants the tier below it as well, so `--allow-send` enables
everything:

| Flag | Adds | Tools |
|---|---|---|
| *(none)* | Connecting, inspection and reverse engineering (reads files, sends nothing) | `bus_status`, `list_interfaces`, `connect`, `disconnect`, `list_identifiers`, `get_frames`, `decode_frame`, `decode_protocol`, `timing`, `analyze_identifier`, `related_signals`, `capture_reaction`, `correlate`, `promote_to_dbc`, `load_database`, `unload_database` |
| `--allow-diagnostics` | Safe diagnostic **reads** (they put a request on the bus) | `query_obd`, `obd_status`, `obd_dtcs`, `obd_vin`, `obd_supported`, `obd_read`, `uds_read_did`, `uds_read_dtcs` |
| `--allow-control` | **Active** diagnostics (changes ECU state) | `obd_clear`, `uds_reset`, `uds_routine`, `uds_request` |
| `--allow-send` | Arbitrary transmit | `send_frame`, `send_message` |

A tool that is not enabled is not even offered to the agent, so it cannot be called.

## Installing and running

The server needs the `mcp` extra:

```bash
pip install 'can-explorer[mcp]'          # or, from source:  uv sync --extra mcp
```

The simplest form takes no arguments — the agent lists the interfaces and connects to one itself, and loads a
database if it needs one:

```bash
can-mcp
```

Everything else is optional. Give a bus up front if you prefer it opened at start:

```bash
can-mcp -i socketcan -c can0                 # open this bus immediately (read-only)
can-mcp -i socketcan -c can0 -d vehicle.dbc  # …and decode frames into named signals
```

Either way the `connect(interface, channel)` and `disconnect` tools are always available, so the agent can open or
switch buses at any time, and `load_database` / `unload_database` load or clear a database at runtime. (Omitting
`--interface` and `--channel` just means the server starts unconnected; give both, or neither.)

Add a permission flag to allow more — for example `--allow-diagnostics` for OBD-II and UDS reads, or `--allow-send`
for full transmit. See [`can-mcp`](../reference/command-line.md#can-mcp) for all options.

!!! warning "Reads are the default; grant the rest deliberately"
    Without a permission flag the agent can only read the bus. Grant `--allow-diagnostics`, `--allow-control` or
    `--allow-send` only on a bus you are allowed to use that way, and remember the agent then decides what is sent
    — a `--allow-control` or `--allow-send` server can reset ECUs, clear trouble codes or transmit arbitrary
    frames.

## Connecting a client

Point your MCP client at the command. For a client that launches stdio servers from a config file, the simplest
entry is just the command — the agent connects itself:

```json
{
  "mcpServers": {
    "canexplorer": { "command": "can-mcp" }
  }
}
```

To pin a bus (and a database, and permissions), add them as `args`:

```json
{
  "mcpServers": {
    "canexplorer": {
      "command": "can-mcp",
      "args": ["-i", "socketcan", "-c", "can0", "-d", "/path/vehicle.dbc", "--allow-diagnostics"]
    }
  }
}
```

## Tools

**Inspection and reverse engineering** (always available):

| Tool | Purpose |
|---|---|
| `bus_status` | Connection state, loaded database, enabled capabilities, frame counts, rates and bus load |
| `list_interfaces` | The CAN interfaces python-can can detect on this machine |
| `connect` / `disconnect` | Open a bus (interface, channel) and start receiving, or close it |
| `list_identifiers` | Every identifier seen, with direction, count, cycle time, last data and database name |
| `get_frames` | Recent frames (optionally of one identifier), decoded into signals |
| `decode_frame` | Decode a payload you supply with the loaded database (sends nothing) |
| `decode_protocol` | Interpret a payload with a protocol decoder (J1939, NMEA 2000, CANopen, ISO-TP, UDS, LIN, J1587) |
| `timing` | Inter-frame timing per identifier: cycle time, jitter, and periodic vs event-triggered |
| `analyze_identifier` | Automatic field analysis (checksums, counters, multiplexers, physical values, states, flags) |
| `related_signals` | Groups of fields that carry the same signal across the bus |
| `capture_reaction` | Diff a recent action window against the baseline before it to find what changed |
| `correlate` | Rank fields by how well they follow a reference (a database signal, a CSV column, or an inline series) |
| `promote_to_dbc` | Write the found fields as signals to a DBC, creating their messages |
| `load_database` / `unload_database` | Load a DBC/ARXML/KCD/SYM/DBF for decoding, or forget it (back to raw bytes) |

**Diagnostics** (`--allow-diagnostics`; the control tools need `--allow-control`):

| Tool | Purpose |
|---|---|
| `query_obd` | One OBD-II value (service 01) via a single-frame request |
| `obd_status` / `obd_vin` | MIL state and DTC count; the VIN |
| `obd_dtcs` | Stored (service 03) or pending (service 07) trouble codes, decoded to P/C/B/U |
| `obd_supported` / `obd_read` | The supported PIDs; read and decode live values |
| `uds_read_did` / `uds_read_dtcs` | Read a UDS data identifier (0x22) or trouble codes (0x19) |
| `obd_clear` | Clear trouble codes and the MIL (service 04) — `--allow-control` |
| `uds_reset` / `uds_routine` / `uds_request` | ECU reset, routine control, or a raw UDS request — `--allow-control` |

**Transmit** (`--allow-send`): `send_frame` (a raw frame) and `send_message` (a database message by name, from its
signal values).

Identifiers are hex strings such as `1A0`. The reverse-engineering tools are the same engine as the
[Reverse engineering](reverse-engineering.md) view, so an agent can run a full loop over MCP: survey the bus with
`timing` and `list_identifiers`, `analyze_identifier`, match a field to a reference with `correlate` or group them
with `related_signals`, and `promote_to_dbc` — then `load_database` and read the signals back decoded. The
diagnostics tools let it read and explain a vehicle's trouble codes over OBD-II or UDS.

## Resources and prompts

The server also exposes the bus as read-only MCP **resources**, so a client can browse them like documents:
`can://status`, `can://identifiers`, `can://frames` and `can://database`.

And it ships guided **prompts** for the common workflows: `reverse_engineer_identifier`, `diagnose_trouble_codes`
and `find_signal`.

## In-app server (from the GUI)

The GUI can also host the MCP server, exposing a **connection it already has open** — so the agent sees the same
live trace you do. This is the way to attach an agent to a bus the GUI is using: most USB/serial adapters are
single-access, so a separate `can-mcp` process cannot open the same channel at the same time.

On a connection's **Trace** toolbar, the **Agent…** button opens a dialog where you choose the access tier
(read-only by default) and **Start** the server. It listens on `127.0.0.1` only and shows a **URL** and a **token**
to paste into your MCP client, which connects with the **streamable-HTTP** transport and the token as an
`Authorization: Bearer <token>` header (or appended as `?token=<token>`). The same permission tiers apply; the
agent cannot open or close the bus (you manage that from the GUI). It needs the `mcp` extra, like the command.

!!! warning "A listening server is a bigger surface"
    The in-app server is off by default and loopback-only, and the token gates it — but an agent on a `control` or
    `send` tier can reset ECUs, clear codes or transmit on your live bus. Start it deliberately, and prefer
    read-only unless you need more.

## How it relates to the rest of CANExplorer

The `can-mcp` command is separate, like [`can-server`](remote-buses.md); it does not need the GUI. It works with any
python-can interface, so it can also sit on top of a [remote bus](remote-buses.md) (`--interface canexplorer_remote`)
shared by `can-server` on another machine — another way to share one bus between the GUI and an agent.
