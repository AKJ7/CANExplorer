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
| **Activity** | A raster of every bit of one identifier across the whole capture, to see its structure at a glance |
| **Automatic analysis** | Checksums, counters, multiplexers, values, states and flags of every identifier, found automatically |
| **Cryptanalysis** | Statistical randomness tests, linear dependency search and keystream reuse detection for every identifier |
| **Gateway security** | Gateway authentication patterns, bypass detection and cross-ECU seed/key correlation |
| **Reference signals** | The fields following a known value: a database signal, a CSV file (e.g. GPS) or OBD-II values |
| **Markers** | The bits changing each time you mark that something happened |
| **Related signals** | Fields carrying the same signal on several messages, grouped |
| **Reaction** | Diff a baseline against an action to find what a control or an event changes on the bus |
| **Console** | An interactive Python console over the captured frames, with the reverse-engineering primitives |

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

## Temporal replay (scrubbing)

**Replay** in the Bits toolbar plays the selected identifier's captured frames back over time. A scrubber bar
appears: drag the slider to any moment, or press play to watch the frames stream by. While replaying, the bit grid,
the byte values and the bit field value show the payload **at that moment**, and bits that just changed light up and
fade — so you can watch the data flow and catch exactly when and how bits move.

- **Play / pause** runs the capture at its recorded pace; **Speed** scales it from 0.1× to 10×.
- **«** and **»** step one frame back or forward; the slider and the `frame N / M` readout show where you are.
- Scrub to the instant something happened and read off which bits and bytes changed into it.

Leave replay (toggle **Replay** off, or select another identifier) to return the grid to the live, latest frame.

## Activity map

The **Activity** tab shows a bit-transition raster of the selected identifier: every bit of its payload across the
whole capture at once, so its structure jumps out where the frame-at-a-time grid and replay cannot show it. Each
**row is a bit** (byte 0 at the top, the most significant bit of each byte first) and each **column is a frame**
over time; **Refresh** rebuilds it from the frames captured so far.

A cell is blue for a steady 1, dark for a steady 0, and orange where the bit changed. The patterns name themselves:

- a **counter** ramps as diagonal stripes, its low bits busier than its high bits;
- a **constant** byte is flat (dark or blue);
- a **physical value** drifts, its high bits slow and its low bits noisy;
- a **toggle** (a flag, a blinker) is a column of orange that comes and goes;
- a **multiplexer** switches whole regions of the payload on and off.

Read off which bytes carry something and what kind, then switch to **Bits** to define the field or **Automatic
analysis** to have it found.

## Timing graphs

Two buttons in the Bits toolbar plot the selected identifier on the [Signals](signal-plots.md) page, from the
frames in the trace so far on:

- **Plot Δt** graphs the **inter-frame interval** (the time between consecutive frames, in milliseconds) over
  time. A flat line is a cyclic message; steps and spikes reveal gateways, bursts and event-triggered messages
  (a frame sent only when something happens). The cursors, statistics (min/max/mean interval) and CSV export of
  the signal plots all apply.
