# MCP interface

CANExplorer speaks the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), so an AI agent such as
Claude Desktop, Claude Code or an agent you build can work on a CAN bus through a set of **tools**. You ask in plain
language, for example *"which identifiers are on the bus, and what is in 0x123?"*, and the agent calls the tools it
needs and explains the answers.

What an agent can do through it:

- **Look at the bus**: connection state, bus load, every identifier with its cycle time, recent frames.
- **Decode**: frames into signals with a database, or payloads with a protocol decoder (UDS, J1939, CANopen…).
- **Reverse-engineer**: find checksums, counters and physical values, test a guess, and write the result to a DBC.
- **Diagnose** *(when you allow it)*: read OBD-II and UDS trouble codes and live values, XCP measurements.
- **Act** *(when you allow it)*: clear codes, reset ECUs, write calibrations, transmit frames.

Everything is **read-only by default**. Diagnostics, ECU control and transmitting each need a
[permission tier](#permission-tiers) that you grant.

## Quick start

There are two ways to give an agent a bus. Both offer the same tools.

=== "Live bus in the GUI (in-app server)"

    Use this when the GUI already has the bus open: the agent sees the same live trace you do.

    1. Open the connection and go to its **Trace** page.
    2. Press **Agent…** in the toolbar, keep the tier on **read-only**, and press **Start**.
    3. Copy the **URL** and **token** the dialog shows into your client
       ([Claude Code](#claude-code), [Claude Desktop and others](#claude-desktop-and-other-json-configs)).
    4. Ask the agent: *"What is on the bus?"*

=== "Standalone (`can-mcp` command)"

    Use this without the GUI, for example on a test bench or a headless machine.

    1. Register the command with your client, for example in Claude Code:

        ```bash
        claude mcp add canexplorer -- can-mcp
        ```

    2. Ask the agent: *"List the CAN interfaces and connect to can0 at 500 kbit/s."*

    The client starts `can-mcp` itself. Without arguments the agent picks and opens the bus; see
    [the `can-mcp` command](#the-can-mcp-command) to open one up front.

### Which one should I use?

| | In-app server | `can-mcp` |
|---|---|---|
| Runs | Inside the GUI, for one open connection | As its own process, started by the MCP client |
| Bus | The connection the GUI already has open | Opened by `can-mcp` (at start, or by the agent with `connect`) |
| Transport | Streamable HTTP on `127.0.0.1`, with a token | stdio |
| Opening/closing the bus | You, in the GUI | The agent (`connect`, `disconnect`) |
| Best for | Working on a bus together with the agent while you watch | Scripts, benches, machines without a display |

Most USB and serial adapters can only be opened by one program at a time. To share a bus the GUI already has open,
use the in-app server.

## In-app server

![The AI agent (MCP) dialog started read-only, with the server URL and token](../assets/screenshots/mcp.png)

The **Agent…** button on a connection's **Trace** toolbar opens this dialog. Choose the tier and press **Start**.
The server then:

- listens on `127.0.0.1` only, on a free port, at a URL like `http://127.0.0.1:57205/mcp`;
- requires the **token** on every request, either as an `Authorization: Bearer <token>` header or as `?token=<token>`
  at the end of the URL. A request without it is refused (HTTP 401);
- shows **● Agent** on the toolbar while it runs. Press it again to stop the server.

The port and token change every time you start it, so copy them again after a restart. The agent cannot open or
close the bus: you do that in the GUI.

To attach Claude Code, put the port and token from the dialog into this command:

```bash
claude mcp add --transport http canexplorer-live http://127.0.0.1:PORT/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

After a restart of the server, remove the old entry first (`claude mcp remove canexplorer-live`) and add it again
with the new port and token. For other clients, see [connecting a client](#connecting-a-client).

!!! warning "Start it deliberately"
    The server is off until you start it, and loopback-only. On the `control` or `send` tier, though, the agent can
    reset ECUs, clear codes or transmit on your live bus. Prefer read-only unless you need more.

## The `can-mcp` command

`can-mcp` runs the same server as a separate process and talks to the client over stdio. It is installed with
CANExplorer. All its arguments are optional:

```bash
can-mcp                                                   # the agent lists interfaces and connects itself
can-mcp -i socketcan -c can0                              # open can0 at start (read-only)
can-mcp -i socketcan -c can0 -d vehicle.dbc               # ...and decode frames into named signals
can-mcp -i pcan -c PCAN_USBBUS1 -b 250000                 # another adapter and bitrate
can-mcp -i socketcan -c can0 --fd --data-bitrate 2000000  # CAN FD
can-mcp -i socketcan -c can0 --allow-diagnostics          # also allow OBD-II/UDS/XCP reads
can-mcp -d vehicle.dbc                                    # a database, no bus: decode payloads offline
```

Give both `--interface` and `--channel`, or neither. Even with a bus given, the agent can switch buses with
`connect` and `disconnect`, and load or forget a database with `load_database` and `unload_database`. All options
are in the [command-line reference](../reference/command-line.md#can-mcp).

!!! warning "Grant more only where you may"
    Grant `--allow-diagnostics`, `--allow-control` or `--allow-send` only on a bus you are allowed to use that way.
    The agent then decides what is sent: with `--allow-control` or `--allow-send` it can reset ECUs, clear trouble
    codes or transmit arbitrary frames.

## Connecting a client

### Claude Code

```bash
# can-mcp over stdio, read-only, the agent picks the bus
claude mcp add canexplorer -- can-mcp

# can-mcp with a bus, a database and diagnostics
claude mcp add canexplorer -- can-mcp -i socketcan -c can0 -d /path/vehicle.dbc --allow-diagnostics

# the in-app server (URL and token from the Agent… dialog)
claude mcp add --transport http canexplorer-live http://127.0.0.1:57205/mcp \
  --header "Authorization: Bearer WWPkEFJO7GXuAQJR1tCarQ"
```

### Claude Desktop and other JSON configs

Most clients read a JSON file with an `mcpServers` section (for Claude Desktop, `claude_desktop_config.json`).

=== "can-mcp"

    ```json
    {
      "mcpServers": {
        "canexplorer": { "command": "can-mcp" }
      }
    }
    ```

=== "can-mcp with a bus"

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

=== "In-app server"

    ```json
    {
      "mcpServers": {
        "canexplorer-live": {
          "url": "http://127.0.0.1:57205/mcp",
          "headers": { "Authorization": "Bearer WWPkEFJO7GXuAQJR1tCarQ" }
        }
      }
    }
    ```

    A client that cannot set headers can use the URL with the token appended instead:
    `http://127.0.0.1:57205/mcp?token=WWPkEFJO7GXuAQJR1tCarQ`.

If the client reports that it cannot find `can-mcp`, give the full path to the command (`which can-mcp` on Linux and
macOS, `where can-mcp` on Windows). This is common when CANExplorer is installed in a virtual environment.

## Example sessions

What an agent typically does for a request: the tools it calls, with their arguments, and the answers it gets back.
The outputs below are real, captured from 40 seconds of a simulated vehicle bus and a simulated OBD-II ECU (shortened
where marked `…`).

### Survey an unknown bus

> *"What is on this bus?"*

`bus_status` gives the overview (shortened: it also reports `rx_per_s`, `tx_per_s` and `bus_load_percent`):

```json
{
  "connected": true,
  "database": null,
  "capabilities": { "diagnostics": true, "control": false, "send": false },
  "description": "virtual:bench @ 500000 bit/s",
  "frames": 6800,
  "identifiers": 3,
  "error_frames": 0
}
```

`list_identifiers` gives one row per identifier, with its cycle time and latest data:

```json
[
  { "id": "123", "direction": "rx", "count": 4000, "cycle_ms": 10.0, "dlc": 8, "data": "C9 FF C4 37 4B A0 7B 00" },
  { "id": "500", "direction": "rx", "count": 2000, "cycle_ms": 20.0, "dlc": 8, "data": "0D 4F 1F 05 01 00 00 00" },
  { "id": "600", "direction": "rx", "count": 800,  "cycle_ms": 50.0, "dlc": 8, "data": "01 78 3D EE DA 65 00 00" }
]
```

`timing` with `{"identifier": "123"}` tells periodic from event-triggered traffic:

```json
{ "id": "123", "count": 4000, "cycle_ms": 10.0, "min_ms": 10.0, "max_ms": 10.0, "jitter_ms": 0.0, "pattern": "periodic" }
```

### Reverse-engineer an identifier

> *"Work out what is in 0x123 and save it as a DBC."*

1. **`analyze_identifier`** `{"identifier": "123"}` finds the fields:

    ```json
    {
      "id": "123",
      "frames": 4000,
      "fields": [
        { "field": "123[0|8@1+]",   "kind": "checksum", "confidence": 1.0,  "details": "CRC-8 SAE J1850 (AUTOSAR), constant 0x0A" },
        { "field": "123[8|4@1+]",   "kind": "counter",  "confidence": 1.0,  "details": "counter, step +1, 0–15" },
        { "field": "123[12|4@1+]",  "kind": "enum",     "confidence": 0.5,  "details": "3 values, rarely changing" },
        { "field": "123[16|16@1+]", "kind": "physical", "confidence": 0.99, "details": "changes smoothly, 1629…63903" },
        { "field": "123[39|12@0-]", "kind": "physical", "confidence": 0.99, "details": "changes smoothly, -1948…1947 (signed)" },
        { "field": "123[56|1@1+]",  "kind": "flag",     "confidence": 0.6,  "details": "changes in 0.5% of the frames" }
      ]
    }
    ```

    A field is written `ID[start|length@order sign]`, as in a DBC: `@1` is little-endian (Intel), `@0` big-endian
    (Motorola), `+` unsigned and `-` signed.

2. **`extract_field`** tests a guess, here that bits 16–31 are an engine speed at 0.25 rpm per bit:

    ```json
    // extract_field {"identifier": "123", "start": 16, "length": 16, "scale": 0.25, "max_samples": 5}
    {
      "field": "123[16|16@1+]",
      "min": 407.25,
      "max": 15975.75,
      "changes": 3986,
      "series": [
        { "t": 0.0, "value": 3588.25 },
        { "t": 10.0, "value": 12796.5 },
        { "t": 20.0, "value": 3587.25 },
        { "t": 29.99, "value": 12815.75 },
        { "t": 39.99, "value": 3569.0 }
      ]
    }
    ```

3. **`promote_to_dbc`** writes the confirmed field. An existing file is extended, not overwritten:

    ```json
    // promote_to_dbc {"path": "found.dbc", "fields": [{"identifier": "123", "start": 16, "length": 16,
    //                 "scale": 0.25, "name": "EngineSpeed", "unit": "rpm"}]}
    { "path": "found.dbc", "signals": 1, "messages": 1, "skipped": 0, "warnings": [] }
    ```

    Without `fields`, it writes everything `analyze_identifier` found (use `min_confidence` to leave out weak
    guesses).

4. **`load_database`** `{"path": "found.dbc"}`. From then on, `get_frames` returns the new signal by name.

The `reverse_engineer_identifier` [prompt](#prompts) walks an agent through these steps.

### Find what reacts to an action

> *"I'll press the brake pedal a few times. Find the signal."*

1. The agent calls `mark` `{"label": "brake"}` each time you press the pedal (it asks you to tell it when).
2. `reaction_at_markers` lists the fields that change around the marks, most specific first, with how many marks
   each one hits compared with how often it changes anyway.

For a single action, use `capture_reaction` instead: keep still for a few seconds, act, wait, then call it. It
compares the action window with the quiet one before it. If you have a reference (a logged speed, a CSV column),
`correlate` ranks the fields that follow it and fits their factor and offset.

### Read trouble codes *(diagnostics tier)*

> *"Is the check-engine light on? Why?"*

```json
// obd_status {}
{ "mil_on": true, "dtc_count": 2 }

// obd_dtcs {}
{ "pending": false, "dtcs": ["P0133", "P0420"] }

// obd_read {"pids": [12, 13]}
{
  "values": [
    { "pid": 12, "name": "Engine speed", "value": 2500.0, "unit": "rpm" },
    { "pid": 13, "name": "Vehicle speed", "value": 60.0, "unit": "km/h" }
  ]
}
```

The agent explains the codes from its own knowledge and the live values. Clearing them (`obd_clear`) needs the
`control` tier.

### Decode without a bus

`decode_frame` and `decode_protocol` send nothing, so they also work offline (for example `can-mcp -d vehicle.dbc`
with no bus):

```json
// decode_frame {"identifier": "100", "data": "A0 0F 5A 01 00 00 00 00"}
{
  "id": "100",
  "name": "EngineData",
  "description": "EngineData: EngineSpeed=1000 rpm, CoolantTemp=50 degC, GearPosition=First",
  "signals": { "EngineSpeed": 1000.0, "CoolantTemp": 50, "GearPosition": "First" }
}

// decode_protocol {"protocol": "UDS", "identifier": "7E8", "data": "03 7F 22 31"}
{ "id": "7E8", "protocol": "UDS",
  "description": "ISO-TP SF (3 B): 7F 22 31 | UDS Negative response to ReadDataByIdentifier: requestOutOfRange" }

// decode_protocol {"protocol": "J1939", "identifier": "18FEF100", "data": "FF 00 3C 00 00 00 00 FF", "extended": true}
{ "id": "18FEF100", "protocol": "J1939", "description": "PGN 0x0FEF1 CCVS1 SA=0x00 DA=0xFF P6" }
```

## Scripting

You can call the tools from your own code with any MCP client library. These examples use the official
[`mcp`](https://pypi.org/project/mcp/) Python SDK (version 2), which CANExplorer already depends on.

Every tool answers with JSON. With this SDK, `result.structured_content['result']` holds the whole answer as Python
data (a dict, or a list for tools such as `list_identifiers`). `result.content` is the same answer as text parts,
one part per list item. This small helper is used in the examples below:

```python
def answer(result):
    """A tool's answer as Python data; raises if the call failed (e.g. a tool the tier does not allow)."""
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    return result.structured_content['result']
```

=== "In-app server (HTTP)"

    ```python
    import asyncio

    from mcp import Client

    URL = 'http://127.0.0.1:57205/mcp'  # from the Agent… dialog
    TOKEN = 'WWPkEFJO7GXuAQJR1tCarQ'  # from the Agent… dialog


    def answer(result):
        if result.is_error:
            raise RuntimeError(result.content[0].text)
        return result.structured_content['result']


    async def main():
        async with Client(f'{URL}?token={TOKEN}') as client:
            status = answer(await client.call_tool('bus_status', {}))
            print(f'{status["description"]}: {status["identifiers"]} identifiers, {status["bus_load_percent"]} % load')

            for row in answer(await client.call_tool('list_identifiers', {})):
                print(f'{row["id"]:>8}  every {row["cycle_ms"]} ms  {row["data"]}')

            analysis = answer(await client.call_tool('analyze_identifier', {'identifier': '123'}))
            for field in analysis['fields']:
                print(f'{field["field"]:<16} {field["kind"]:<9} {field["details"]}')


    asyncio.run(main())
    ```

    To send the token as a header instead of in the URL, pass the SDK an HTTP client that carries it:

    ```python
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(headers={'Authorization': f'Bearer {TOKEN}'})
    async with Client(streamable_http_client(URL, http_client=http)) as client:
        ...
    ```

=== "can-mcp (stdio)"

    The client starts `can-mcp` itself, here with a database and diagnostics allowed:

    ```python
    import asyncio

    from mcp import Client, StdioServerParameters


    def answer(result):
        if result.is_error:
            raise RuntimeError(result.content[0].text)
        return result.structured_content['result']


    async def main():
        server = StdioServerParameters(command='can-mcp', args=['-d', 'vehicle.dbc', '--allow-diagnostics'])
        async with Client(server) as client:
            answer(await client.call_tool('connect', {'interface': 'socketcan', 'channel': 'can0'}))

            codes = answer(await client.call_tool('obd_dtcs', {}))
            print('Stored trouble codes:', ', '.join(codes['dtcs']) or 'none')

            frames = answer(await client.call_tool('get_frames', {'identifier': '100', 'limit': 5}))
            for frame in frames:
                print(frame['t'], frame['data'], frame['info'])


    asyncio.run(main())
    ```

Resources and prompts are read the same way:

```python
status = await client.read_resource('can://status')
print(status.contents[0].text)  # JSON text

prompts = await client.list_prompts()
print(
    [prompt.name for prompt in prompts.prompts]
)  # ['reverse_engineer_identifier', 'diagnose_trouble_codes', 'find_signal']
```

## Permission tiers

Each tier includes the ones above it, so `send` allows everything.

| `can-mcp` flag | Dialog tier | Adds | Examples |
|---|---|---|---|
| *(none)* | `read-only` | Looking, decoding, reverse engineering, reading files. Sends nothing. | `bus_status`, `analyze_identifier`, `promote_to_dbc` |
| `--allow-diagnostics` | `diagnostics` | Diagnostic **reads**. These put requests on the bus but change nothing. | `obd_dtcs`, `uds_read_did`, `xcp_measure` |
| `--allow-control` | `control` | **Active** diagnostics that change ECU state. | `obd_clear`, `uds_reset`, `xcp_write` |
| `--allow-send` | `send` | Arbitrary transmit. | `send_frame`, `send_message`, `j1939_send` |

The agent is only offered the tools its tier allows. Calling another tool fails with `Unknown tool: <name>`. Each
tool's tier is listed in the [tool reference](#tool-reference) below.

## Tool reference

Identifiers are hex strings such as `"1A0"` or `"18FEF100"`; add `"extended": true` for a 29-bit identifier. Payloads
are hex bytes such as `"A0 0F 5A 03"`. Arguments in the tables are the required ones and the most useful optional
ones; the agent sees the full list.

### Bus and traffic *(read-only)*

| Tool | Main arguments | What it does |
|---|---|---|
| `bus_status` | — | Connection state, database, tier, frame counts, rates and bus load |
| `list_interfaces` | — | The CAN interfaces python-can detects on this machine |
| `connect` / `disconnect` | `interface`, `channel`, `bitrate`, `fd`, `data_bitrate` | Open a bus (closing the current one), or close it. `can-mcp` only |
| `list_identifiers` | `limit` | Every identifier seen: direction, count, cycle time, latest data, database name |
| `get_frames` | `identifier`, `limit` (50), `decode` | Recent frames, newest last, decoded into signals |
| `timing` | `identifier`, `series` | Cycle time, jitter, and periodic vs event-triggered; `series` adds the gaps over time |

### Decoding and databases *(read-only)*

| Tool | Main arguments | What it does |
|---|---|---|
| `decode_frame` | `identifier`, `data` | Decode a payload with the loaded database |
| `decode_protocol` | `protocol`, `identifier`, `data` | Decode with a protocol decoder, no database: J1939, J1939-22, NMEA 2000, CANopen, ISO-TP, UDS, KWP2000, their *(extended)* variants, K-Line, LIN, J1587, NM, SecOC, XCP, CCP |
| `load_database` / `unload_database` | `path` | Load a DBC, ARXML, KCD, SYM or DBF, or go back to raw bytes |
| `secoc_status` | — | SecOC secured PDUs of the loaded ARXML and their live checks (MAC, freshness). Never shows a key. `can-mcp` unlocks the key store with `CANEXPLORER_KEYSTORE_PASSWORD` |

### Reverse engineering *(read-only)*

These use the same engine as the [Reverse engineering](reverse-engineering.md) view.

| Tool | Main arguments | What it does |
|---|---|---|
| `analyze_identifier` | `identifier`, `include_constant` | Find checksums, counters, multiplexers, physical values, states, flags, SecOC freshness and MACs |
| `bit_activity` | `identifier` | How often each bit is set and flips, and a per-byte summary |
| `extract_field` | `identifier`, `start`, `length`, `byte_order`, `signed`, `scale`, `offset` | Decode a guessed bit range over time, without a database |
| `related_signals` | `top`, `min_correlation` | Groups of fields carrying the same signal across the bus |
| `capture_reaction` | `baseline_seconds`, `action_seconds` | What changed during one action, compared with the quiet time before it |
| `mark` / `clear_markers` | `label` | Mark the moments of a repeated action |
| `reaction_at_markers` | `before`, `after`, `top` | The fields that react at the marks |
| `correlate` | `signal` *or* `csv_path` + `column` *or* `times` + `values` | Rank fields by how well they follow a reference, with fitted factor, offset and lag |
| `promote_to_dbc` | `path`, `identifier` *or* `fields`, `min_confidence` | Write found fields, or the specs you give, to a DBC |

### FlexRay and XCP files *(read-only)*

| Tool | Main arguments | What it does |
|---|---|---|
| `flexray_load_cluster` | `path`, `cluster` | Load a [FlexRay](flexray.md) cluster (FIBEX or ARXML) |
| `flexray_read_log` | `path`, `slot`, `channel`, `limit` | Frames and events of a pcap/pcapng, BLF or ASC file, decoded into PDUs and signals |
| `flexray_analyze_log` | `path` | Check a log against the cluster's schedule, and the bus health |
| `flexray_decode` | `slot`, `cycle`, `data`, `channel` | Decode one FlexRay payload with the cluster |
| `xcp_list_variables` | `a2l`, `pattern`, `kind` | Measurements, characteristics and axes of an [A2L file](xcp.md#a2l-files) |

### Diagnostics *(diagnostics tier)*

OBD-II tools default to `request="7DF"` (functional) and `response="7E8"`; UDS tools to `7E0`/`7E8`. For ISO-TP
extended or mixed addressing, put the address byte after a colon, as in the
[Diagnostics view](diagnostics.md#addressing): `"request": "6F1:12", "response": "612:F1"`.

| Tool | Main arguments | What it does |
|---|---|---|
| `obd_status` / `obd_vin` | — | Check-engine light (MIL) and DTC count; the VIN |
| `obd_dtcs` | `pending` | Stored (service 03) or pending (07) trouble codes |
| `obd_supported` / `obd_read` | `pids` | The supported PIDs; read and decode live values |
| `query_obd` | `pid` | One OBD-II value with a single-frame request |
| `uds_read_did` | `did` | Read a data identifier (0x22) |
| `uds_read_dtcs` | `status_mask` | Read trouble codes (0x19) |
| `uds_dtc_details` | `dtc` | A DTC's snapshot (freeze frame) and extended data. Records of unknown length are returned raw |
| `uds_find_ecus` | `extended` | The ECUs answering a functional TesterPresent |
| `j1939_request` | `pgn`, `destination` (`FF` = all) | Request a parameter group and decode the answers (address claims, DM1/DM2, identification text, acknowledgments). Requesting DM3 or DM11 clears codes and needs `control` |
| `xcp_connect` / `xcp_disconnect` / `xcp_status` | `protocol`, `transport`, `request`, `response`, `a2l`, `key` | Open an [XCP](xcp.md) (CAN, CAN FD, UDP, TCP) or CCP session for the other `xcp_*` tools |
| `xcp_read` / `xcp_read_memory` | `names` / `address`, `size` | A2L variables in physical units, or raw memory |
| `xcp_measure` | `names`, `duration` | Measure for up to 30 s (DAQ, or polling): samples, min, max, mean, last |

### Control *(control tier)*

| Tool | Main arguments | What it does |
|---|---|---|
| `obd_clear` | — | Clear trouble codes and the MIL (service 04) |
| `uds_reset` | `reset_type` | ECU reset (0x11) |
| `uds_routine` | `control_type`, `routine`, `data` | Routine control (0x31) |
| `uds_request` | `payload` | Any raw UDS request |
| `uds_secured_request` | `payload`, `key`, `sign`, `encrypt` | A request inside a [SecuredDataTransmission](diagnostics.md#secured-data-transmission-0x84) (0x84) envelope; returns the unwrapped response and whether its signature verified |
| `xcp_write` / `xcp_write_memory` | `name`, `value` / `address`, `data` | Write a calibration value (checked against its A2L limits and read back), or raw memory |

### Transmit *(send tier)*

| Tool | Main arguments | What it does |
|---|---|---|
| `send_frame` | `identifier`, `data`, `extended`, `fd` | Send one raw frame |
| `send_message` | `name`, `signals` | Send a database message from signal values, e.g. `{"name": "EngineData", "signals": {"EngineSpeed": 1000}}` |
| `j1939_send` | `pgn`, `data`, `destination`, `priority` | Send a J1939 parameter group (one frame, BAM, RTS/CTS, or ETP above 1785 bytes) |

## Resources and prompts

### Resources

Read-only JSON documents a client can browse or read directly:

| URI | Holds |
|---|---|
| `can://status` | The same as `bus_status` |
| `can://identifiers` | The same as `list_identifiers` |
| `can://frames` | The 100 most recent frames, decoded |
| `can://database` | The loaded database's path, messages, identifiers and signal names |

### Prompts

Ready-made instructions for common jobs. In Claude Code they appear as slash commands, for example
`/mcp__canexplorer__find_signal`.

| Prompt | Argument | Walks the agent through |
|---|---|---|
| `reverse_engineer_identifier` | `identifier` | Timing, automatic analysis, testing the meaning, writing a DBC |
| `diagnose_trouble_codes` | — | Reading and explaining the OBD-II trouble codes with live values (needs `diagnostics`) |
| `find_signal` | `description` | Finding the field that carries a described signal, e.g. *"the steering angle"* |

Every tool, resource and prompt also has a readable **title** and a **description** the agent uses to choose it. The
descriptions of the diagnostics, control and send tools state the tier they need and what they do to the bus or ECU.

## Troubleshooting

`Unknown tool: obd_dtcs` (or another tool)
:   The tier does not allow that tool. Restart `can-mcp` with the flag it needs, or stop the in-app server and start
    it on a higher tier. See [permission tiers](#permission-tiers).

HTTP 401, *Unauthorized: missing or wrong token*
:   The token is missing or out of date. It changes every time the server starts; copy it again from the
    **Agent…** dialog.

The client rejects the `Authorization` header
:   Check the header name in your config is exactly `Authorization`, with no spaces around it, and its value is
    `Bearer <token>`.

The client cannot connect to the in-app server
:   The server only runs while **● Agent** shows on the Trace toolbar, and only accepts connections from the same
    computer. The port changes on every start.

`can-mcp` cannot open the adapter
:   Another program (often the GUI) has it open. Use the in-app server instead, or close the other program.

The client cannot find `can-mcp`
:   Use the command's full path in the client config (`which can-mcp`, or `where can-mcp` on Windows).

## How it relates to the rest of CANExplorer

`can-mcp` does not need the GUI, much like [`can-server`](remote-buses.md). It works with any python-can interface,
including a [remote bus](remote-buses.md) shared by `can-server` on another machine. That is another way for the GUI
and an agent to share one bus:

```bash
can-mcp -i canexplorer_remote -c "raspberrypi.local:8765/vehicle?token=change-me"
```
