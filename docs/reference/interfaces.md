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
*NicanBus* cannot list their channels: type the channel (e.g. the serial port) instead. LIN buses use *SocketcanBus* with a LIN
driver; see [LIN](../user-guide/lin.md).

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
