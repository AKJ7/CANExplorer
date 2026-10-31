# Ethernet capture

The **Ethernet capture** tool opens a **pcap** or **pcapng** capture of automotive Ethernet traffic (or captures
live) and decodes the packets down the stack — Ethernet, 802.1Q VLAN, IPv4/IPv6, ARP, UDP/TCP — and recognises the
automotive protocols carried on top: **SOME/IP** (and SOME/IP-SD), **DoIP**, **gPTP** (IEEE 802.1AS), **AVTP**
(IEEE 1722) and **LLDP**. It is a standalone tool, separate from the CAN connections.

Where the [SOME/IP](someip.md) and [DoIP](doip.md) tools interpret one protocol live, this one shows *everything*
on an Ethernet segment — so you can see what is actually on the wire, and which protocols.

## Opening a capture

**Open pcap…** reads a capture file. It reads both the classic **pcap** format and **pcapng** (Wireshark's
default), for the Ethernet link type.

Each packet is shown with its time, source and destination (IP and port, or MAC for non-IP frames), the highest
recognised **protocol**, length and an **info** summary (for example a SOME/IP method and payload size, a DoIP
payload type, a gPTP message type, an AVTP stream and sequence number, or an LLDP neighbour). The **filter** box
narrows the list by any text — a protocol (`SOME/IP`), an address (`192.168.0.10`) or a port (`30509`) — and the
pills above the table show the **protocol breakdown**. **Export…** saves the packets back to a pcap file.

**SOME/IP spanning packets is reassembled**: a message carried over **TCP** that is split across segments is
decoded on the segment that completes it (the earlier ones show `[SOME/IP segment]`), and a **SOME/IP-TP**
segmented message is reassembled across its datagrams. When the capture contains Service Discovery, a **SOME/IP
services** card lists the services offered — their service and instance ids, version, endpoints and how many times
each was offered.

## Time sync (gPTP)

When the capture contains **gPTP** (IEEE 802.1AS) traffic, a **Time sync** card summarises the network's time
synchronisation:

- the **grandmaster** — its clock identity, priority and clock quality (for example "synchronized to a primary
  reference") and its time source (GPS, PTP…),
- how many **clocks** and **domains** are present, and the **steps removed** from the grandmaster, and
- the **Sync** and **Announce** intervals the grandmaster advertises, with the **observed Sync rate** measured from
  the capture.

Each gPTP packet in the list also shows its message type (Announce, Sync, Follow_Up, Pdelay…) with the sequence
number, and the grandmaster and clock class for Announce messages.

## Capturing live

On **Linux**, pick an interface and press **Capture** to capture live from it. Raw capture needs privileges
(run as root, or grant the binary `CAP_NET_RAW`), and a full view of the traffic needs a **network tap** or a
**mirror/SPAN port** — a plain host only sees broadcast/multicast and the traffic addressed to it. On other
platforms, capture live with another tool and open the saved pcap here.

## Limits

- pcap and pcapng are read for the **Ethernet** link type only.
- Decoding is a summary per packet, not a full field-by-field dissection; load a FIBEX or ARXML file in the
  [SOME/IP](someip.md) tool to decode SOME/IP payloads into named signals.
- TCP reassembly follows the stream in capture order (a 4 GiB sequence-number wrap within one connection is not
  handled); live capture is Linux only (open a saved pcap/pcapng on other platforms).
