# Supported interfaces

CANExplorer talks to CAN hardware through [python-can](https://python-can.readthedocs.io/en/stable/interfaces.html)
(version 4.6). Every interface python-can supports can be used; the connection dialog lists them by the names in
the first column. Install the vendor's driver or library for your adapter first; python-can only uses it.

## All interfaces

| In CANExplorer | python-can name | Hardware | Platforms | Driver or library to install | CAN FD |
|---|---|---|---|---|---|
| *SocketcanBus* | `socketcan` | Any adapter with a Linux kernel driver (see [below](#adapters-through-socketcan)), virtual `vcan` buses, LIN through [sllin](../user-guide/lin.md) | Linux | Part of the Linux kernel; set up with `ip link` ([installation](../getting-started/installation.md#setting-up-interfaces)) | ✓ |
| *PcanBus* | `pcan` | PEAK-System PCAN-USB, PCAN-USB FD, PCAN-PCI(e), PCAN-LAN … | Windows, Linux, macOS | PCAN-Basic (Windows, Linux) or [PCBUSB / MacCAN](https://mac-can.com) (macOS) | ✓ |
| *VectorBus* | `vector` | Vector VN1610, VN1630, VN1640, VN5610 … and the Vector virtual channels | Windows | Vector XL Driver Library (`vxlapi64.dll`) and the device driver | ✓ |
| *KvaserBus* | `kvaser` | Kvaser Leaf, U100, Memorator, USBcan … | Windows, Linux | Kvaser CANlib (Windows) or linuxcan (Linux) | ✓ |
| *IXXATBus* | `ixxat` | HMS IXXAT USB-to-CAN, CAN-IB … | Windows | IXXAT VCI 3 or VCI 4 (VCI 4 for CAN FD) | ✓ |
| *EtasBus* | `etas` | ETAS ES581, ES582, ES584, ES891 … | Windows | ETAS BOA (Basic Open API) | ✓ |
| *NeoViBus* | `neovi` | Intrepid Control Systems neoVI, ValueCAN, RAD-Galaxy … | Windows, Linux, macOS | Intrepid's driver and the Python package `python-ics` ¹ | ✓ |
| *NiXNETcanBus* | `nixnet` | National Instruments NI-XNET devices | Windows | NI-XNET driver and the Python package `nixnet` ¹ | ✓ |
| *NicanBus* | `nican` | National Instruments NI-CAN devices (older series) | Windows | NI-CAN driver | – |
| *UcanBus* | `systec` | SYS TEC electronic USB-CANmodul | Windows | SYS TEC USB-CANmodul driver (`usbcan64.dll`) | – |
| *Usb2canBus* | `usb2can` | 8devices USB2CAN (Korlan) | Windows | 8devices USB2CAN driver (`usb2can.dll`); on Linux use *SocketcanBus* | – |
| *IscanBus* | `iscan` | Thorsis isCAN | Windows | isCAN driver (`iscandrv`) | – |
| *NeousysBus* | `neousys` | CAN ports of Neousys industrial computers | Windows, Linux | Neousys WDT_DIO library | – |
| *CANalystIIBus* | `canalystii` | Chuangxin Technology CANalyst-II (and compatible) | Windows, Linux, macOS | libusb and the Python package `canalystii` ¹ | – |
| *CantactBus* | `cantact` | CANtact, CANable with candleLight firmware | Windows, Linux, macOS | The Python package `cantact` ¹ | – |
| *GsUsbBus* | `gs_usb` | Adapters with gs_usb / candleLight firmware (CANable, candleLight, CANtact …) without a kernel driver | Windows, macOS, Linux | libusb (WinUSB on Windows) and the Python package `gs_usb` ¹; on Linux the kernel driver and *SocketcanBus* are simpler | – |
| *slcanBus* | `slcan` | Lawicel CANUSB, CANable with slcan firmware, USBtin, … (serial adapters with the slcan protocol) | Windows, Linux, macOS | None: a serial port (`COM3`, `/dev/ttyACM0`); FD-capable slcan firmware also takes a data bitrate | ✓ |
| *GvretBus* | `gvret` | Devices with SavvyCAN's GVRET protocol: ESP32RET boards, Macchina M2 (M2RET), CANDue (GVRET firmware) | Windows, Linux, macOS | None: a serial port, with `@1` for the device's second bus (`COM5@1`), or `socket://IP:23` for ESP32RET on WiFi (see [below](#gvret-devices)) | – |
| *J2534Bus* | `j2534` | J2534 pass-thru devices: Drew Technologies Mongoose and CarDAQ, Tactrix OpenPort 2.0, J2534 drivers of Bosch, Autel, Kvaser, PEAK, Intrepid … (see [below](#j2534-pass-thru-devices)) | Windows | The device's J2534 (04.04) driver; 32-bit drivers run in the J2534 bridge ([below](#32-bit-drivers)) | – |
| *Elm327Bus* | `elm327` | OBD adapters with the ELM327 command set: OBDLink MX+, LX, SX, EX and ELM327 compatibles, over USB, Bluetooth or WiFi; made for diagnostics, with limits (see [below](#elm327-and-obdlink-adapters)) | Windows, Linux, macOS | None: a serial port (`COM7`, `/dev/rfcomm0`), or `socket://IP:35000` for WiFi adapters | – |
| *SerialBus* | `serial` | Adapters speaking python-can's own serial protocol | Windows, Linux, macOS | None: a serial port | – |
| *robotellBus* | `robotell` | Robotell USB-CAN adapters | Windows, Linux, macOS | None: a serial port | – |
| *SeeedBus* | `seeedstudio` | Seeed Studio USB-CAN Analyzer | Windows, Linux, macOS | None: a serial port | – |
| *SocketCanDaemonBus* | `socketcand` | A SocketCAN bus on another Linux computer, shared by the [socketcand](https://github.com/linux-can/socketcand) daemon | Windows, Linux, macOS | None on this computer; socketcand on the other | – |
| *RemoteBus* | `canexplorer_remote` | A bus on another computer, shared by CANExplorer's [can-server](../user-guide/remote-buses.md) | Windows, Linux, macOS | None on this computer; can-server on the other | ✓ |
| *UdpMulticastBus* | `udp_multicast` | A software bus between computers or programs over UDP multicast | Windows, Linux, macOS (some socket options only on Linux) | The Python package `msgpack` ¹ | ✓ |
| *VirtualBus* | `virtual` | A software bus inside CANExplorer: all connections with the same channel name share it | Windows, Linux, macOS | None | ✓ |

¹ These Python packages are not installed with CANExplorer and not part of the installers. Run CANExplorer
[from source](../getting-started/installation.md#from-source) with the `drivers` extra, which installs all of them:

```bash
uv sync --extra drivers     # neoVI, NI-XNET, CANalyst-II, CANtact, gs_usb and UDP multicast
uv run canexplorer
```

CAN FD (✓) means the interface can send and receive CAN FD frames with python-can, if the adapter supports them;
enable **Flexible Data (CAN FD)** and the data bitrate in the connection dialog. *VirtualBus* and *RemoteBus*
carry FD frames as they are.

**Detect** in the connection dialog asks every installed driver for its channels and offers them in the channel
field. The drivers of *CANalystIIBus*, *GsUsbBus*, *slcanBus*, *robotellBus*, *SeeedBus*, *IscanBus* and
*NicanBus* cannot list their channels: type the channel (e.g. the serial port) instead. *GvretBus* lists the USB
serial ports, and *Elm327Bus* the USB and Bluetooth serial ports; whether a port has such a device is only known
when it is opened. *J2534Bus* lists the registered J2534 drivers with CAN that CANExplorer can load, itself or
through the J2534 bridge. LIN buses use *SocketcanBus* with a LIN driver; see [LIN](../user-guide/lin.md).

## GVRET devices

*GvretBus* opens one bus of a device running GVRET firmware, the binary protocol of
[SavvyCAN](https://www.savvycan.com/): [ESP32RET](https://github.com/collin80/ESP32RET) boards (e.g. the
A0 / EVTV ESP32 boards), the Macchina M2 with M2RET, and the CANDue with GVRET.

| Channel | Bus |
|---|---|
| `/dev/ttyACM0`, `COM5`, `/dev/cu.usbmodem1101` | The device's first bus (0) on a USB serial port |
| `COM5@1` | The second bus (1) of the same device |
| `socket://192.168.4.1:23` | An ESP32RET board on WiFi (its access point, or its address in your network) |

When the connection opens, the bus is enabled at the connection's bitrate; the device's other bus keeps its
settings. A device is used by one connection at a time (two would read each other's data from the port), so
open one of its buses per device. GVRET devices carry
classic CAN frames: CAN FD and remote frames are not supported. The device does not echo sent frames;
**Receive own messages** adds them to the trace locally. SavvyCAN must not have the device open at the same time.

## J2534 pass-thru devices

*J2534Bus* opens the CAN bus of a vehicle interface through its SAE J2534 (version 04.04) driver, the standard
programming interface of workshop and flashing tools: Drew Technologies Mongoose and CarDAQ, Tactrix OpenPort 2.0,
and the J2534 drivers of Bosch, Autel, Kvaser, PEAK, Intrepid and many others.

The channel is the device's name as its driver registers it (**Detect** lists them), or the path of the driver's
library (`C:\Program Files\Vendor\j2534_64.dll`) for drivers that are not registered.

### 32-bit drivers

CANExplorer is a 64-bit program, and a 64-bit program cannot load a 32-bit driver, which many J2534 drivers are.
These drivers run in the **J2534 bridge**, a small 32-bit program that CANExplorer starts for the connection and
talks to through a pipe (no network port). The connection then shows *(32-bit bridge)*. CANExplorer finds the
bridge:

1. The program or the 32-bit Python named by the environment variable `CANEXPLORER_J2534_BRIDGE`
   (e.g. `C:\Python312-32\python.exe`).
2. `j2534-bridge32\j2534-bridge32.exe` next to CANExplorer: the Windows installer contains it.
3. When run from source, a 32-bit Python installed from [python.org](https://www.python.org/downloads/windows/)
   (*Windows installer (32-bit)*), found by the `py` launcher. It needs no packages.

Without a bridge, **Detect** leaves 32-bit drivers out, and opening one by name says how to get one. Drivers of
other architectures, such as 64-bit drivers for a 32-bit program, are not supported.

The bus runs at the connection's bitrate, if the device supports it, and receives 11 and 29 bit identifiers with
the device's timestamps. **Receive own messages** uses the device's loopback. J2534 04.04 has no listen only mode,
no CAN FD and no error frames. A device is used by one program at a time.

## ELM327 and OBDLink adapters

*Elm327Bus* opens the CAN bus at a vehicle's OBD port through an OBD adapter with the ELM327 command set: the
OBDLink MX+, LX, SX and EX (STN chips) and ELM327 compatible adapters. Use it for
[diagnostics](../user-guide/diagnostics.md), reading OBD data and watching light traffic; for analysing a busy bus,
use a CAN interface.

| Channel | Adapter |
|---|---|
| `COM4`, `/dev/ttyUSB0`, `/dev/cu.usbserial-…` | A USB adapter (OBDLink SX, EX) |
| `COM7`, `/dev/rfcomm0`, `/dev/cu.OBDLinkMXP-STN-SPP` | A Bluetooth adapter (OBDLink MX+, LX), paired with the computer |
| `socket://192.168.0.10:35000` | A WiFi adapter (its address and port) |

**Bluetooth**: pair the adapter in the system's Bluetooth settings first. Windows then creates a serial port
(*Standard Serial over Bluetooth link*, use the outgoing one), macOS a `/dev/cu.…` device. On Linux, bind the
paired adapter to a serial port with `sudo rfcomm bind 0 <adapter address>` and open `/dev/rfcomm0`. Bluetooth Low
Energy adapters, such as the OBDLink CX, are not supported.

The adapter is found at 115200, 38400 or 9600 baud; *serial_baudrate* sets one. It is reset when the connection
opens and runs at the connection's bitrate: 500 kbit/s divided by a whole number (500, 250, 125, 100, 83.3, 50 …
kbit/s, not 1 Mbit/s). ELM327 compatibles without the `AT PB` command support only 500 and 250 kbit/s.

These adapters are made for diagnostics, not as CAN interfaces:

- They print every frame as text, and the serial link carries a few hundred frames per second. On a busy bus frames
  are lost; the adapter reports *BUFFER FULL* in the log. On many vehicles the OBD port is behind a gateway and only
  carries diagnostic traffic.
- Sending a frame interrupts monitoring and takes some tens of milliseconds; frames on the bus in the meantime are
  lost, but the responses to the frame are received. Cyclic transmission at short intervals is not possible.
- Classic CAN data frames with 1 to 8 bytes only: no CAN FD, no remote frames to send, no error frames. Timestamps
  are taken when a frame reaches the computer.
- **Listen only** keeps the adapter from acknowledging frames; sending is then not possible. The adapter does not
  show sent frames; **Receive own messages** adds them to the trace locally.

An adapter is used by one program at a time.

## Adapters through SocketCAN

On Linux, most adapters have kernel drivers and appear as network interfaces (`can0`, `can1` …) that CANExplorer
opens with *SocketcanBus*. Among the drivers in the kernel:

| Kernel driver | Adapters |
|---|---|
| `peak_usb`, `peak_pci`, `peak_pciefd` | PEAK-System PCAN-USB, PCAN-USB FD, PCAN-PCI(e) |
| `kvaser_usb`, `kvaser_pci`, `kvaser_pciefd` | Kvaser USB, PCI and PCIe adapters |
| `gs_usb` | candleLight / gs_usb firmware: CANable, candleLight, CANtact, Geschwister Schneider USB/CAN |
| `ems_usb`, `esd_usb`, `etas_es58x`, `mcba_usb`, `ucan`, `usb_8dev` | EMS, esd, ETAS ES58x, Microchip CAN BUS Analyzer, Theobroma Systems, 8devices adapters |
| `mcp251x`, `mcp251xfd` | Microchip MCP2515 and MCP2517FD/MCP2518FD, e.g. on Raspberry Pi CAN hats |
| `m_can`, `flexcan`, `c_can`, `rcar_canfd` … | CAN controllers built into processors (embedded boards) |
| `slcan` (with `slcand`) | Serial slcan adapters as a network interface |
| `vcan`, `vxcan` | Virtual buses on one computer, and between network namespaces |

Which drivers a distribution includes varies; `ip -details link show can0` shows the driver of an interface.
