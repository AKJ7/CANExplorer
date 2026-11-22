# Protocol decoding

The protocol chosen for a connection decodes the frames passively into the *Info* column. Decoding runs for every
frame in arrival order, so multi-frame transfers are reassembled.

| Protocol | Decodes |
|---|---|
| Raw | Nothing (only the signal database) |
| ISO-TP | ISO 15765-2 single, first, consecutive and flow control frames; reassembled payloads |
| UDS | ISO-TP, plus the UDS services in the reassembled payloads |
| KWP2000 | ISO-TP, plus the Keyword Protocol 2000 (ISO 14230) services — the diagnostic protocol that preceded UDS, in vehicles from roughly 1996–2006; same ISO-TP transport and request/response convention, the older ISO 14230-3 service set |
| ISO-TP (extended), UDS (extended), KWP2000 (extended) | The same with ISO-TP **extended or mixed addressing**: the first data byte of every frame is an address byte (the target address N_TA, or the address extension N_AE of mixed addressing), shown in brackets, e.g. `ISO-TP [12] SF`. Transfers to different addresses on one identifier are reassembled separately |
| K-Line | KWP2000 (ISO 14230) and OBD-II/J1979 services on a [K-Line bus](../reference/interfaces.md#k-line-iso-9141-2-iso-14230) (the `elm327_kline` interface), where each frame already carries one bare service payload (no ISO-TP). Use this rather than KWP2000, which expects ISO-TP on CAN |
| VW TP 2.0 | Volkswagen-group transport protocol 2.0, the CAN transport of VW, Audi, Seat and Škoda diagnostics before UDS (roughly 2000–2011): channel setups on 0x200 (request) and 0x200 + module (response), the channel parameters with block size and timing, channel tests, ACKs and disconnects, and the data packets reassembled into KWP2000 messages, e.g. `TP2.0 0x01 to tester data #4 (last, ACK requested) | KWP Positive response to ReadDataByLocalIdentifier: 01 …`. The channel's identifiers are learnt from the module's setup response; see [VW TP 2.0 and KWP2000](kwp2000-tp20.md) |
| J1939 | Priority, PGN with names of common PGNs, source and destination address, requests, acknowledgments (ACK, NACK, access denied, cannot respond), address claims and commanded addresses with the NAME, DM1/DM2 lamps and trouble codes, identification text (software, component, vehicle), the transport protocol (BAM and RTS/CTS, including packets a receiver asks for again) and the extended transport protocol (ETP, messages over 1785 bytes) with reassembly, and the reasons of connection aborts; see also the [J1939 view](j1939.md) |
| J1939-22 | SAE J1939-22 (J1939-FD): J1939 on CAN FD. Single-PG frames decode like J1939 on the longer FD payload; multiplexed-PG (Multi-PG) frames are split into their parameter groups, and FD transport (FD.TP) messages, BAM and RTS/CTS, are reassembled. [Assurance data trailers](#j1939-22-assurance-data) are split off and, where configured, checked |
| NMEA 2000 | The marine protocol, built on J1939: PGN with names of common marine PGNs, source address, Fast Packet reassembly, and fields of the common PGNs (engine, position, COG/SOG, heading, wind, depth, speed, battery) in marine units |
| CANopen | NMT, SYNC, EMCY, TIME, PDOs, SDO requests and responses, heartbeat and node guarding; with an object dictionary loaded in the [CANopen view](canopen.md), SDO object names and PDO values |
| LIN | Headers and responses, diagnostic frames, see [LIN](lin.md) |
| J1587 | SAE J1587 messages (MID and PIDs) of the older heavy-truck bus, with common parameters scaled. J1587 runs over J1708, a 9600-baud serial bus (**not** CAN); decode a live capture through the [J1708 interface](../reference/interfaces.md#sae-j1708), a serial log, or a gateway. Each frame's data is one J1587 message |
| NM | AUTOSAR Network Management (CAN-NM / UDP-NM): the source node id, the control-bit flags (Repeat Message, Active Wakeup, Sleep Ready, PN Learning) and, when the Partial Network Information bit is set, the requested partial-network clusters. Point it at the bus's NM identifiers; the node-id and control-vector positions are configurable |
| SecOC | AUTOSAR Secure Onboard Communication: splits a secured PDU into the authentic payload, the truncated freshness value and the truncated MAC. Verifying the MAC (CMAC/AES-128) needs the key and configuration — available as a library (`can_explorer.core.secoc`) |
| XCP, CCP | The commands, responses, errors, events and DAQ data of an [XCP or CCP](xcp.md) measurement and calibration session: e.g. `SHORT_UPLOAD 2 @ 0x00001000`, the CONNECT parameters, seed & key, `ERR access locked`, `DAQ PID 3`, the master's `STIM PID 4` and the programming commands. The command/response identifiers are set when the XCP/CCP page connects, or learnt from a CONNECT and its answer; until then only CONNECT candidates are labelled, with a question mark |

!!! tip "Choose the protocol of the traffic"
    ISO-TP and UDS interpret the first byte of every frame as a transport protocol header. On a bus that also
    carries ordinary signal frames these are labelled as ISO-TP as well; use **Raw** there, or a filter such as
    `7E0-7EF` to look at the diagnostic traffic.

Examples:

```text
ISO-TP complete (20 B): 62 F1 90 57 44 42 … | UDS Positive response to ReadDataByIdentifier: F1 90 57 44 42 …
PGN 0x0FECA DM1 SA=0x00 DA=0xFF P6: TP.DT #2: complete PGN 0x0FECA DM1 (10 B): 01 02 03 …
PGN 0x0E800 Acknowledgment SA=0x00 DA=0xFF P6: NACK for PGN 0x0FEDA requested by 0xF9
PGN 0x0C800 ETP.CM SA=0x42 DA=0xF9 P7: ETP.CTS 16 packets from #257 for PGN 0x0D700
ISO-TP [12] SF (2 B): 10 03 | UDS DiagnosticSessionControl 03
PGN 127488 Engine Parameters, Rapid Update SA=35 P2: engine 0: 2400 rpm
SDO response node 5: read response 0x1018:01 = 78 56 34 12
ISO-TP SF (2 B): 10 81 | KWP StartDiagnosticSession 81
PGN 0x02500 Multi-PG SA=0x00 DA=0xFF P3: 2 PGs: PGN 0x0FEF1 CCVS1: FF 00 3C 00 00 00 00 FF | PGN 0x0F004 EEC1: 01 02 …
PGN 0x04E00 FD.TP.DT SA=0x00 DA=0x42 P7: FD.TP.DT session 0 #3/3: complete PGN 0x0EF00 (150 B): 00 01 02 …
MID 128 Engine #1, PID 84 Road Speed 60 mph, PID 190 Engine Speed 1500 rpm
NM node 0x12 [ActiveWakeup] PN requests #0, #2
SecOC: authentic 11 22 33 44 | freshness 05 | MAC AABBCCDD
XCP SHORT_UPLOAD 2 @ 0x00001000 ext 0
CCP CRM #7 access locked (seed & key required)
```

With a [diagnostic description](diagnostics.md#diagnostic-descriptions-cdd-odxpdx), UDS (and LIN diagnostics) decode
ReadDataByIdentifier data into fields.

![A J1939 connection: parameter groups, transport protocol sessions and DM1 trouble codes decoded in the Info column](../assets/screenshots/protocol-decoding.png)

## J1939-22 assurance data

A parameter group in a J1939-22 Multi-PG frame can carry **assurance data** after its data: cybersecurity data
(SAE J1939-91C secure messaging) and/or functional safety data. Its header says which, with the *type of service*
(TOS) and *trailer format* (TF):

| TOS | TF | Trailer |
|---|---|---|
| 1 | 1 | 32-bit manufacturer specific cybersecurity assurance data |
| 1 | 2 | 32-bit manufacturer specific functional safety assurance data |
| 1 | 3 | 32-bit cybersecurity followed by 32-bit functional safety assurance data |
| 1 | 5 | 64-bit manufacturer specific cybersecurity assurance data |
| 1 | 6 | 64-bit manufacturer specific functional safety assurance data |
| 2 | 0 | No assurance data (plain J1939) |
| others | | Reserved: the payload is shown raw, not interpreted |

The trailer is the end of the payload, so the **J1939-22** decoder splits it off, decodes the parameter group's data
like any other, and shows the trailer after it (here checked with the test key 00 01 … 0F):

```text
PGN 0x02500 Multi-PG SA=0x02 DA=0xFF P6: 1 PG: PGN 0x0FEF1 CCVS1: 01 02 03 04 [64-bit OEM cybersecurity assurance data: cybersecurity 000000C8ED035D51; tag OK, FV 100]
```

### Checking the trailers

The contents of the trailers are manufacturer specific, and SAE J1939-22 and J1939-91C are not public, so CANExplorer
checks them only for the parameter groups you configure, with the layout you give. A wrong layout cannot pass —
a CRC or a MAC does not match by accident — so it is reported as *does not verify with this configuration*, not as
forged data.

- **Functional safety: AUTOSAR E2E Profile 22** — an 8-bit CRC and a 4-bit counter, as AUTOSAR stacks and
  J1939-22 tool chains use for these trailers. You give the 16 Data IDs, the byte offset of the Profile 22 header
  (CRC byte, then the counter in the low nibble of the next byte) inside the safety trailer, and the largest
  accepted counter step. The protected data is the whole payload (data and trailers), the CRC covering everything
  but itself (CANExplorer's choice). Shown: `E2E P22 OK, counter 5`, lost, `REPEATED` or `WRONG SEQUENCE`.
- **Cybersecurity: J1939-91C secure messages** — the trailer holds the freshness value FV, the encryption flag E and
  a truncated AES-CMAC tag over `((E << 23) | PGN) ‖ SA ‖ FV ‖ data`. You give the key (a *SecOC key* of the
  [security manager](security-manager.md) as `keystore:<name>`, or hex) and the widths of FV and the tag in bits
  (with E they fill the trailer, most significant bit first; the nonce fields are packed big endian — a choice, as
  the widths are not public). Shown: `tag OK, FV 100`, a tag that does not verify, or a freshness value that does
  not increase (`replayed`). Encrypted payloads (E = 1) are labelled and not checked: the cipher is not public.

The configuration is a JSON file, `j1939_assurance.json` next to the key store (in the application data folder),
or the file named by the `CANEXPLORER_J1939_ASSURANCE` environment variable. It is read when the J1939-22 decoder
is chosen (in the trace, the MCP server or a script):

```json
{
  "pgs": [
    {
      "pgn": "0x0F004",
      "name": "EEC1",
      "safety": {"profile": 22, "data_ids": ["0x01", "0x02", "0x03", "0x04", "0x05", "0x06", "0x07", "0x08",
                                             "0x09", "0x0A", "0x0B", "0x0C", "0x0D", "0x0E", "0x0F", "0x10"],
                 "offset": 0, "max_delta": 1}
    },
    {
      "pgn": "0x0FEF1",
      "source": "0x02",
      "security": {"key": "keystore:Network key", "freshness_bits": 31, "tag_bits": 32}
    }
  ]
}
```

`source` limits a configuration to one sender (otherwise any). Keys from the key store need it unlocked (the MCP
server unlocks it with `CANEXPLORER_KEYSTORE_PASSWORD`); a locked store shows `tag not checked`. In scripts,
`can_explorer.core.j1939_assurance` has the same checks (`AssuranceChecker`) and `protect`, which builds payloads
with valid trailers for simulations.

Sources: the TOS/TF table, the trailer sizes, Profile 22 for safety data and the J1939-91C tag construction are
shown in New Eagle's public *J1939-91C in Practice* presentation (Raptor Innovation Summit 2025), with a bus trace
whose trailer split the tests reproduce; Profile 22 itself (including its offset) follows the AUTOSAR E2E protocol
specification and is tested against its examples.

## Decoding in scripts

The decoders work on any frames, e.g. those of a log file. Each keeps the state of multi-frame transfers, so feed it
the frames in order (and `reset()` it between logs):

```python
import can

from can_explorer.core.protocols import PROTOCOL_DECODERS

uds = PROTOCOL_DECODERS['UDS']()  # The names of the protocol list: 'ISO-TP', 'J1939', 'CANopen', …
with can.LogReader('diagnostics.asc') as log:
    for msg in log:
        text = uds.describe(msg)  # None for frames the decoder has nothing to say about
        if text:
            print(f'{msg.timestamp:8.3f}  {msg.arbitration_id:03X}  {text}')
```

```text
   0.000  7E0  ISO-TP SF (2 B): 10 03 | UDS DiagnosticSessionControl 03
   0.010  7E8  ISO-TP SF (6 B): 50 03 00 32 01 F4 | UDS Positive response to DiagnosticSessionControl: 03 00 32 01 F4
   1.000  7E0  ISO-TP SF (3 B): 22 F1 90 | UDS ReadDataByIdentifier F1 90
   1.010  7E8  ISO-TP FF (total 20 B)
   1.020  7E0  ISO-TP FC CTS BS=0 STmin=0x00
   1.030  7E8  ISO-TP CF sn=1 (13/20 B)
   1.040  7E8  ISO-TP complete (20 B): 62 F1 90 57 44 42 31 32 … | UDS Positive response to ReadDataByIdentifier: F1 90 …
   2.000  7E0  ISO-TP SF (2 B): 27 01 | UDS SecurityAccess 01
   2.010  7E8  ISO-TP SF (3 B): 7F 27 35 | UDS Negative response to SecurityAccess: invalidKey
```

## Limits

- ISO-TP: normal addressing with **ISO-TP**, **UDS** and **KWP2000**; extended and mixed addressing with their
  *(extended)* variants. Pick the one the bus uses: one decoder cannot tell an address byte from an ISO-TP header.
- J1939: names are known for common PGNs and SPNs; signals of J1939 messages are decoded with a DBC. The text of
  identification groups is shown as it is; its fields are not split.
- J1939-22: SAE J1939-22 is not public. The Multi-PG and FD.TP layouts follow the open-source
  [python-can-j1939](https://github.com/juergenH87/python-can-j1939) implementation (its `j1939_22.py`), and the
  tests replay frames that library sends; the parameter group header also agrees with public J1939-22
  presentations. Assurance data trailers: see [J1939-22 assurance data](#j1939-22-assurance-data) for what is
  public and what is configuration. FD.TP transfers announcing assurance data are only labelled (type and size):
  where its bytes travel is not public. Nothing has been checked against a J1939-22 ECU yet.
- NMEA 2000: names for common PGNs, fields for the most common ones; the full PGN catalogue is not built in.
- CANopen: the predefined connection set (default COB-IDs).
- J1587: a subset of MIDs and PIDs is named and the well-known parameters are scaled; others are shown as raw
  bytes. A live J1708 bus is captured through the [J1708 interface](../reference/interfaces.md#sae-j1708); the
  decoder works on each frame's J1587 message bytes.
- K-Line: the named KWP2000 and OBD-II/J1979 services are described; an unrecognised payload is left blank. It
  expects a bare service payload (as the `elm327_kline` interface delivers), not ISO-TP frames.
- VW TP 2.0: the data packets are only recognised after the channel setup response that assigns their identifiers
  (typically 0x300 and 0x740), so start the capture before the tester opens the channel. Broadcast frames (0x23 /
  0x24) are labelled but not expanded, and a refused setup (0xD6–0xD8) carries no reason: TP 2.0 has no public
  specification, and the [community description](https://jazdw.net/tp20) it follows documents neither.
- NM: AUTOSAR NM (the PDU shared by CAN-NM and UDP-NM); the older OSEK/VDX direct NM is not decoded.
- SecOC: the decoder shows the secured-PDU structure of any frame, with byte-aligned trailer lengths it is given.
  With an ARXML database, the secured PDUs it describes are checked (MAC and freshness) with the keys of the
  [security manager](security-manager.md#secoc-in-the-trace), whichever decoder is selected.
- XCP, CCP: XCP on CAN or CAN FD (XCP on Ethernet is not CAN traffic). A DTO is labelled with its packet id; its
  values are only known to the master that configured the DAQ lists (the [XCP/CCP page](xcp.md)).
