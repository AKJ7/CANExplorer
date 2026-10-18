# Protocol decoding

The protocol chosen for a connection decodes the frames passively into the *Info* column. Decoding runs for every
frame in arrival order, so multi-frame transfers are reassembled.

| Protocol | Decodes |
|---|---|
| Raw | Nothing (only the signal database) |
| ISO-TP | ISO 15765-2 single, first, consecutive and flow control frames; reassembled payloads |
| UDS | ISO-TP, plus the UDS services in the reassembled payloads |
| J1939 | Priority, PGN with names of common PGNs, source and destination address, requests, address claims with the NAME, DM1/DM2 lamps and trouble codes, transport protocol (BAM and RTS/CTS) with reassembly; see also the [J1939 view](j1939.md) |
| CANopen | NMT, SYNC, EMCY, TIME, PDOs, SDO requests and responses, heartbeat and node guarding; with an object dictionary loaded in the [CANopen view](canopen.md), SDO object names and PDO values |
| LIN | Headers and responses, diagnostic frames, see [LIN](lin.md) |

!!! tip "Choose the protocol of the traffic"
    ISO-TP and UDS interpret the first byte of every frame as a transport protocol header. On a bus that also
    carries ordinary signal frames these are labelled as ISO-TP as well; use **Raw** there, or a filter such as
    `7E0-7EF` to look at the diagnostic traffic.

Examples:

```text
ISO-TP complete (20 B): 62 F1 90 57 44 42 … | UDS Positive response to ReadDataByIdentifier: F1 90 57 44 42 …
PGN 0x0FECA DM1 SA=0x00 DA=0xFF P6: TP.DT #2: complete PGN 0x0FECA DM1 (10 B): 01 02 03 …
SDO response node 5: read response 0x1018:01 = 78 56 34 12
Heartbeat node 5: Operational
```

With a [diagnostic description](diagnostics.md#diagnostic-descriptions-cdd), UDS (and LIN diagnostics) decode
ReadDataByIdentifier data into fields.

## Limits

- ISO-TP: normal addressing; the extended and mixed addressing formats are not decoded.
- J1939: names are known for common PGNs and SPNs; signals of J1939 messages are decoded with a DBC.
- CANopen: the predefined connection set (default COB-IDs).
