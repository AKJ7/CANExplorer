# Database editor

The database editor creates and changes CAN signal databases: their messages, signals and nodes. Open it with
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
| AUTOSAR ARXML (`.arxml`) | ✓ | Save as one of the formats above |

LIN description files (LDF) and CANdelaStudio diagnostic descriptions (CDD) cannot be edited.

**Save** writes back to the opened file; **Save as…** writes another file, in the format of its extension, which
also converts the database. When the format cannot hold everything, the editor lists what was left out under
*Notes*, for example several senders of a message in a DBF file, or senders and receivers in a SYM file.
**Revert** reloads the file and discards the changes. An orange dot next to the file name and next to *Database
editor* in the sidebar marks unsaved changes; opening another file, creating a new one or quitting asks before
they are lost.

Connections that decode with the saved file reload it, so the trace, overview and signal plots use the changes
right away.

## Messages

The list on the left shows the identifier, name, an **FD** tag for CAN FD frames, the number of signals and a red
dot for messages with problems. **+** adds a message with a free identifier, **−** removes the selected one; the
search field filters by name or identifier.

| Property | Description |
|---|---|
| Name | Letters, digits and `_`, not starting with a digit; unique |
| Identifier | Hexadecimal; up to 7FF, or 1FFFFFFF for extended frames |
| Length | 0 to 8 bytes, or up to 64 bytes for CAN FD frames |
| Cycle time | In milliseconds; empty for messages that are not sent cyclically |
| Senders | Node names, comma separated; unknown nodes are added to the database |
| Extended (29 bit), CAN FD | Frame format |
| Comment | Free text |

## Signals

Select a signal in the list (its colour matches the layout below); **+** adds an 8 bit signal at the first free
byte, **−** removes the selected one. Changes are applied when a field is left or ++enter++ is pressed; invalid
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

Multiplexed signals show their multiplexer and values; their multiplexing is kept but not edited.

## Layout and problems

The layout shows the bits of the message, byte by byte with bit 7 on the left. Each signal's bits have its
colour, the selected signal is outlined, bits used by several signals are red, and clicking a bit selects its
signal.

![Layout of a message with a Motorola signal overlapping another one](../assets/screenshots/database-editor-layout.png)

*Problems* lists what makes frames undecodable or ambiguous: overlapping signals, signals reaching beyond the
message length, minimums above maximums and identifiers used by two messages. They do not prevent saving.
