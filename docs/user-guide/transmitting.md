# Transmitting

The transmit panel below the trace and the overview sends frames on live connections.

| Field | Description |
|---|---|
| ID | Identifier in hexadecimal |
| Ext | Extended (29-bit) identifier |
| FD | CAN FD frame with bitrate switch (CAN FD connections) |
| RTR | Remote frame; on LIN connections **Header only** |
| Data | Data bytes in hexadecimal: `01 02 FF`, `0102FF` or `01,02,FF`; up to 8 bytes, 64 for CAN FD |
| Every … ms | Send periodically |
| HW timed | Let the interface schedule periodic frames |

**Send** sends the frame once. With **Every**, **Start** begins a periodic transmission; several can run at the same
time and **Stop all** ends them. Disconnecting stops them as well.

## Application and hardware timing

- By default CANExplorer schedules periodic frames itself. They appear in the trace and in recordings, with a
  timing precision of about a millisecond.
- **HW timed** uses the cyclic transmission of the interface (python-can `send_periodic`), which is more precise
  where the hardware or driver supports it. These frames only appear in the trace when the interface reports
  own frames (*Self-Reception*).

Frames sent by CANExplorer appear in the trace as `Tx`. With *Self-Reception*, they appear when the interface
reports them instead.

For sequences, reactions to received frames and frames built from signal values, use a
[simulation script](simulation.md).
