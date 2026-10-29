# MCP interface

`can-mcp` exposes a CAN bus to an AI agent over the [Model Context Protocol](https://modelcontextprotocol.io),
so an MCP client (for example Claude Desktop or an agent you build) can inspect the bus — and, when you allow it,
transmit — through a small set of tools.

It opens one bus with python-can, keeps receiving into a trace, and answers the client's tool calls over stdio. It
is read-only unless started with `--allow-send`.

## Installing and running

The server needs the `mcp` extra:

```bash
pip install 'can-explorer[mcp]'          # or, from source:  uv sync --extra mcp
```

Run it against a bus (read-only), optionally with a database so frames are decoded into named signals:

```bash
can-mcp --interface socketcan --channel can0 --bitrate 500000 --database vehicle.dbc
```

Add `--allow-send` to let the agent transmit. See [`can-mcp`](../reference/command-line.md#can-mcp) for all options.

!!! warning "Sending is off by default"
    Without `--allow-send`, the agent can only read the bus; the `send_frame` and `query_obd` tools are not even
    offered. Enable sending only on a bus you are allowed to transmit on, and remember that the agent then controls
    what is sent.

## Connecting a client

Point your MCP client at the command. For a client that launches stdio servers from a config file, the entry is
roughly:

```json
{
  "mcpServers": {
    "canexplorer": {
      "command": "can-mcp",
      "args": ["--interface", "socketcan", "--channel", "can0", "--database", "/path/vehicle.dbc"]
    }
  }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `bus_status` | Bus state, loaded database, frame counts, rates and bus load |
| `list_identifiers` | Every identifier seen, with direction, count, cycle time, last data and database name |
| `get_frames` | Recent frames (optionally of one identifier), decoded into signals |
| `decode_frame` | Decode a payload you supply with the loaded database (sends nothing) |
| `analyze_identifier` | Automatic field analysis of an identifier (checksums, counters, multiplexers, physical values, states, flags) |
| `correlate` | Rank fields by how well they follow a reference — a database signal, a CSV column, or an inline time series — with the fitted factor, offset and lag |
| `promote_to_dbc` | Write the found fields (of one identifier, or all) as signals to a DBC, creating their messages |
| `load_database` | Load a DBC/ARXML/KCD/SYM/DBF for decoding |
| `send_frame` | Send one frame — only with `--allow-send` |
| `query_obd` | Request one OBD-II value (service 01) and return the decoded answer — only with `--allow-send` |

Identifiers are hex strings such as `1A0`. `analyze_identifier`, `correlate` and `promote_to_dbc` are the same
engine as the [Reverse engineering](reverse-engineering.md) view, so an agent can run a full loop over MCP: survey
the bus, analyse an identifier, match a field to a reference (a GPS/CSV log or an OBD-II value it polled), and write
the result to a DBC — then load that DBC and read the signals back decoded. `correlate` and `promote_to_dbc` read
and (for the DBC) write files but do not transmit, so they are available without `--allow-send`; `promote_to_dbc`
extends an existing database at the path rather than overwriting it.

## How it relates to the rest of CANExplorer

The server is a separate command, like [`can-server`](remote-buses.md); it does not need the GUI. It works with any
python-can interface, so it can also sit on top of a [remote bus](remote-buses.md) (`--interface canexplorer_remote`)
shared by `can-server` on another machine.
