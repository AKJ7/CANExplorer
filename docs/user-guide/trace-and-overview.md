# Trace and overview

## Trace

The trace lists every received (Rx) and transmitted (Tx) frame in arrival order.

![Trace](../assets/screenshots/trace.png)

| Column | Content |
|---|---|
| Time [s] | Time since the first frame of the session |
| Dir | `Rx` or `Tx` |
| Type | `Std` or `Ext` identifier, plus `FD`, `RTR` (remote frame) and `ERR` (error frame) |
| ID [hex] | Identifier, 3 digits for standard and 8 for extended identifiers |
| DLC | Data length code |
| Data [hex] | Data bytes |
| Info | Protocol decoding and decoded signals of the loaded database |

The trace keeps the most recent frames, 100 000 by default (see [Preferences](preferences.md)). With
**Auto-scroll** it follows new frames unless rows are selected.

## Overview

The overview has one row per identifier and direction, sorted by identifier.

![Overview](../assets/screenshots/overview.png)

| Column | Content |
|---|---|
| Count | Number of frames |
| Cycle [ms] | Time since the previous frame of the identifier |
| Data [hex] | Latest data; bytes that differ from the previous frame of the identifier are highlighted in **bold orange** (the warning colour of the theme) |

Click a column header to sort the overview by that column, click it again to reverse the order, and a third time
to return to the identifier order. The sorted column is marked with ▲ or ▼. Rows move as their values change
(e.g. when sorted by count), and selected rows stay selected.

## Toolbar

The **Connect** / **Disconnect** button (Replay / Stop for log files) and the filter field are in the top bar;
the trace, overview and signals views have these buttons:

| Button | Action |
|---|---|
| Pause view | Freeze the views; frames are still received, recorded and counted, and appear when the view is resumed |
| Clear | Remove all frames, statistics and plot data of the connection |
| Record… | Record to a log file, see [Recording, replay and export](recording-and-replay.md) |
| Export… | Save the selected trace rows, or all displayed rows, to a log file |
| Load database… | Load a signal database or a CDD diagnostic description, see [Signal databases](signal-databases.md) |
| Bus selector | Shown for databases describing several buses |
| Auto-scroll | Keep the newest frame of the trace visible |

## Selecting and copying

Click rows to select them; ++shift++ and ++ctrl++ extend the selection. **⋮ → Copy Selected Rows**
(++ctrl+c++) copies them as tab separated text with a header line, e.g. to paste into a spreadsheet.
**⋮ → Select All** (++ctrl+a++) selects all rows of the current view.

## Bus statistics

The dashboard and the status line are updated every second:

- **Rx/Tx** frame rates in frames per second.
- **Load**: estimated share of time the bus is busy, computed from the frame sizes and the bitrate (and the
  data bitrate for CAN FD frames with bitrate switch). Bit stuffing is not included, so the real load can be up
  to about 20 % higher. For LIN, headers and responses are counted at the LIN baud rate. Not shown for replays.
- **Error frames** received.
- **Bus state** (`active`, `passive`, `error` for bus off) where the interface reports it.
