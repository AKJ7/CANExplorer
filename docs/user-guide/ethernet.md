# Ethernet capture

The **Ethernet capture** tool opens a **pcap** capture of automotive Ethernet traffic (or captures live) and
decodes the packets down the stack — Ethernet, 802.1Q VLAN, IPv4/IPv6, ARP, UDP/TCP — and recognises the
automotive protocols carried on top: **SOME/IP** (and SOME/IP-SD), **DoIP**, **gPTP** (IEEE 802.1AS) and **AVTP**
(IEEE 1722). It is a standalone tool, separate from the CAN connections.

Where the [SOME/IP](someip.md) and [DoIP](doip.md) tools interpret one protocol live, this one shows *everything*
on an Ethernet segment — so you can see what is actually on the wire, and which protocols.

## Opening a capture

**Open pcap…** reads a capture file. It reads the classic **pcap** format (Ethernet link type); a **pcapng** file
(Wireshark's default) is detected and you are asked to save it as "Wireshark/tcpdump pcap" first.

Each packet is shown with its time, source and destination (IP and port, or MAC for non-IP frames), the highest
recognised **protocol**, length and an **info** summary (for example a SOME/IP method and payload size, a DoIP
payload type, a gPTP message type, or an AVTP stream). The **filter** box narrows the list by any text —
a protocol (`SOME/IP`), an address (`192.168.0.10`) or a port (`30509`) — and the pills above the table show the
**protocol breakdown**. **Export…** saves the packets back to a pcap file.

## Capturing live

On **Linux**, pick an interface and press **Capture** to capture live from it. Raw capture needs privileges
(run as root, or grant the binary `CAP_NET_RAW`), and a full view of the traffic needs a **network tap** or a
**mirror/SPAN port** — a plain host only sees broadcast/multicast and the traffic addressed to it. On other
platforms, capture live with another tool and open the saved pcap here.

## Limits

- Classic pcap only (not pcapng); Ethernet link type.
- Decoding is a summary per packet, not a full field-by-field dissection; load a FIBEX file in the
  [SOME/IP](someip.md) tool to decode SOME/IP payloads into named signals.
- SOME/IP-TP (segmented) reassembly and TCP stream reassembly are not done; each frame is decoded on its own.
