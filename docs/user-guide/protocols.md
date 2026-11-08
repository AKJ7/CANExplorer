# Protocol decoding

The protocol chosen for a connection decodes the frames passively into the *Info* column. Decoding runs for every
frame in arrival order, so multi-frame transfers are reassembled.

| Protocol | Decodes |
|---|---|
| Raw | Nothing (only the signal database) |
| ISO-TP | ISO 15765-2 single, first, consecutive and flow control frames; reassembled payloads |
| UDS | ISO-TP, plus the UDS services in the reassembled payloads |
| KWP2000 | ISO-TP, plus the Keyword Protocol 2000 (ISO 14230) services — the diagnostic protocol that preceded UDS, in vehicles from roughly 1996–2006; same ISO-TP transport and request/response convention, the older ISO 14230-3 service set |
| J1939 | Priority, PGN with names of common PGNs, source and destination address, requests, address claims with the NAME, DM1/DM2 lamps and trouble codes, transport protocol (BAM and RTS/CTS) with reassembly; see also the [J1939 view](j1939.md) |
| J1939-22 | SAE J1939-22 (J1939-FD): J1939 on CAN FD. Single-PG frames decode like J1939 on the longer FD payload; the FD transport (FD.TP) and multiplexed-PG (Multi-PG) frames are recognised and labelled |
| NMEA 2000 | The marine protocol, built on J1939: PGN with names of common marine PGNs, source address, Fast Packet reassembly, and fields of the common PGNs (engine, position, COG/SOG, heading, wind, depth, speed, battery) in marine units |
| CANopen | NMT, SYNC, EMCY, TIME, PDOs, SDO requests and responses, heartbeat and node guarding; with an object dictionary loaded in the [CANopen view](canopen.md), SDO object names and PDO values |
| LIN | Headers and responses, diagnostic frames, see [LIN](lin.md) |
| J1587 | SAE J1587 messages (MID and PIDs) of the older heavy-truck bus, with common parameters scaled. J1587 runs over J1708, a 9600-baud serial bus (**not** CAN), so this decodes J1587 message bytes from a serial log or a gateway, not a live J1708 capture |
| NM | AUTOSAR Network Management (CAN-NM / UDP-NM): the source node id, the control-bit flags (Repeat Message, Active Wakeup, Sleep Ready, PN Learning) and, when the Partial Network Information bit is set, the requested partial-network clusters. Point it at the bus's NM identifiers; the node-id and control-vector positions are configurable |
| SecOC | AUTOSAR Secure Onboard Communication: splits a secured PDU into the authentic payload, the truncated freshness value and the truncated MAC. Verifying the MAC (CMAC/AES-128) needs the key and configuration — available as a library (`can_explorer.core.secoc`) |

!!! tip "Choose the protocol of the traffic"
    ISO-TP and UDS interpret the first byte of every frame as a transport protocol header. On a bus that also
    carries ordinary signal frames these are labelled as ISO-TP as well; use **Raw** there, or a filter such as
    `7E0-7EF` to look at the diagnostic traffic.

Examples:

```text
ISO-TP complete (20 B): 62 F1 90 57 44 42 … | UDS Positive response to ReadDataByIdentifier: F1 90 57 44 42 …
PGN 0x0FECA DM1 SA=0x00 DA=0xFF P6: TP.DT #2: complete PGN 0x0FECA DM1 (10 B): 01 02 03 …
PGN 127488 Engine Parameters, Rapid Update SA=35 P2: engine 0: 2400 rpm
SDO response node 5: read response 0x1018:01 = 78 56 34 12
ISO-TP SF (2 B): 10 81 | KWP StartDiagnosticSession 81
PGN 0x02500 Multi-PG SA=0x00 DA=0x00 P6: multiplexed PGs, 32 bytes (contained PGs not expanded)
MID 128 Engine #1, PID 84 Road Speed 60 mph, PID 190 Engine Speed 1500 rpm
NM node 0x12 [ActiveWakeup] PN requests #0, #2
SecOC: authentic 11 22 33 44 | freshness 05 | MAC AABBCCDD
```

With a [diagnostic description](diagnostics.md#diagnostic-descriptions-cdd-odxpdx), UDS (and LIN diagnostics) decode
ReadDataByIdentifier data into fields.

## Limits

- ISO-TP: normal addressing; the extended and mixed addressing formats are not decoded.
- J1939: names are known for common PGNs and SPNs; signals of J1939 messages are decoded with a DBC.
- J1939-22: single-PG frames are decoded; expanding a Multi-PG frame into its contained PGs and reassembling the
  FD transport (FD.TP) need the C-PG / FD-TP byte layout from the SAE J1939-22 standard (which is not public), so
  those frames are recognised and labelled but not expanded.
- NMEA 2000: names for common PGNs, fields for the most common ones; the full PGN catalogue is not built in.
- CANopen: the predefined connection set (default COB-IDs).
- J1587: a subset of MIDs and PIDs is named and the well-known parameters are scaled; others are shown as raw
  bytes. There is no live J1708 serial capture — the decoder works on J1587 message bytes.
- NM: AUTOSAR NM (the PDU shared by CAN-NM and UDP-NM); the older OSEK/VDX direct NM is not decoded.
- SecOC: the decoder shows the secured-PDU structure; verifying the MAC and freshness needs the per-PDU
  configuration (Data ID, key, truncated lengths) from the ARXML, which the UI does not read yet. The CMAC/AES-128
  algorithm and the generate/verify operations are available for scripts in `can_explorer.core.secoc`.
