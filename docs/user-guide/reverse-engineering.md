# Reverse engineering

The **Reverse engineering** view of a connection helps to find signals in traffic without a signal database:
which bits of an identifier change, how often and when, which bits change when you do something in the vehicle,
and what a group of bits looks like as a number over time. Fields you identify can be plotted right away and added
to a database.

![Reverse engineering: after a notch only the door switch of 500 stands out; the vehicle speed as a Motorola bit field](../assets/screenshots/reverse-engineering.png)

For diagnostics instead of broadcast traffic, the [UDS discovery](uds-discovery.md) page finds the services and
data identifiers an ECU answers to.

Its tabs:

| Tab | Purpose |
|---|---|
| **Bits** | The bits and bytes of an identifier, the notch, and the bit field form |
| **Automatic analysis** | Checksums, counters, multiplexers, values, states and flags of every identifier, found automatically |
| **Reference signals** | The fields following a known value: a database signal, a CSV file (e.g. GPS) or OBD-II values |
| **Markers** | The bits changing each time you mark that something happened |
| **Related signals** | Fields carrying the same signal on several messages, grouped |

The view works on live buses and on replayed log files. The bit statistics start when it is first opened,
including the frames already in the trace, and follow the bus from then on; **Clear** in the trace toolbar and
**Reset** start again. The other tabs work on the newest million frames of the trace when you start them.

## Identifiers

The list on the left shows every identifier with its frame count, cycle time and the message name when a database
describes it. Select one to see its bits and bytes.

## Bits

The bit grid shows the bits of the selected identifier as in a DBC file: one row per byte, bit 7 on the left.

- The number in a cell is the bit's value in the last frame.
- **Blue** cells change; the stronger the tint, the more often (on a logarithmic scale, so a bit changing in one
  frame in a thousand is still visible). Grey cells never changed.
- A cell flashes **orange** when the bit changes and fades out over a second.
- The tooltip gives the share of the frames with the bit set, the share of the frames in which it changed, and
  when it last changed.

Counters show up as a staircase: the lowest bit changes in every frame, the next one in every second frame and so
on. Physical values such as speeds look similar but slower and less regular, and flags change rarely.

## Finding the bits of an action: the notch

1. Let the traffic run for a while without doing anything, so that the bits that change by themselves are
   counted.
2. Press **Notch**. Every bit that changed so far is hidden (dimmed).
3. Do the action, e.g. press a button, open a door or switch on the lights.
4. Bits that change now are outlined in orange. The identifiers with such bits show how many (e.g. **2 bits**),
   identifiers that appeared only after the notch are marked **new**, and **Only changed since the notch** shows
   just those.

Press **Notch again** to hide these bits as well (e.g. to separate two buttons), and **Clear notch** to show all
bits again.

!!! tip
    A slowly changing value can reach bits it did not use before the notch (e.g. the upper bits of the engine
    speed when you accelerate), and these bits stand out too. Keep the rest of the vehicle as steady as possible
    while doing the action, and repeat it a few times: the bits of the action change every time.

## Bytes

The table below the bit field gives for each byte its value in the last frame, how many distinct values it had,
its minimum and maximum, its entropy (0 bits: constant, 8 bits: every value equally often) and a histogram of its
values from 00 to FF.

## Bit fields

A bit field is a group of bits read as a number, like a signal in a database:

- Click a bit in the grid to start a field there, and Shift+click another bit to extend the field to it. Click a
  byte label to select the whole byte.
- **Byte order**: *Intel* (little endian) fields start at their least significant bit and continue upwards, so a
  16 bit value with its low byte first spans byte 0 and byte 1. *Motorola* (big endian) fields start at their
  most significant bit; a 16 bit value with its high byte first spans byte 0 bit 7 to byte 1 bit 0. Choose the
  byte order before selecting the bits.
- **Signed** reads the field as a two's complement number; **Factor** and **Offset** convert it to a physical
  value (`value = raw × factor + offset`), with a **Unit**.

