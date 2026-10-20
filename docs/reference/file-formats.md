# File formats

## Log files

Read and written through [python-can](https://python-can.readthedocs.io/en/stable/file_io.html); the format follows
the extension.

| Extension | Format | Notes |
|---|---|---|
| `.asc` | Vector ASCII log | Text; widely supported by analysis tools |
| `.blf` | Vector binary logging format | Compact binary; channel names are stored as numbers (CANExplorer drops channel names that do not fit) |
| `.csv` | Comma separated values | One frame per line |
| `.log` | can-utils `candump -l` | Text; readable by `canplayer` |
| `.trc` | PEAK trace | Text |
| `.db` | SQLite | One table of frames |
| `.mf4` | ASAM MDF 4 | Binary measurement data format of ASAM, read by tools such as asammdf and CANape; needs the optional `mf4` extra (see below) |
| `.asc.gz`, `.csv.gz`, `.log.gz`, `.trc.gz` | The text formats, gzip compressed | Typically 5–10 times smaller; other tools may need the file decompressed first (`gunzip`) |

Every format can be recorded, exported, replayed and replayed on a bus. Timestamps are kept, so replays reproduce
the original timing.

MF4 support uses the [asammdf](https://asammdf.readthedocs.io/) package, which brings numpy and pandas along and
is therefore optional:

```bash
uv sync --extra mf4
```

Without it, MF4 is not offered in the file dialogs and opening an `.mf4` file explains what to install.

## Signal databases

| Extension | Format | Read | Write | Library |
|---|---|:---:|:---:|---|
| `.dbc` | Vector CANdb++ | ✓ | ✓ | cantools |
| `.dbf` | BUSMASTER database 1.3 | ✓ | ✓ | CANExplorer |
| `.arxml` | AUTOSAR system description 3.x and 4.x | ✓ | | cantools |
| `.kcd` | Kayak network definition | ✓ | ✓ | cantools |
| `.sym` | PEAK PCAN Symbol 6.0 | ✓ | ✓ | cantools |
| `.ldf` | LIN description file 1.3–2.2A | ✓ | | ldfparser |
| `.cdd` | Vector CANdelaStudio diagnostic description | ✓ | | CANExplorer |
| `.odx`, `.odx-d` | ASAM ODX diagnostic description | ✓ | | CANExplorer |
| `.pdx` | Packaged ODX (ZIP of ODX documents) | ✓ | | CANExplorer |

### BUSMASTER DBF

BUSMASTER databases are text files, written in the Windows ANSI code page with CRLF line endings:

```text
[START_MSG] <name>,<id>,<length>,<signal count>,<byte order>,<S|X>,<sender>
[START_SIGNALS] <name>,<length>,<byte>,<bit>,<type>,<max>,<min>,<byte order>,<offset>,<factor>,<unit>,<multiplex>,<receivers>
[VALUE_DESCRIPTION] "<text>",<raw value>
[END_MSG]
```

- `<byte>` counts from 1 and `<bit>` from 0. They address the **least** significant bit of the signal, also for
  Motorola (big endian) signals, whose DBC start bit is the most significant bit; CANExplorer converts between both
  conventions like BUSMASTER.
- `<byte order>`: 1 Intel (little endian), 0 Motorola (big endian).
- `<type>`: `U` unsigned, `I` signed, `B` boolean, `F` 32-bit and `D` 64-bit float.
- `<max>` and `<min>` are raw values: physical = raw × factor + offset.
- `<multiplex>`: `M` for the multiplexer, `m<n>` for signals present when it is *n*.
- For `[PROTOCOL] J1939` the identifier is the PGN; reading assigns priority 6 and source address 0xFE.

Comments of nodes, messages and signals are kept. Value tables, attributes (e.g. cycle times) and network comments
are not converted.

### CANdelaStudio CDD

CANExplorer reads the data identifiers (DIAGINST with STATICVALUE) of the ECU and the layout of their data:

- Data types with a coded value (`IDENT`, `LINCOMP`, `TEXTTBL` and others with `CVALUETYPE`): unsigned, signed,
  IEEE float and text (`asc`, `utf`) encodings, big (`bo="21"`) and little (`bo="12"`) endian, linear scaling
  (`COMP`), units and text tables (`TEXTMAP`, including value ranges).
- Structures, unions (all alternatives over the same bits) and shared DID structures (`DIDDATAREF`).
- Field offsets count bits from the least significant bit of each byte, like CANdelaStudio; big endian fields are
  located by their most significant bit.

Services, diagnostic trouble code descriptions and structured data types without a coded value are not read.

### ASAM ODX and PDX

CANExplorer reads the ReadDataByIdentifier services (request service id `0x22`) of an ODX document, and from a
`.pdx` archive every ODX document it contains (their identifiers combined):

- The service's name (`LONG-NAME`, otherwise `SHORT-NAME`) names the data identifier; the identifier is the
  constant (`CODED-CONST`) parameter following the service id in the request.
- Each positive-response parameter referencing a data object property (`DOP-REF`) becomes a field, at its
  `BYTE-POSITION`/`BIT-POSITION` relative to the start of the identifier's data.
- Data object properties with a fixed-length `DIAG-CODED-TYPE` (`STANDARD-LENGTH-TYPE`) are decoded: base types
  `A_UINT32`, `A_INT32`, `A_FLOAT32`/`A_FLOAT64`, `A_ASCIISTRING` and `A_UTF8STRING`, high-low (big endian) or
  low-high (little endian) byte order, and `IDENTICAL`, `LINEAR` (single rational scaling) or `TEXTTABLE`
  (value texts) compu methods, with the unit from `UNIT-REF`.

Other services, variable-length types, structures, multiplexers and inheritance across diagnostic layers are not
read; a listed identifier whose data cannot be decoded is shown in hex.

## LIN frames on SocketCAN

LIN drivers following the [sllin](https://rtime.felk.cvut.cz/gitweb/linux-lin.git) convention carry LIN as CAN frames with the
LIN identifier (0x00–0x3F) as standard identifier, headers as remote frames and responses as data frames. Recordings
of LIN connections therefore use the ordinary log formats.
