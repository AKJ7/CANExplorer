# Recording, replay and export

## Log file formats

CANExplorer reads and writes the log formats of python-can, chosen by the file extension:

| Extension | Format |
|---|---|
| `.asc` | Vector ASCII |
| `.blf` | Vector binary logging format |
| `.csv` | Comma separated values |
| `.log` | can-utils `candump -l` |
| `.trc` | PEAK trace |
| `.db` | SQLite |

## Recording

**Record…** asks for a file and records every frame of the connection from then on, independent of the display
filter and of **Pause view**. The status line shows the file while recording. **Stop recording**, disconnecting or
closing the connection ends the recording.

If writing fails, e.g. because the disk is full, the recording stops and an error is shown; the connection
keeps running.

## Replay

**⋮ → Replay Log File…** (++ctrl+o++) or the welcome page opens a recording as a new connection:

- **Protocol**: protocol decoder for the *Info* column.
- **Reproduce original timing**: plays the frames with their recorded timing. Without it, the file is read as
  fast as possible, e.g. to analyse a long recording.

The replay starts immediately. **Stop** ends it and **Replay** starts it again. Replays can be filtered,
plotted, decoded with databases and exported like live connections, but nothing can be transmitted.
**⋮ → Recent Log Files** reopens recent replays with their settings.

## Export

**Export…** saves frames of the trace to a log file: the selected rows, or all rows currently displayed (which
honours the filter) when nothing is selected.