The title shows the field in DBC notation, e.g. `1A0[16|8@1+]` (start bit, length, `1` Intel or `0` Motorola, `+`
unsigned or `-` signed), and the value in the last frame is shown below the form.

**Plot** shows the field on the [Signals](signal-plots.md) page, with its values in all frames of the trace so
far, next to database signals. A smooth curve means the field is right; a curve jumping between two ranges
usually means the byte order, the signedness or a boundary is wrong. Plotted fields are saved in
[workspaces](workspaces.md), and stay when a database is unloaded.

**Add to database** adds the field as a signal to the [database editor](database-editor.md), together with a
message for the identifier when the database has none (named `Message_1A0`), and opens the editor. Without a
database open in the editor, the connection's database is opened, or a new one started. Save the database and load
it into the connection to decode the signal everywhere.

## Automatic analysis

**Analyse all identifiers** (or only the selected one) looks at the frames of each identifier and lists the fields
it finds, with their kind and how sure it is:

![Automatic analysis: a CRC, an alive counter, values in both bit orders, flags, states and a multiplexer with its cell voltages](../assets/screenshots/reverse-analysis.png)

| Kind | How it is recognised |
|---|---|
| **Checksum** | A byte that is a CRC-8 (SAE J1850 / AUTOSAR, 8H2F, SMBus, Maxim, CDMA2000), XOR or sum of the other bytes, with any start value, final XOR, data ID or added constant; also AUTOSAR E2E profile 2 style data IDs depending on a counter |
| **Counter** | Changes by the same step from frame to frame and runs through its whole range (alive counters) |
| **Multiplexer** | A byte or nibble taking a few values: grouping the frames by its value makes many other bits much calmer. The fields are then found per multiplexer value and shown with their condition, e.g. `if 600[0\|2@1+] = 1` |
| **Value** | Changes smoothly (physical values: speeds, temperatures, voltages); signed when that is smoother, i.e. when it crosses zero |
| **State** | A few values, rarely changing (gear, mode) |
| **Flag** | A single bit |
| **Constant** | Bits that never changed (shown with **Constant bits**) |
| **Unknown** | Changes irregularly: noise, or several fields that could not be told apart |

Field boundaries come from how often each bit changes: within a field, the less significant a bit, the more often
it changes, so a bit changing much less often than the one above it starts a new field. This is done in both Intel
and Motorola bit order, and fields next to each other are told apart by whether one part changes only when the
other wraps around (one field) or independently (two fields). As messages normally use one bit order, the bit
order giving the more plausible fields is kept.

!!! tip "Getting good results"
    The analysis can only find bits that change. Record while the values move through a good part of their range
    (drive, accelerate, steer, switch things on and off) for a minute or more. Bits that never change are reported
    as constant, so a value that never went negative is found as unsigned, and one that stayed below 256 as 8 bits.

**Use** shows a field in the **Bits** tab (for a multiplexed field with its condition, see below); **Plot** plots it
on the Signals page right away.

**Promote to database** turns the whole result into a draft database in one step: every found field except the
constant bits becomes a signal in the [database editor](database-editor.md), each message is created as needed, and
each field's kind, multiplexer condition and details are kept as the signal's comment. Where two interpretations
claim the same bits, the more confident one is used, so the signals never overlap. It is a starting point to
review — rename the signals, set units and scaling, drop what is noise — and then **Save as** a `.dbc` file.

## Reference signals

When you know a value from elsewhere, the field carrying it can be found by comparing:

- **Database signal**: a signal of the connection's database, e.g. to find the same value in another message or
  on another bus (its own message is left out).
- **CSV file**: a file with the time in seconds in the first column and values in the others, for example from a
  GPS logger or a data logger. Headers like `speed [km/h]` give the unit (the CSV export of the signal plots has
  this layout). Times below 10⁹ are taken as seconds from the first frame of the trace, larger ones as absolute
  timestamps.
- **OBD-II values** (live connections that can transmit): choose values such as the engine speed or vehicle
  speed, and **Start polling** while you record. The OBD-II service 01 requests go to the functional address
  `7DF` (`18DB33F1` with **29 bit identifiers**) one after the other, and the answers become references.

