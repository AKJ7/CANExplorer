# Recording, replay and export

## Log file formats

CANExplorer reads and writes the log formats of python-can, chosen by the file extension. Every format can be
recorded and exported as well as replayed, also on a bus:

| Extension | Format |
|---|---|
| `.asc` | Vector ASCII |
| `.blf` | Vector binary logging format |
| `.csv` | Comma separated values |
| `.log` | can-utils `candump -l` |
| `.trc` | PEAK trace |
| `.db` | SQLite |
| `.mf4` | ASAM MDF 4 (with the optional `mf4` extra, see [File formats](../reference/file-formats.md#log-files)) |
| `.asc.gz`, `.csv.gz`, `.log.gz`, `.trc.gz` | The text formats, gzip compressed |

The file dialogs list each format; when no extension is typed, the one of the chosen format is added.

## Recording

**Record…** asks for a file and records every frame of the connection from then on, independent of the display
filter and of **Pause view**. The status line shows the file while recording. **Stop recording**, disconnecting or
closing the connection ends the recording.

If writing fails, e.g. because the disk is full, the recording stops and an error is shown; the connection
keeps running.

## Replay

**⋮ → Replay Log File…** (++ctrl+o++) or the welcome page opens a recording as a new connection:

- **Protocol**: protocol decoder for the *Info* column.
- **Speed**: 1× plays the frames with their recorded timing; 0.25× to 10× slow it down or speed it up, and
  *As fast as possible* reads the file at once, e.g. to analyse a long recording.

The replay starts immediately. The playback bar above the views shows the file, its state, the position and
length of the recording and the number of frames, and controls the replay while it runs:

| Control | Action |
|---|---|
| ▶ / ❚❚ | Pause and resume the replay (unlike **Pause view**, no frames are read while paused); ▶ plays a finished replay again |
| Speed | Change the speed at any time |

**Stop** in the top bar ends the replay and **Replay** starts it again. Replays can be filtered, plotted, decoded
with databases and exported like live connections, but nothing can be transmitted. **⋮ → Recent Log Files**
reopens recent replays with their settings.

![Replay of a log file, paused](../assets/screenshots/replay.png)

## Replaying on the bus

**Replay to bus…** in the toolbar of a connected bus transmits the frames of a log file on that bus with their
recorded timing, e.g. to reproduce a situation for an ECU on the bench:

| Option | Description |
|---|---|
| File | Any of the log formats above |
| Speed | 0.25× to 10× the recorded timing, or as fast as possible |
| Frames | Empty for all frames, or a [display filter](filters.md) selecting them, e.g. `rx !7DF` sends only the frames received in the recording, except 7DF |
| Repeat until stopped | Start over at the end of the file |

Error frames are not sent. The transmitted frames appear as Tx frames in the trace, overview and statistics, and
can be recorded. A playback bar shows the progress (and the pass when repeating); pause, resume and change the
speed there, **■** stops and **✕** closes the bar. Disconnecting stops the replay; if transmitting fails, it
stops with a notification.

![Log file replayed on the bus](../assets/screenshots/bus-replay.png)

## Export

**Export…** saves frames of the trace to a log file: the selected rows, or all rows currently displayed (which
honours the filter) when nothing is selected.
