# Remote buses

`can-server` shares the CAN interfaces of one computer, e.g. a Raspberry Pi with a CAN hat in a vehicle or on a
test bench, with CANExplorer on other computers in the network. CANExplorer then works with the remote bus like
with a local one: trace, recording, transmitting, diagnostics, simulation and tests.

## Running the server

`can-server` is installed with CANExplorer (from source: `uv run can-server`). Name every bus to share and give its
python-can interface, channel and bitrate:

```bash
can-server --bus vehicle=socketcan:can0:500000 --bus body=socketcan:can1:125000 --token "$(openssl rand -hex 16)"
```

| Option | Description |
|---|---|
| `--bus NAME=INTERFACE:CHANNEL[:BITRATE[:DATA_BITRATE]]` | A bus to share (repeatable); a data bitrate enables CAN FD |
| `--host` | Address to listen on; default `0.0.0.0` (all networks) |
| `--port` | TCP port; default 8765 |
| `--token` | Clients must present this token; also `CAN_SERVER_TOKEN` |
| `-v` | Log every client and bus event |

A bus is opened when the first client connects to it and closed when the last one leaves. Several clients can
use a bus at the same time; frames one of them sends are received by the others.

!!! warning "Security"
    Anyone who can reach the server and knows the token can read and send on the buses. Use a token, keep the
    server in a trusted network (or behind a VPN), and do not expose it to the internet. The connection is not
    encrypted; put a TLS proxy in front of the server and use `wss://` URLs when it crosses untrusted networks.

To start the server with the Pi, a systemd unit such as this one works:

```ini
[Unit]
Description=CAN server
After=network-online.target

[Service]
ExecStartPre=/usr/sbin/ip link set can0 up type can bitrate 500000
ExecStart=/home/pi/.local/bin/can-server --bus vehicle=socketcan:can0:500000
Environment=CAN_SERVER_TOKEN=change-me
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Connecting

In **New connection**, choose the interface **RemoteBus** and enter the channel `HOST:PORT/BUS`, with the token
when the server needs one:

```text
raspberrypi.local:8765/vehicle?token=change-me
```

Enter the bitrate of the remote bus too, for the bus load. Self-reception asks the server to echo the frames you
send. Workspaces and recent connections remember remote connections like local ones.

When the connection to the server is lost, the connection shows the error and stops; **Connect** connects again.
Timestamps are those of the server, so recordings have the timing of the remote bus, not of the network.

The remote bus is a python-can interface (`canexplorer_remote`), so scripts and `can-test` use it as well:

```bash
uv run can-test tests/ecu_tests.py --interface canexplorer_remote --channel "raspberrypi.local:8765/vehicle?token=change-me"
```

`GET http://HOST:PORT/buses` (with `?token=…`) lists the buses of a server with their bitrates and clients.
