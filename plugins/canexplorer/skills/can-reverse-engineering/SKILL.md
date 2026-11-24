---
name: can-reverse-engineering
description: >-
  Reverse-engineer an unknown CAN or CAN FD bus with the CANExplorer MCP server (can-mcp): survey the traffic, find
  the fields of an identifier, work out what a field means from a physical action or a reference log, confirm the
  bit layout and scaling, and save the result as a DBC. Use when asked "what is on this bus?", "find the steering
  angle / brake / speed signal", "work out what is in 0x1A0", "build a DBC for this car", or to decode a CAN trace
  that has no database. Needs the CANExplorer MCP tools (bus_status, analyze_identifier, extract_field,
  promote_to_dbc, …).
---

# Reverse-engineering a CAN bus with CANExplorer

This skill drives the CANExplorer MCP server, either `can-mcp` or the in-app **Agent…** server. Its tools are named
below without a prefix. In Claude Code they show up as `mcp__<server>__<tool>`, for example
`mcp__canexplorer__analyze_identifier`.

The aim is a DBC whose signals are **evidence-backed**. Every signal you write should have a stated reason: it
reacted to a marked action, it correlated with a reference, or it is an unambiguous counter or checksum. Never name
a signal from a guess and present it as fact.

## 0. Check the setup

Call `bus_status`.

- **No CANExplorer tools are available.** Tell the user to install CANExplorer with its MCP extra
  (`pip install "can-explorer[mcp]"`), then register the server with `claude mcp add canexplorer -- can-mcp`. They
  can instead start the in-app server from **Trace → Agent…** in the GUI. Stop here until they have done that.
- **`"connected": false`.** Call `list_interfaces`. Ask the user which interface, channel and bitrate to use unless
  they already said: a wrong bitrate gives error frames and no data. Then call `connect`. Common values are
  500000 bit/s for powertrain buses and 125000 for comfort buses. For CAN FD, pass `fd=true` and the data bitrate.
- **`error_frames` keeps rising.** The bitrate is probably wrong, or the bus is not terminated. Say so before
  analysing anything.
- **The tiers.** `capabilities` shows which tiers are enabled. Everything in this skill is **read-only** and
  sends nothing on the bus. Do not ask for diagnostics, control or send rights to reverse-engineer passive
  traffic.

## 1. Survey the bus

1. `list_identifiers` lists the identifiers with their counts, cycle times and latest data.
2. `timing` separates periodic from event-triggered identifiers. Fast periodic identifiers (10–20 ms) usually
   carry powertrain and chassis values. Slow ones (100 ms to 1 s) usually carry status, network management or
   comfort data. Event-triggered ones follow actions such as buttons, doors and locks.
3. If the bus is mostly J1939, CANopen, NMEA 2000 or another standard protocol, `decode_protocol` may already
   explain the frames. The protocol is often given away by extended identifiers such as `18FEF100`, or by a
   `7E0`/`7E8` diagnostic pair. A database can also already exist: if the user has one, call `load_database` and
   work only on the identifiers it does not describe.
4. Give the user a short summary: how many identifiers there are, which are fast and which are event-driven, and
   which ones you suggest looking at first.

Wait until a few seconds of traffic have been captured before analysing. The analysis only sees the **recent**
frames, so a value that never changed in that window looks constant.

## 2. Find the fields of an identifier

Call `analyze_identifier` with the identifier as hex, for example `"1A0"`. Pass `extended=true` for 29-bit
identifiers. Each field it returns is written `ID[start|length@order sign]`, as in a DBC: `@1` is little-endian
(Intel), `@0` big-endian (Motorola), `+` unsigned and `-` signed. Each field also has a `kind`:

