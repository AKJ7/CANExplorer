# SOME/IP

**SOME/IP** (Scalable service-Oriented MiddlewarE over IP) is the service-oriented middleware of automotive
Ethernet. The **SOME/IP** tool listens on UDP, discovers the services offered on the network through SOME/IP-SD
(Service Discovery) and shows the decoded messages.

![SOME/IP: discovered services and decoded messages](../assets/screenshots/someip.png)

## Listening

Enter the **UDP ports** to listen on (comma separated; `30490` is the usual Service Discovery port) and the
**SD multicast** group to join (the ASAM default is `224.244.224.245`; leave it empty to listen on unicast only),
then **Listen**. **Stop** closes the sockets and **Clear** empties the tables.

!!! note "What a socket sees"
    A plain UDP socket receives the multicast groups it joins and the traffic addressed to this host. To capture
    *all* SOME/IP on the wire — unicast between other ECUs — the computer needs to sit on a network tap or a
    switch mirror port; passive packet capture is not done by the tool itself.

## Services

Every service announced with **OfferService** in Service Discovery is listed with its **Service** and **Instance**
IDs, its **Version** (major.minor), the **Endpoints** it offers (address, port and transport), and how many offers
have been seen. A service whose offer expires (TTL 0, **StopOfferService**) is removed.

## Messages

Each SOME/IP message is shown with the time (relative to the first message), the **Source** (sender address and
port), the **Service.Method** ID, the message **Type**, the payload length and a decoded **Description**:

- **REQUEST**, **REQUEST_NO_RETURN**, **RESPONSE**, **ERROR** (with the return code) and **NOTIFICATION** (events,
  whose method id has the top bit set), and their segmented `_TP` variants.
- **SOME/IP-SD** messages (service `FFFF`, method `8100`) are expanded into their entries: OfferService,
  FindService, SubscribeEventgroup and the acknowledgements, with the service, version, endpoints and TTL.

## Decoding payloads (FIBEX / ARXML)

Load a **FIBEX** or AUTOSAR **ARXML** file (**FIBEX / ARXML…**) describing the SOME/IP service interfaces to decode
the message payloads into named signals: the **Description** column then shows the decoded values (for example
`speed=4000` or `status.flags=3, status.temp=-15`) instead of the payload length. A method's request and response
are decoded with their own parameters. The format is chosen by the file extension (`.arxml` for ARXML, otherwise
FIBEX).

A subset of each is read: service interfaces with their methods (input and return parameters) and events, and the
data types they use — scalars, fixed and dynamic arrays, length-prefixed and fixed strings, nested structures and
**unions**. From ARXML the SOME/IP service/method/event ids come from the `SOMEIP-SERVICE-INTERFACE-DEPLOYMENT`
elements, which reference the service interfaces.

**Optional (TLV) members** are decoded: where a FIBEX member carries a SOME/IP **data ID**, the struct is read as
tag/length/value, so members may be absent, arrive out of order, or be unknown to the description (unknown ones are
skipped). The decoder also handles **unions** (a length, a type selector, then the selected member) and **bit
fields** (several named sub-fields packed into one integer); unions are read from FIBEX and ARXML, while bit fields
and ARXML TLV data IDs are supported by the decoder for scripts but not auto-derived from the description files.

The live SOME/IP tool reads UDP. SOME/IP carried over **TCP** (reassembled across segments) and **SOME/IP-TP**
segmented messages are decoded when you open a capture in the [Ethernet capture](ethernet.md) tool.

## End-to-end protection (E2E)

AUTOSAR **E2E** protection guards a message against corruption, loss, repetition and reordering with a CRC, a
sequence counter, a length and a Data ID in an E2E header. **E2E…** in the toolbar configures which methods and
events are protected; their messages are then checked as they arrive, and the result is added to the description:

```text
Speed: VehicleSpeed=88 [E2E P04 ok, counter 41]
Speed: VehicleSpeed=88 [E2E P04 ok, counter 44 (2 lost)]
Speed: VehicleSpeed=88 [E2E P04 REPEATED, counter 44]
Speed: VehicleSpeed=88 [E2E P04 WRONG SEQUENCE, counter 60 (after 44)]
Speed: VehicleSpeed=91 [E2E P04 CRC ERROR 0x1F2E3D4C, expected 0x9A8B7C6D, counter 61]
Speed: VehicleSpeed=91 [E2E P04 ERROR: length field 22, data 23 bytes]
```

Failed checks are shown in red. Each protection gives:

| Setting | Meaning |
|---|---|
| Service, Method / event | The SOME/IP ids (hex), e.g. `1234` and `8001` for an event |
| Profile | **4** (CRC-32, 16-bit counter, 32-bit Data ID), **6** (CRC-16, 8-bit counter, 16-bit Data ID, not sent) or **7** (CRC-64, 32-bit counter, 32-bit Data ID) — the profiles AUTOSAR defines for SOME/IP and other large data |
| Data ID | The Data ID of the protected data (hex) |
| Offset (bits) | Where the E2E header starts in the protected data; 64 (the default) is right after the SOME/IP header |
| Max counter Δ | The largest counter step accepted between two messages: 1 means every message must be received; a bigger step is a *wrong sequence*, a step up to it is *ok* with the number of lost messages |

As in the AUTOSAR E2E protocol specification, the protected data of a SOME/IP message starts at its Request ID:
the Client ID, Session ID, protocol and interface version, message type and return code (the last 8 bytes of the
SOME/IP header), followed by the payload, so the CRC covers those header fields too. The Message ID and the
SOME/IP Length are not protected; the E2E Length field covers the protected data. The counters are tracked per
method or event and client, and start over when the messages are cleared. SOME/IP-TP messages are checked once
reassembled.

The protections are kept between sessions, shared with the [Ethernet capture](ethernet.md) tool (which checks them
in captures too), and can be exported to and imported from a JSON file:

```json
{"protections": [
  {"service_id": "0x1234", "method_id": "0x8001", "profile": 4, "data_id": "0x0A0B0C0D",
   "offset": 64, "max_delta_counter": 1, "name": "VehicleSpeed"}
]}
```

The profiles are verified against the protocol examples of the AUTOSAR E2E Protocol Specification (R23-11), with
and without the SOME/IP header in front. The protections of signal PDUs in an ARXML system description are checked
live in the trace of a connection, see [ARXML end-to-end protection](signal-databases.md#arxml-end-to-end-protection-e2e).

Limitations:

- The protections of SOME/IP messages are not read from ARXML. There, E2E for SOME/IP goes through the E2E
  transformer of the ISignals serialized into the PDU and the PDU's message id through the socket connection's PDU
  identifiers, a chain CANExplorer does not resolve yet; enter the protections instead.
- Profiles 1, 2, 5, 11 and 22 (made for CAN-sized PDUs) and the method profiles 4m and 7m are not offered for
  SOME/IP. Scripts can use all the implemented profiles in `can_explorer.core.e2e` (2, 4, 5, 6, 7 and 22) and
  the checker in `can_explorer.core.someip_e2e`.
