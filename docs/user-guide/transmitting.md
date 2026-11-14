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

## From scripts

[Test scripts](test-automation.md) send with `t.send`, by database message name or as raw data:

```python
from can_explorer.testing import TestModule, testcase


class Stimulus(TestModule):
    @testcase
    async def ramp_engine_speed(self, t):
        for rpm in range(800, 3001, 200):  # A database message by name, every 100 ms
            t.send('EngineData', EngineSpeed=rpm, GearPosition='Second')
            await t.sleep(0.1)

    @testcase
    async def raw_frames(self, t):
        t.send(0x123, [0x01, 0x02])  # Raw data
        t.send(0x18FEF100, bytes(8), extended=True)
        t.send(0x7DF, [0x02, 0x01, 0x0D])
```

```bash
uv run can-test stimulus.py --database vehicle.dbc --interface socketcan --channel can0
```

Outside CANExplorer, python-can sends on the same interfaces, periodically too (the interface's cyclic transmission
where it has one, like **HW timed**):

```python
import time

import can

with can.Bus(interface='socketcan', channel='can0') as bus:
    task = bus.send_periodic(can.Message(arbitration_id=0x123, data=[1, 2], is_extended_id=False), period=0.1)
    time.sleep(1.0)
    task.stop()
    bus.send(can.Message(arbitration_id=0x7DF, data=[0x02, 0x01, 0x0D], is_extended_id=False))
```