- `checksum` and `counter` are usually exact, so trust them at high confidence.
- `physical` changes smoothly, so it is a candidate quantity. Its meaning is still unknown.
- `enum` and `flag` are candidate modes and switches. Their meaning is still unknown.
- `multiplexer` selects which layout the rest of the frame has. Analyse each multiplexer value as its own frame.
- `constant` fields are hidden unless you pass `include_constant=true`. `unknown` bits change but fit no pattern.
- `freshness` and `mac` are AUTOSAR SecOC fields from a loaded ARXML.
- `random` is only a hint: possibly a truncated MAC or encrypted data. Report it as unexplained rather than
  inventing a layout for it.

If a byte does not split cleanly into fields, use `bit_activity`. It shows per-bit set and flip rates, so constant
bits, toggling bits and counter bits stand out. It also gives a per-byte entropy.

## 3. Find out what a field means

The analysis says *where* the fields are. These steps show *what* they are. Choose by what the user can do:

- **A repeatable action** (pressing the brake, flicking an indicator, turning the wheel to full lock). Ask the user
  to do it about five times and to tell you each time. Call `mark` with a label at each one, then call
  `reaction_at_markers`. A real reaction hits most of the markers and has a low background change rate. A field
  that hits one or two markers but changes all the time anyway is a coincidence. Call `clear_markers` before
  starting on a different action.
- **A single action** (starting the engine, opening a door). Ask the user to keep everything still for about
  5 seconds, then do the action, then wait about 5 seconds. Then call `capture_reaction`, setting
  `baseline_seconds` and `action_seconds` to the times used. It reports identifiers that appeared and bits that
  were stable and then changed.
- **A reference** (a speed from GPS or OBD logged to CSV, a signal from another database, or values the user reads
  off the dashboard). Call `correlate`. It returns the best-matching fields with their fitted factor, offset and
  time lag, already in the reference's unit.
- **No action and no reference.** Call `related_signals` to find the same quantity rebroadcast on several
  identifiers. A value repeated by a gateway is usually an important one. After that, state plainly that the
  meaning is unconfirmed.

## 4. Confirm the layout and scaling

Before saving a field, check your hypothesis with `extract_field`: the start bit, length, byte order, sign, scale
and offset. For `little_endian`, `start` is the least significant bit. For `big_endian`, it is the most
significant bit. Look at `min`, `max` and the `series`:

- Do the values make physical sense? A speed should not go negative, and an engine should not idle at
  20 000 rpm.
- Do they line up with what the user did? The steering angle should be roughly zero when the wheels are straight.
- Values that jump or wrap around point to the wrong byte order, length or sign. Try the other byte order, or one
  byte more or less.

If a fitted scale is close to a round factor such as 0.1, 0.25, 0.5, 1/128 or 0.01, it is likely the real one.
Prefer the round factor and say that you rounded it.

## 5. Save the result

Call `promote_to_dbc` with a `path` ending in `.dbc` and an explicit `fields` list for what you confirmed. Each
entry is `{identifier, start, length, byte_order, signed, scale, offset, name, unit, comment}`. An existing file at
`path` is extended, not overwritten. Put the evidence in `comment`, for example `"reacts to brake pedal, 5/5
markers"` or `"correlates 0.998 with GPS speed, lag 40 ms"`. Calling `promote_to_dbc` without `fields` writes
every field the analysis found; only do that if the user asks for it, and then set `min_confidence`.

Then call `load_database` on the new file and `get_frames` for the identifier, and show the user the decoded
signals as a check.

## Reporting

End with a table of what you found: the identifier, the bit field, the name, the scale and unit, the evidence, and
your confidence. List separately what is still unexplained. Keep three things apart:

- **Confirmed** fields: an action, a reference or an exact checksum or counter backs them.
- **Plausible** fields: the value behaves like a quantity, but nothing confirms its meaning.
- **Unknown** bytes.

## Safety

- This workflow is passive. If the user asks you to send frames, replay traffic or run diagnostics, point out that
  this needs the matching tier (`--allow-send`, `--allow-diagnostics` or `--allow-control`). Those tools act on a
  real vehicle, so confirm the setup with the user first, for example a parked car or a bench.
- Do not ask the user to perform actions while driving. Use a parked car, a bench, or a passenger with a laptop and
  a logged reference.
