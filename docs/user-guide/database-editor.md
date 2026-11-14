# Database editor

The database editor creates and changes signal databases: CAN databases with their messages, signals and nodes,
and LIN description files with their frames, signals, nodes, encoding types and schedule tables. Open it with
**Database editor** under *Tools* in the sidebar, **⋮ → Edit Signal Database…**, the welcome page, or **Edit
database** in the toolbar of a connection, which opens the database the connection decodes with.

![Database editor](../assets/screenshots/database-editor.png)

## Formats

| Format | Open | Save |
|---|---|---|
| Vector DBC (`.dbc`) | ✓ | ✓ |
| BUSMASTER DBF (`.dbf`) | ✓ | ✓ |
| Kayak KCD (`.kcd`) | ✓ | ✓ |
| PCAN Symbol (`.sym`) | ✓ | ✓ |
| AUTOSAR ARXML (`.arxml`) | ✓ | Save as one of the CAN formats above (DBC is suggested) |
| LIN description file (`.ldf`) | ✓ | ✓ (see [LIN databases](#lin-databases)) |

**New ▾** creates an empty CAN or LIN database. CANdelaStudio diagnostic descriptions (CDD) cannot be edited.

**Save** writes back to the opened file; **Save as…** writes another file, in the format of its extension, which
also converts the database. A read-only format (ARXML) has no in-place Save: **Save** and **Save as…** open the
Save-as dialog with a writable name suggested (the same file as a `.dbc`), so an edited ARXML is never a dead end. When the format cannot hold everything, the editor lists what was left out under
*Notes*, for example several senders of a message in a DBF file, or senders and receivers in a SYM file.
**Revert** reloads the file and discards the changes. An orange dot next to the file name and next to *Database
editor* in the sidebar marks unsaved changes; opening another file, creating a new one or quitting asks before
they are lost.

Connections that decode with the saved file reload it, so the trace, overview and signal plots use the changes
right away.

## Undo and redo

**↶** and **↷** in the header, or ++ctrl+z++ and ++ctrl+shift+z++ (++ctrl+y++ on Windows), undo and redo edits:
changing a field, adding, removing, duplicating, copying or pasting messages and signals, removing a node, and
signals promoted from [reverse engineering](reverse-engineering.md) (each promotion is one step). Undo selects the
message the step changed, so you see what was undone. While a text field is being edited, the keys undo the
typing in that field instead.

Undoing back to the saved content clears the unsaved-changes dot; saving keeps the history, so you can still undo
past a save. An edit that fails changes nothing (no partial edit is left), and setting a field to the value it has
is not a step. Opening another file, creating a new one or **Revert** starts a new history. The last 200 steps are
kept. This works for LIN databases too.

## Messages

The list on the left shows the identifier, name, an **FD** tag for CAN FD frames, the number of signals and a red
dot for messages with problems. **+** adds a message with a free identifier, **⧉** duplicates the selected one
(with its signals, under the name *Name_copy* and the next free identifier after the original, placed right after
it), **−** removes the selected one.

The search field filters by message name or identifier, and by **signal name**: a message whose signals match is
listed with the matching signals under it (*↳ WheelSpeed*), and clicking it selects the first of them.

### Signals by sender

**By sender** above the list shows the database as a tree: each node that sends messages, its messages under it
(identifier, name and number of signals) and their signals under them. A message with several senders is listed
under each of them, and the messages without a sender come last, under *No sender*. In LIN databases, frames and
signals are grouped by the publisher of the signals.

Nodes start unfolded and messages folded, so even a large database opens as a short list. Click a node, or the
**▸** of a message (or double-click the message), to unfold or fold it; ++left++ and ++right++ fold and unfold the
focused row. **⊞** and **⊟** unfold and fold everything. Clicking a message or a signal selects it for editing.

The search field filters the tree too: a node, message or identifier that matches keeps all that is under it, and a
matching signal keeps its message and node. The matches are shown unfolded; what you fold while searching is
folded until the search changes, and clearing the search brings back the folding of before.

## Signals

Select a signal in the list (its colour matches the layout below); **+** adds an 8 bit signal at the first free
byte, **−** removes the selected one. **⧉** duplicates the selected signal into its own message, and **Copy** then
**Paste** copies it into another message (select the message, then paste; it can also be pasted into another
database opened afterwards). The copy keeps the name when it is free in that message (else *Name_copy*) and is
placed at the first free bits where it fits; when nothing is free it keeps its start bit and the overlap is listed
under *Problems*. A multiplexed signal stays multiplexed only in a message with its multiplexer; a copy of a
multiplexer into a message that has one becomes a plain signal. LIN signals cannot be duplicated or copied. Changes are applied when a field is left or ++enter++ is pressed; invalid
values are reported in the header and the field returns to the stored value.

| Property | Description |
|---|---|
| Name | Unique within the message |
| Start bit, length | Position and size in bits. For Motorola signals the start bit is the most significant bit, as in DBC files |
| Byte order | Intel (little endian) or Motorola (big endian) |
| Value type | Unsigned or signed |
| Factor, offset | Physical value = raw value × factor + offset |
| Minimum, maximum | Physical range; empty for none |
| Unit | E.g. `rpm`, `km/h` |
| Initial value | Raw value before the first frame |
| Receivers | Node names, comma separated |
| Value descriptions | Names of raw values, e.g. `0=Off; 1=On; 3=Error` (hexadecimal values such as `0x0F=Fault` work too) |
| Comment | Free text |
| Multiplexing | **None**, **Multiplexer** (the switch) or **Multiplexed** (present only for some switch values) |

### Multiplexing

A message can carry different signals depending on the value of one **multiplexer** (switch) signal. Set one signal
to **Multiplexer**, then set the others to **Multiplexed** and enter the **switch values** they appear for
(e.g. `0, 1, 3`). Multiplexed signals for different switch values may share the same bits. When a message has more
than one multiplexer, a second control chooses which switch a signal follows. The switch cannot be removed while
signals still follow it, and renaming it updates them.

## Layout and problems

The layout shows the bits of the message, byte by byte with bit 7 on the left. Each signal's bits have its
colour, the selected signal is outlined, bits used by several signals are red, and clicking a bit selects its
signal.

![Layout of a message with a Motorola signal overlapping another one](../assets/screenshots/database-editor-layout.png)

*Problems* lists what makes frames undecodable or ambiguous: overlapping signals, signals reaching beyond the
message length, minimums above maximums and identifiers used by two messages. They do not prevent saving.

## LIN databases

A LIN description file opens with four tabs.

![Editing a LIN description file](../assets/screenshots/database-editor-lin.png)

**Frames and signals** works like the CAN messages above, with the properties of LIN frames and signals:

| Frame | |
|---|---|
| Identifier | 0 to 0x3B (0x3C and above are the diagnostic frames) |
| Length | 1 to 8 bytes |
| Publisher | The node sending the response; it also publishes the frame's signals |

| Signal | |
|---|---|
| Start bit, length | Bit offset in the frame and width (1 to 64 bits); LIN sends the least significant bit first |
| Initial value | A number, or for byte arrays the bytes, e.g. `1, 2, 3, 4` for a 32 bit signal |
| Subscribers | The nodes receiving the signal |
| Encoding type | How raw values translate into physical values and texts, see below |

**Nodes** sets the bus speed, the LIN protocol and language versions, the channel name and the master node with
its time base and jitter, and lists the slave nodes with their node attributes: configured and initial NAD,
supplier and function ID, variant and LIN protocol. Renaming a node renames it in all frames, signals and
schedule tables; a node that still publishes frames or signals cannot be removed.

**Encoding types** and **Schedule tables** are edited as text in the syntax of the LDF and checked when
**Apply** is pressed:

```text
logical_value, 0, "off";
physical_value, 1, 254, 0.5, -40, "degC";
```

```text
SeatStatus delay 10 ms;
MasterReq delay 20 ms;
AssignNAD { SeatECU } delay 20 ms;
```

Renaming a frame or signal also renames it in schedule tables, sporadic and event triggered frames, node
attributes and encodings; removing a frame removes its signals and its schedule entries. The file is written in
the LDF syntax and read back with the LIN parser before it replaces the file, so only valid files are saved.
Comments of the original file are not kept.

