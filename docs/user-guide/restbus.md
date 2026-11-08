# Restbus simulation

**Restbus simulation** sends the messages of a signal database periodically, so an ECU on the bench sees the rest
of the bus it expects — the other nodes — even though they are not present. It is the counterpart of the
[diagnostics and ECU simulators](simulation.md): where those answer requests, this keeps the periodic bus traffic
alive.

It is a view of a connection that can transmit: open **Restbus** in the connection's view list in the sidebar.

## Using it

1. **Open a connection** that can transmit and load a signal database (DBC, ARXML, KCD, SYM or DBF) on it, as for
   [signal decoding](signal-databases.md). The **Restbus** view then lists the database's messages with their
   identifier, name, sending node, cycle time and signal count. (Load or change the database at any time — the view
   follows it.)
2. Every message that has a cycle time (the DBC `GenMsgCycleTime`, or the ARXML cycle) is **enabled** by default;
   event-only messages are listed but off. Tick or untick the **Send** box to choose what to transmit, or use
   **All**, **None** and **Cyclic only**.
3. To simulate the rest of the bus around **the ECU you are testing**, pick that ECU next to **Exclude** and press
   **Exclude node**: every message that node sends is switched off, leaving the messages of the other nodes.
4. Press **Start**. Each enabled message is transmitted at its cycle time; messages without one use the
   **Default cycle** (100 ms unless you change it). The frames appear in the connection's trace like any other
   transmitted frame. Press **Stop** to end.

## Configuring the sent values

The payload of each message is encoded from its signals' initial values (or their minimum, or zero). Click a
message's **signal count** to expand it and edit those values; the change takes effect on the next transmission,
including while the simulation is running.

## Limits

- Message **counters and E2E/CRC checksums are not computed**: each signal keeps the value it was given, so a
  receiver that validates a rolling counter or a checksum will see it constant. Use the
  [scripting console](simulation.md) when you need a changing counter or a computed checksum.
- A multiplexed or container message whose default values do not form a valid layout is sent as all-zero bytes of
  the right length, so the identifier is still present on the bus.
- Transmission needs a connection that is connected and can send (not a replayed log file).