- **Plot bytes** graphs **each payload byte** as a value over time, one chart per byte — a quick look at which
  bytes move and how, without defining a bit field first.

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
| **SecOC freshness** / **SecOC MAC** | The freshness value and the truncated MAC of a [secured PDU](security-manager.md#secoc-in-the-trace), at the bits the connection's ARXML gives (see below) |
| **Random (MAC?)** | Trailing bytes whose every bit changes in about half the frames and is set in about half of them: possibly a truncated MAC or encrypted data (see below) |
| **Linear relation** | Bytes that are an affine (linear over GF(2)) function of the other bytes, such as a CRC-16, CRC-32 or XOR mask — found by Gaussian elimination |
| **Keystream reuse** | Encrypted bytes where the same counter value appeared more than once and the XOR of same-counter frames shows structure (low entropy, ASCII-range bytes), indicating keystream reuse |

**Secured frames.** AUTOSAR SecOC appends a freshness value and a truncated MAC to the payload of a secured PDU.
The MAC changes at random in every frame, so read as an ordinary field it would only be noise split into
meaningless values. When the connection's database is an ARXML describing the PDU as secured, the analysis labels
its **SecOC freshness** and **SecOC MAC** bits exactly and analyses only the rest. Without one, trailing bytes (at
least two) that look random are labelled **Random (MAC?)** and left out of the other fields — a hint, not a finding:
encrypted data and a field of pure noise look the same, and a checksum found at the end of the frame stays a
checksum. The randomness detection uses statistical tests (monobit, runs, chi-square and serial correlation) when
enough frames are available, and falls back to the original half-set/half-flip threshold otherwise. It needs 100
frames or more of the identifier to tell. The freshness value usually shows up as a
**Counter** just before it. To check the MACs, load the ARXML and add the keys in the
[Security Manager](security-manager.md).

**Cryptanalysis tools.** The MCP tool `assess_frame_security` runs a passive security assessment on one identifier:
statistical randomness tests on its trailing bytes, freshness and replay analysis of any counter+MAC pair, a linear
dependency search (GF(2) Gaussian elimination) across the payload, and keystream reuse detection. It never sends
anything on the bus. For active testing on an authorised bench, `core.active_security_test` provides replay and MAC
forgery tests.

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

## Capture reaction

**Reaction** answers "I did X — what changed?" the safe way: it records the bus while steady, then while you perform
an action, and diffs the two. It never transmits — it only watches — so it is the right tool for finding which
message controls something (a relay, a light, a door) without sending unknown frames at the vehicle.

1. **Record baseline** — leave the bus steady (do nothing) for a few seconds.
2. **Start action** — now perform the action (press the button, open the door) once.
3. **Capture reaction** — the action window is diffed against the baseline.

The results list, most reactive first:

- **new** identifiers that appeared only during the action (an ECU started transmitting), and
- **changed** fields: runs of bits that were stable throughout the baseline but changed during the action, with
  their **Change** (value before → after) and how many times they flipped.

Fields that were already moving on their own during the baseline (counters, live sensors) are left out, so what
remains is what the action caused. **Use** a result to load it into the bit field form, or **Plot** it.

Having found the controlling message, confirm it by replaying that captured frame back onto the bus with
**Replay to bus…** or the transmit panel — replaying a real, observed message, not a guessed one.

![Capture reaction: a new identifier, a stepped nibble and a flipped bit found by diffing a baseline against an action](../assets/screenshots/reverse-reaction.png)

## Scripting console

The **Console** tab is an interactive Python console over the frames captured so far — an ad-hoc frame processor
or field hunt with the same primitives the tools use, for when a point-and-click tool does not cut it. Type a
snippet in the syntax-highlighted editor and press **Ctrl+Enter** (or **Run**); the value of a trailing expression
is shown like a Python REPL, `print()` output is captured, and errors are reported instead of raised. Variables persist between
runs; **Clear** empties the transcript and forgets them. Type `HELP` for the available names.

In scope:

- `messages` — the captured frames (`can.Message`); `frames` — `collect(messages)`, keyed by `(id, extended)`;
  `analyses` and `fields` — the results of the **Automatic analysis** tab.
- Helpers `ids()` and `frames_of(id)` (the identifier as an int or a hex string).
- The `can_explorer.core.reverse` primitives: `collect`, `analyse`, `analyse_all`, `BitField`, `candidates`,
  `signal_groups`, `correlate` and `read_reference_csv`.

For example, `[(f.field.key, f.kind) for f in analyse(frames_of('100')).fields]` lists the fields found in `100`,
and `{hex(i): len(f.payloads) for (i, e), f in frames.items()}` counts the frames of each identifier. This is the
same scriptable surface the [MCP interface](mcp.md) exposes to an AI agent, at the keyboard.

!!! note
    The console runs Python in the application's own process — it is not a sandbox. It is for exploring your own
    capture, so do not paste scripts you do not trust.

## Cryptanalysis

The **Cryptanalysis** tab runs a passive security assessment on the payloads of every identifier (or only the
selected one). For each identifier it reports:

| Test | What it checks |
|---|---|
| **Randomness** | Statistical tests on trailing bytes: monobit (proportion of 1-bits), runs (consecutive identical bits), chi-square (uniform distribution of byte values) and serial correlation (lag-1 correlation between frames). A *random* verdict means the bytes look like a random MAC or encrypted data; *structured* means they fail one or more tests |
| **Linear relation** | GF(2) Gaussian elimination across the payload bytes, finding affine bit relations such as CRC-16, CRC-32 or XOR masks, and matching them against known CRC polynomials. The confidence percentage reflects the share of verification frames that matched |
| **MAC classification** | Classifies a suspected MAC byte range by its length: 24/28/32-bit truncated CMAC-AES-128 (AUTOSAR SecOC), 128-bit full CMAC, 256-bit HMAC-SHA-256. When a freshness counter is present alongside a 24-32 bit field, it strongly suggests SecOC rather than a CRC |
| **Keystream reuse** | Finds repeated counter values in suspected encrypted bytes and checks whether the XOR of same-counter frames shows structure (low entropy or ASCII-range bytes), indicating keystream reuse |
| **E2E** | Detects AUTOSAR End-to-End protection from captured payloads without an ARXML: identifies the profile (2, 4, 5, 6, 7, 22), locates the CRC and alive counter, recovers the Data IDs, and reports the match rate. See [E2E reverse engineering](#e2e-reverse-engineering) below |
| **E2E bypass** | Detects E2E protection weaknesses: static CRC (never changes despite payload changes), frozen alive counter, CRC always zero or 0xFF (configured but not computed) |

The verdicts are colour-coded: green for *random*, amber for *structured*, red for *finding*, grey for
*inconclusive* (too few frames). The confidence percentage reflects how many frames were available and how many
tests agreed. Double-clicking a row navigates to that identifier in the Bits tab; the plot button shows the
inter-frame interval.

The same analyses are available as the MCP tool `assess_frame_security` (for one identifier) and in the
interactive console.

### E2E reverse engineering

The E2E detection pipeline runs automatically as part of the Cryptanalysis analysis. For each identifier
with enough frames (at least 30), it:

1. **Detects the alive counter** — scans each nibble and byte for monotonic increment modulo N
   (mod 16 for 4-bit Profile 2/22, mod 256 for 8-bit Profile 5).
2. **Identifies the profile** — tries each AUTOSAR E2E profile's CRC computation at plausible offsets.
   Profiles 4, 6 and 7 are detected by their Length field matching the payload length and their
   characteristic header layout.
3. **Recovers the Data IDs** — for Profile 2/22, each counter value's Data ID is solved
   deterministically from observed CRCs. For Profile 5/6, the 16-bit Data ID is brute-forced
   (65 536 candidates). For Profile 4/7, the Data ID is read from the header.
4. **Checks for bypasses** — static CRC, frozen counter, zero/0xFF CRC bytes.

The MCP tools `detect_e2e` (full pipeline) and `compute_e2e` (compute a valid E2E header with
recovered parameters) provide the same capabilities for agent-driven workflows.

## Gateway security analysis

The **Gateway security** tab analyses captured UDS traffic for gateway behaviour. Press **Run analysis**
to execute the full pipeline; press **Generate report** for a consolidated severity summary, or
**Crack seed/key** to re-run algorithm recovery on extracted pairs.

### Topology mapping

The gateway map clusters responding ECUs by response timing into bus segments, lists the services each
ECU supports and the authentication type (0x27, 0x29 or none). The map is also available as the MCP
tool `map_gateway`.

### Access matrix

The access matrix records every (ECU, service, session) combination observed in the trace and classifies
each as *granted*, *denied* or *conditional* (NRC 0x22 conditionsNotCorrect). Rows with write services
denied only in higher sessions are flagged. MCP tool: `access_matrix`.

### Authentication patterns

Which ECUs require authentication (0x27 SecurityAccess or 0x29 Authentication), at which security level
and in which diagnostic session, and which services are locked (NRC 0x33 securityAccessDenied or 0x34
authenticationRequired).

### Bypass detection

- **Unfiltered IDs**: diagnostic request IDs that never got a securityAccessDenied — the gateway does not
  filter them. Write services (0x2E WriteDID, 0x31 RoutineControl, 0x34 RequestDownload) on an unfiltered ID
  are flagged as findings.
- **Session fallback**: services that are locked in the extended diagnostic session but work in the default
  session when switching back — the gateway may not re-apply filtering.
- **Timing window**: services that succeed within two seconds of a session change, suggesting a window before
  the gateway filters the new session.
- **Functional addressing**: services that work via the functional broadcast address (0x7DF) but are locked
  via physical addressing.

### Cross-ECU seed/key correlation

When the captured traffic contains 0x27 SecurityAccess exchanges for two or more ECUs, the tool
automatically extracts seed/key pairs and checks whether they share the same algorithm or master key
(a compromise of one ECU exposes the others). MCP tool: `correlate_ecu_seeds`.

### NRC fingerprinting

The set of NRC codes an ECU returns and its response timing form a fingerprint that can identify the
software stack or vendor. The analysis matches observed NRC sets against known patterns (e.g. AUTOSAR
Classic returns 0x13 incorrectMessageLengthOrInvalidFormat where Vector stacks return 0x31
requestOutOfRange). MCP tool: `fingerprint_ecu` (via `gateway_report`).

### Authentication timing

Each step of the 0x27 SecurityAccess flow is timed: request-to-seed, seed-to-key-send, and key-send-to-
response. Unusually fast key verification (< 1 ms) may indicate a trivial check; unusually slow
verification (> 50 ms) may indicate a computationally expensive KDF. MCP tool: `analyse_auth_timing`
(via `gateway_report`).

### DID/routine security classification

For each ECU, the tool probes which DIDs are readable and writable, and what authentication posture
each requires. The result is a per-DID breakdown of access, auth requirement and observed NRC.
MCP tool: `classify_did_security`.

### Seed entropy assessment

Extracted seeds are tested for Shannon entropy, unique ratio, sequential patterns and fixed bytes.
Weak seeds (low entropy, predictable counters, repeated values) are flagged. MCP tool:
`assess_seed_quality`.

### Seed/key algorithm cracking

When three or more (seed, key) pairs are available for an ECU, the tool tests hypotheses from identity
through XOR, bit-reversal, GF(2) linear algebra, hash truncation (MD5/SHA-1/SHA-256), CRC (common
CRC-16/32 parameter sets, with the final XOR solved from a pair), AES- and DES/3DES-based KDFs and
LFSR scramblers. Recovered algorithms appear as findings with a severity of *finding*. The **Crack
seed/key** button re-runs cracking independently of the full analysis. MCP tool: `crack_seed_key`.

A recovered closed-form algorithm (identity, XOR, add/subtract, rotation, byte shuffle) can be turned
into a `compute_key(level, seed)` script with `findings_artifacts.match_to_script`, ready to store as a
seed/key entry in the [Security Manager](security-manager.md); key-dependent or table-based matches
raise rather than emit a script that would silently drop part of the algorithm.

### Consolidated report

The **Generate report** button (or MCP tool `gateway_report`) combines all analyses into a single
report with an overall severity rating (info / warning / finding / critical) and a prioritised
summary of the most important findings.

### Bus segmentation detection

The tool clusters CAN IDs by timing characteristics (periodic vs event-driven, rate) and ID range to
infer physical bus segments.  IDs with low-jitter intervals are classified as periodic; the rest as
event-driven.  Contiguous ID ranges (gaps larger than 0x100 start a new segment) are grouped together.
MCP tool: `detect_bus_segments`.

### Response-after-reset analysis

After an ECU reset (0x11), there may be a short boot-up window where security is not yet enforced.
The tool detects services that succeed within two seconds of a reset positive response and flags them.
Write services (WriteDID, RoutineControl, RequestDownload) in this window are flagged as findings.
MCP tool: `analyse_reset_behaviour`.

### OEM-specific NRC decoding

Vendor-specific NRC codes in the 0x70-0x7E range are decoded from public sources (ISO 14229 Annex A,
AUTOSAR SWS documents).  When an ECU returns a vendor-specific NRC, the authentication pattern details
include the decoded name (e.g. 0x72 generalProgrammingFailure, 0x73 wrongBlockSequenceCounter).  Codes
not found in public specifications are labelled "vendor-specific 0xNN" rather than guessed.

### Export

The **Export** button exports the current results as JSON or CSV.  The JSON format includes the full
structured report (ECUs, authentication patterns, bypass findings, access matrix, correlations).  The
CSV format has one row per finding with columns: category, id, finding, severity, details.  MCP tool:
`export_gateway_report` with a `format` parameter.

The same menu offers two more exports built from the last run's capture:

- **Disclosure report (Markdown)** — a responsible-disclosure document for an OEM PSIRT: each weakness
  with its evidence, remediation and WP.29 R155 / ISO 21434 reference, most-severe first. It states
  only what the capture shows and names no exploit. MCP tool: `disclosure_report` (`format` markdown or
  json; `vehicle`, `researcher` and `contact` label it).
- **Discovered DBC scaffold** — a minimal DBC naming each discovered ECU's request and response
  message, with the services seen as comments, to label unknown traffic in the signal views instead of
  re-deriving the topology. MCP tool: `gateway_findings_to_dbc`.

The analysis is passive: it reads the captured frames only and never sends anything on the bus.
Double-clicking a gateway result navigates to that ECU's identifier in the Bits tab.

The response-ID convention (request + 8) is configurable via the `response_offset` parameter in the
MCP tools, for OEMs that use a different offset.

### Active security tests (MCP only)

For authorised lab benches, the MCP send-tier tools transmit frames on the bus to test an ECU's
resilience:

- **replay_test**: replays a captured frame and checks whether the ECU acts on it. A frame protected
  by a freshness counter should be rejected after a short time.
- **forgery_test**: sends frames with the authentic payload but random MAC bytes. A properly
  implemented MAC should reject every forged frame.
- **functional_bypass_test**: sends the same service request via physical and functional addressing
  and compares responses. Reports services that are filtered via physical but pass via functional.
- **session_race_test**: switches to extended session and immediately sends a locked service with
  varying delays. Reports the timing window (if any) during which the service succeeds.
- **reset_bypass_test**: sends ECUReset (0x11) and attempts locked services during the boot-up
  window. Measures the window duration and which services are accessible.
- **dos_resilience_test**: sends diagnostic request bursts at increasing rates and measures response
  degradation, dropped frames and crash/recovery.

All require `--allow-send` and should only be used on a bench you are authorised to test.

### Executing a probing plan

The passive planners above (enumeration, brute-force and non-standard scan) emit the steps worth
trying; `active_prober.execute` runs those steps on a bench. It is the one reverse-engineering
primitive that transmits, and several safety rails bound it: a **dry run** that sends nothing (the
default from the GUI and the `preview_probe_plan` MCP tool), an **allowlist** of request IDs,
**state-changing services skipped by default** (ECU reset, writes, routines, transfers — pass an empty
`blocked_services` to allow them for an engagement), a **rate limit**, a **request cap** and an
optional **per-step confirmation**. Every send is recorded in the [audit log](security-manager.md).
The caller supplies the `send` callable; the module never opens a bus itself, and the live run is
driven from the gateway panel, not from MCP.

## SecOC reverse engineering

The **SecOC** tab detects AUTOSAR SecOC-protected frames from statistical properties of the payload,
without an ARXML. For each identifier with enough frames it reports:

| Category | What it checks |
|---|---|
| **Layout** | The MAC and freshness counter byte boundaries, inferred from byte-level randomness and counter heuristics |
| **Freshness** | Reconstructed counter width, type (monotonic or truncated), rate and wrap count. A counter that wraps is flagged |
| **Strength** | Practical security margin: forgery probability, expected time to forgery, freshness window, and whether the truncation length meets AUTOSAR recommendations |
| **Key recovery** | Tries common/weak keys (all-zero, all-one, Data-ID-derived, ECU-serial-derived) against observed truncated MACs. A recovered key is a critical finding |
| **Bypass** | Detects missing security trailers, static MACs, counter wrap acceptance and post-reset windows |

Press **Analyse all identifiers** to scan every identifier, or **Analyse *selected*** to check only the
currently selected one. Double-clicking a row navigates to that identifier in the Bits tab; the plot
button shows the inter-frame interval.

The same analyses are available as MCP tools: `detect_secoc_layout`, `assess_secoc_strength`,
`try_secoc_key_recovery`, `detect_secoc_bypass`, `detect_shared_secoc_keys`, `predict_freshness`,
`replay_window`, `classify_secoc_profile` and `analyse_e2e_secoc_interaction`.

## Security dashboard

The **Security dashboard** tab combines the results of gateway analysis, SecOC analysis, seed quality
and NRC fingerprints into a single security posture view. Press **Refresh dashboard** to run all
analyses.

The summary cards show:

- **ECUs** — the number of responding ECUs discovered in the trace.
- **Findings** — the total number of security findings across all analyses.
- **Overall score** — a 0–10 severity score (0 = no findings, 10 = critical).
- **Grade** — a letter grade derived from the score (A ≤ 2, B ≤ 4, C ≤ 6, D ≤ 8, F > 8).

Below the cards, a bullet list summarises the top findings and a table lists every finding with its
ECU, category, severity and details. Double-clicking a row navigates to that ECU's identifier.

The same analysis is available as the MCP tool `attack_surface_report`.

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