![Reference signals: the vehicle speed of a GPS log found in 500 with factor 0.01](../assets/screenshots/reverse-reference.png)

**Find matching fields** samples every field candidate (the fields of the automatic analysis, and every byte
aligned 8 and 16 bit field in both bit orders, unsigned and signed) at the times of the reference and ranks them by
their correlation with it. A straight line fit gives the **factor** and **offset** converting the field into the
reference's unit, and the **lag** is the delay between them (up to **Max. lag**) giving the best match.
Correlations near 1 (or −1) are good matches; when no field comes close the view says so: the value may not be on
this bus, or it was too steady while recording. **Use** fills the bit field form with the factor, offset, unit and
a name.

!!! warning "OBD-II requests"
    Polling sends diagnostic requests on the bus. Only poll on a vehicle you may send to, and stop when you are done.
    The requests and answers (`7DF`, `7E0`–`7EF`, `18DB33F1`, `18DAF1xx`) are left out of the analysis.

## Markers

Markers tell the view when something happened, so the bits changing at that moment can be found, also when they
are not the only bits changing:

1. Type a label, e.g. *door opened*, and press **Mark now** (or ++ctrl+m++) each time you do the action, e.g.
   five times opening the door. Several labels can be used, e.g. *door opened* and *door closed*.
2. **Find changing bits** (for all markers or those with one label) lists the fields whose bits changed within
   the window around most markers (**Seconds before** and **after** the marker, so a marker set a little late
   still counts) but rarely otherwise. **Markers hit** shows at how many markers, **Otherwise** how often the bits
   change elsewhere, and **Changes** the values before and after, e.g. `0 → 1`.

![Markers: the door switch changed at all five markers and never otherwise](../assets/screenshots/reverse-markers.png)

The time of a marker is that of the newest frame, so markers also work while replaying a log file at normal speed.

## Related signals

Vehicles often carry the same quantity on several messages — a gateway re-broadcasts wheel speed, engine speed or a
pedal position from one bus onto another, sometimes scaled or delayed. **Related signals** finds these without any
reference: every value and state field found by the automatic analysis is compared with all the other fields, and
those that follow one another are grouped.

**Find related signals** lists the groups. Each group's first field is the **reference** the others are compared
with; every other member shows its **Correlation** to it, the **Factor** and **Offset** converting its raw value to
the reference's, and the **Lag** (how far it trails the reference, e.g. a gateway re-broadcasting after 20 ms).
**Minimum correlation** sets how closely two fields must match to be grouped, and **Max lag** how far apart in time
a copy may be.

This labels unknowns cheaply: once one member of a group is identified (here, or against a database or GPS log in
**Reference signals**), the others are the same signal elsewhere. **Use** a member to load it into the bit field
form, or **Plot** it.

![Related signals: one signal found on three messages, one of them at twice the scale](../assets/screenshots/reverse-related.png)

## Multiplexed fields

A field found for one multiplexer value carries a condition: the bit field form shows *only if
600[0|2@1+] = 1*, and plotting it takes only the frames with that multiplexer value. Click the condition to use all
frames again. **Add to database** adds the field as a plain signal with the condition in its comment.

## Trying it without a vehicle

`tools/synthetic_traffic.py` writes a log file with traffic from a signal database, with smooth, constant,
counting, toggling or noisy signals, and steps at given times:

```bash
uv run python tools/synthetic_traffic.py examples/simulation/vehicle.dbc traffic.asc --duration 30 \
    --behaviour EngineData.GearPosition=step:10
```

Replay `traffic.asc` at normal speed, open **Reverse engineering**, select `100`, press **Notch** before 10 seconds
and watch bits 24 to 27 when the gear position changes. Then run the automatic analysis: it finds the engine speed,
coolant temperature and wheel speed fields, and the gear position as a state.

The tests use `tests/data/reverse.dbc` (a CRC, an alive counter, Intel and Motorola values, signed values, flags
and a multiplexer) with the same generator, and check that the analysis finds every field whose bits change.
