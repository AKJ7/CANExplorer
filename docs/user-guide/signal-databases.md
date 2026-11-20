# Signal databases

A signal database names the messages and signals of a bus. With a database loaded, the *Info* column shows the
decoded signals, signals can be [plotted](signal-plots.md), and [simulation scripts](simulation.md) can send and
receive by name.

## Supported formats

| Extension | Format | Load | Convert to |
|---|---|:---:|:---:|
| `.dbc` | Vector CANdb++ DBC | ✓ | ✓ |
| `.dbf` | BUSMASTER database | ✓ | ✓ |
| `.arxml` | AUTOSAR system description (3.x and 4.x; written as R20-11) | ✓ | ✓ |
| `.kcd` | Kayak KCD | ✓ | ✓ |
| `.sym` | PEAK PCAN Symbol | ✓ | ✓ |
| `.ldf` | LIN description file | ✓ | |
| `.cdd` | Vector CANdelaStudio diagnostic description | ✓ ([Diagnostics](diagnostics.md#diagnostic-descriptions-cdd-odxpdx)) | |
| `.odx`, `.odx-d`, `.pdx` | ASAM ODX / packaged ODX diagnostic description | ✓ ([Diagnostics](diagnostics.md#diagnostic-descriptions-cdd-odxpdx)) | |

DBC, ARXML, KCD and SYM are read with [cantools](https://cantools.readthedocs.io/), DBF, CDD and ODX/PDX with
readers of CANExplorer, LDF with [ldfparser](https://pypi.org/project/ldfparser/). DBC, KCD and SYM are written with
cantools, DBF and ARXML with writers of CANExplorer (see [Writing ARXML](#writing-arxml)).

## Loading a database

**Load database…** in the toolbar loads a database for the current connection; **Unload database** removes it.
Each connection has its own database. A `.cdd`, `.odx`, `.odx-d` or `.pdx` file loaded this way becomes the diagnostic description of
the connection and leaves the signal database unchanged.

The *Info* column then shows, for example:

```text
EngineData: EngineSpeed=3200 rpm, CoolantTemp=60 degC, GearPosition=Second
```

Value tables are shown with their texts (`GearPosition=Second`), multiplexed messages with the signals of the
active multiplexer value.

## Databases with several buses

ARXML system descriptions and KCD files may describe several buses (clusters) that reuse the same identifiers. A
connection decodes with the messages of one bus:

- When exactly one bus has the bitrate of the connection, that bus is selected when the database is loaded.
- Otherwise, or to change it, pick the bus in the selector next to **Unload database**. *All buses* uses the first
  definition of each identifier.

## ARXML container frames

Container I-PDUs (several PDUs with headers in one, usually CAN FD, frame) are decoded into their contained PDUs:

```text
Bundle [EnginePdu: EngineSpeed=500 rpm, GearPosition=First; StatusPdu: AliveCounter=7]
```

The signals of contained PDUs are listed and plotted under the PDU name, e.g. `StatusPdu.AliveCounter`.

## ARXML end-to-end protection (E2E)

When an ARXML system description protects PDUs with AUTOSAR **E2E** (an `END-TO-END-PROTECTION-SET`), the trace
checks the CRC of every received frame of those PDUs and adds the result to its description:

```text
EngineData: EngineSpeed=833 rpm, GearPosition=7 [E2E P05 ok, counter 7]
EngineData: EngineSpeed=833.25 rpm, GearPosition=7 [E2E P05 CRC ERROR 0x0D05, expected 0x0D04, counter 7]
```

**Profiles 2, 5 and 22** are checked (with the AUTOSAR CRC routines, verified against the worked examples of the
AUTOSAR E2E Protocol Specification). The toolbar shows *E2E: n of m checked*; its tooltip lists each protected
message. A protection is shown as *not checked*, with the reason, when its profile is not implemented (e.g.
profiles 1, 4, 7 and 11), when the profile cannot be told from its `CATEGORY` (CANExplorer does not guess it from
the number of Data IDs: 16 fit both profiles 2 and 22), or when its Data IDs do not fit the profile.

Each frame is checked on its own: the counter is shown, but gaps in the counter sequence (lost or repeated frames)
are not tracked, and the PDUs inside container frames are not checked.

## ARXML secured PDUs (SecOC)

Secured PDUs (`SECURED-I-PDU`, AUTOSAR SecOC) of an ARXML are checked in the trace too — their MAC and freshness
value, with keys from the [security manager](security-manager.md#secoc-in-the-trace). The toolbar shows *SecOC: n
of m checked*.

## Editing databases

The [database editor](database-editor.md) changes the messages, signals and nodes of DBC, DBF, KCD, SYM and ARXML
files (an opened ARXML is saved under a new name, see [Writing ARXML](#writing-arxml)). **Edit database** in the toolbar opens the database of the
connection; saving it updates the decoding.

## Converting databases

**⋮ → Convert Signal Database…** (also on the welcome page) converts between the formats with ✓ in the table above. Choose the source
and the destination file; the format follows the extension.

![Convert dialog](../assets/screenshots/convert-database.png)

The summary lists what the target format cannot hold, for example:

- several buses merged into one, with identifiers that clash (not in ARXML, which keeps the buses apart),
- container frames written without their contained PDUs (not in ARXML),
- cycle times and attributes that BUSMASTER DBF does not store,
- names ARXML does not allow, and nested multiplexing (see below).

## Writing ARXML

CANExplorer writes an AUTOSAR R20-11 system description (schema `AUTOSAR_00049.xsd`) of the CAN part of a database,
for tools that only import ARXML. It holds:

| Database | ARXML |
|---|---|
| Bus (or one bus named `CAN` for a DBC) | `CAN-CLUSTER` with its baud rates and a physical channel |
| Message | `CAN-FRAME`, its `CAN-FRAME-TRIGGERING` (identifier, standard/extended, CAN 2.0/CAN FD) and a PDU |
| Signals of a message | `I-SIGNAL-I-PDU` with the cycle time; `MULTIPLEXED-I-PDU` for a multiplexed message |
| Signal | `I-SIGNAL` (length, base type, initial and invalid value) and `SYSTEM-SIGNAL` (comment, unit, conversion) |
| Factor, offset, minimum, maximum, value table | `COMPU-METHOD` (`LINEAR`, `TEXTTABLE` or `SCALE_LINEAR_AND_TEXTTABLE`) |
| Node | `ECU-INSTANCE` with `I-SIGNAL-I-PDU-GROUP`s for what it sends and receives |
| Container frame, secured PDU, E2E protection (from an ARXML) | `CONTAINER-I-PDU` (short headers), `SECURED-I-PDU`, `END-TO-END-PROTECTION` |

Signal positions are written as they are: AUTOSAR counts a signal's start bit like DBC (the least significant bit
of a little-endian signal, the most significant bit of a big-endian one).

What ARXML or the database model cannot carry is left out, and the summary or the editor's *Notes* say so:

- **Nested multiplexing** (a multiplexed signal that is itself a multiplexer): a `MULTIPLEXED-I-PDU` has one
  selector field, so the signals of the deeper levels are left out.
- **Names** with characters other than letters, digits and `_`, or not starting with a letter, are changed (`_x`
  becomes `N_x`); names used twice get a number.
- **Receivers** belong to whole messages in ARXML: an ECU that receives one signal of a message receives the message.
- DBC attributes other than the cycle time, and signal groups.
- E2E protections and secured PDUs keep what CANExplorer reads from an ARXML (profile and Data IDs; Data ID,
  algorithm names and lengths), not a full E2E or SecOC configuration.

When CANExplorer reads such a file back, the multiplexer signal is named after the message (`Muxed_selector1`), as
the ARXML stores no name for it, and its minimum and maximum are not read.

**An opened ARXML is never overwritten.** A system description usually holds much more than the signal database —
software components, other buses, transformer configurations — which rewriting it would drop. The editor's **Save**
therefore asks for a new name for an opened ARXML. Files CANExplorer wrote itself start with the comment
`<!-- Written by CANExplorer: the CAN signal database only. -->` and are saved over directly; a file changed by
another ARXML tool loses the comment and is protected again.

The same is available on the command line, including converting a single bus; see
[can-db-convert](../reference/command-line.md#can-db-convert).

## Databases in scripts

`DbcDecoder` loads any of the formats above and decodes frames as the trace does; its `database` is a
[cantools](https://cantools.readthedocs.io/) database, which encodes them:

```python
import can

from can_explorer.core.database_io import convert_database
from can_explorer.core.decoding import DbcDecoder

database = DbcDecoder('vehicle.dbc')  # Also .dbf, .arxml, .kcd and .sym
engine = database.database.get_message_by_name('EngineData')  # A cantools message
data = engine.encode({'EngineSpeed': 2400, 'CoolantTemp': 90, 'GearPosition': 'Second'})
msg = can.Message(arbitration_id=engine.frame_id, data=data, is_extended_id=False)
print(database.describe(msg))  # As in the Info column of the trace
print(database.decode(msg))

report = convert_database('vehicle.dbc', 'vehicle.kcd')  # As can-db-convert does
print(report.messages, report.signals, report.warnings)  # 2 4 []
```

```text
EngineData: EngineSpeed=2400 rpm, CoolantTemp=90 degC, GearPosition=Second
{'EngineSpeed': 2400.0, 'CoolantTemp': 90, 'GearPosition': 'Second'}
```

Simulation and test scripts get the connection's database without loading it: see
[simulation](simulation.md) and [test automation](test-automation.md).
