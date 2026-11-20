"""comma.ai pandas over USB (red panda, and the panda inside comma three/3X and comma four): a python-can bus
(interface "panda", class PandaBus) with CAN and CAN FD on the panda's three buses.

EXPERIMENTAL: written from panda's open source (commaai/panda master, November 2026: python/__init__.py,
board/main_comms.h, board/main.c, board/can_comms.h, board/health.h, board/stm32h7/llfdcan.h; and the packet and
safety definitions of commaai/opendbc, opendbc/safety/can.h and declarations.h) and tested against a simulated
device, not yet on a panda. Needs pyusb and libusb (`uv sync --extra drivers`).

The channel is the panda's USB serial number, or empty for the first panda, optionally followed by a bus: `@1`
(bus 1 only), `0123456789abcdef01234567@2`. Without a bus all three buses are received, each with its bus number as
the message's channel, and a message is sent on its own channel (bus 0 when it has none). The bitrate (and CAN FD
data bitrate) is set on the chosen bus, or on all buses.

USB protocol: vendor 0xBBAA or 0x3801, product 0xDDCC (0xDDEE is the bootstub, not used); control requests (type
0x40 out, 0xC0 in) for the settings; bulk endpoint 0x81 receives and 0x03 sends CAN packets: a 6-byte header
(byte 0: length code << 4 | bus << 1 | FD; bytes 1-4 little endian: identifier << 3 | extended << 2 | returned << 1 |
rejected; byte 5: XOR of the header and the data) and the data. Packets may span USB transfers.

Safety: the panda only sends what its safety mode allows. Listen only uses SILENT (the CAN controllers do not
acknowledge). Otherwise ALLOUTPUT (send anything) is asked for; release firmware leaves it out (it needs a debug
build, ALLOW_DEBUG) and stays in SILENT, so the bus then falls back to ELM327 mode, which sends only ISO 15765-4
diagnostic frames (8 bytes, to 0x7DF, 0x6xx, 0x7xx, 0x18DB33F1, 0x18DAxxF1), and logs it. Frames the panda refused
come back marked "rejected" and are reported once in the log. The heartbeat check (which falls back to SILENT
after 2-5 s without the host's heartbeat) is turned off, as panda's own library does outside openpilot.

Packet version: the firmware reports a hash of opendbc/safety/can.h; this module knows the hash of the version it
was written from (KNOWN_CAN_PACKET_VERSIONS). Another hash is logged as a warning and the bus is opened anyway:
each packet's XOR checksum still catches a different layout. Older black and grey pandas (STM32F4) are not
supported by current panda firmware and not by this module. Timestamps are the host's time of arrival.
"""

import queue
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import can

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

USB_VIDS = (0xBBAA, 0x3801)
USB_PID = 0xDDCC
REQUEST_OUT, REQUEST_IN = 0x40, 0xC0
ENDPOINT_IN, ENDPOINT_OUT = 0x81, 0x03
HEADER_SIZE = 6
DLC_TO_LEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
LEN_TO_DLC = {length: dlc for dlc, length in enumerate(DLC_TO_LEN)}
BUS_COUNT = 3
# The hash of opendbc/safety/can.h (sha256, first 4 bytes little endian) the packet layout above was taken from
KNOWN_CAN_PACKET_VERSIONS = frozenset({0x83E460B9})
# Bitrates the firmware accepts (llfdcan.h, in units of 100 bit/s)
BITRATES = (10_000, 20_000, 50_000, 100_000, 125_000, 250_000, 500_000, 1_000_000)
DATA_BITRATES = BITRATES + (2_000_000, 5_000_000)
# Hardware types (python/__init__.py) of the STM32H7 pandas, which have CAN FD
HW_TYPES = {0x07: 'red panda', 0x09: 'tres', 0x0A: 'cuatro', 0xB1: 'body'}
# Safety modes (opendbc/safety/declarations.h)
SAFETY_SILENT, SAFETY_ELM327, SAFETY_ALLOUTPUT, SAFETY_NOOUTPUT = 0, 3, 17, 19
SAFETY_NAMES = {SAFETY_SILENT: 'SILENT', SAFETY_ELM327: 'ELM327', SAFETY_ALLOUTPUT: 'ALLOUTPUT', 19: 'NOOUTPUT'}
# Control requests (board/main_comms.h)
REQ_CAN_RESET_COMMUNICATIONS, REQ_GET_HW_TYPE, REQ_HEALTH, REQ_GET_VERSION = 0xC0, 0xC1, 0xD2, 0xD6
REQ_SET_SAFETY_MODE, REQ_GET_PACKET_VERSIONS, REQ_SET_CAN_SPEED = 0xDC, 0xDD, 0xDE
REQ_SET_POWER_SAVE, REQ_SET_CANFD_AUTO, REQ_CAN_CLEAR = 0xE7, 0xE8, 0xF1
REQ_DISABLE_HEARTBEAT, REQ_SET_CAN_DATA_SPEED, REQ_SET_CANFD_NON_ISO = 0xF8, 0xF9, 0xFC
HEALTH_SAFETY_MODE_OFFSET = 31  # health_t: uptime 4, voltage 2, current 2, 5 counters of 4, flags 2, harness 1
RX_SIZE = 16384
USB_TIMEOUT_MS = 100
QUEUE_SIZE = 50_000
_CLOSED = object()


@dataclass
class PandaPacket:
    address: int
    data: bytes
    bus: int
    extended: bool = False
    fd: bool = False
    returned: bool = False
    rejected: bool = False


def checksum(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


def pack_packet(packet: PandaPacket) -> bytes:
    if len(packet.data) not in LEN_TO_DLC:
        raise can.CanOperationError(f'A panda cannot send {len(packet.data)} data bytes (CAN FD lengths only)')
    word = (packet.address << 3) | (int(packet.extended) << 2) | (int(packet.returned) << 1) | int(packet.rejected)
    header = bytearray(HEADER_SIZE)
    header[0] = (LEN_TO_DLC[len(packet.data)] << 4) | (packet.bus << 1) | int(packet.fd)
    header[1:5] = word.to_bytes(4, 'little')
    header[5] = checksum(bytes(header[:5]) + packet.data)
    return bytes(header) + packet.data


class PacketParser:
    """Splits the received bytes into packets, keeping a packet that continues in the next transfer."""

    def __init__(self):
        self._buffer = b''
        self.checksum_errors = 0

    def feed(self, data: bytes) -> List[PandaPacket]:
        buffer = self._buffer + bytes(data)
        packets = []
        while len(buffer) >= HEADER_SIZE:
            length = DLC_TO_LEN[buffer[0] >> 4]
            if len(buffer) < HEADER_SIZE + length:
                break
            raw, buffer = buffer[: HEADER_SIZE + length], buffer[HEADER_SIZE + length :]
            if checksum(raw) != 0:
                # Out of step (or a different packet layout): drop what is buffered, as panda's library would fail
                self.checksum_errors += 1
                buffer = b''
                break
            word = int.from_bytes(raw[1:5], 'little')
            packets.append(
                PandaPacket(
                    address=word >> 3,
                    data=raw[HEADER_SIZE:],
                    bus=(raw[0] >> 1) & 0x7,
                    extended=bool(word & 0x4),
                    fd=bool(raw[0] & 0x1),
                    returned=bool(word & 0x2),
                    rejected=bool(word & 0x1),
                )
            )
        self._buffer = buffer
        return packets

    def reset(self) -> None:
        self._buffer = b''


def parse_channel(channel: Any) -> Tuple[str, Optional[int]]:
    """'serial@1' → ('serial', 1); '@2' → ('', 2); '' → ('', None)."""
    text = str(channel if channel is not None else '').strip()
    serial, separator, bus = text.rpartition('@')
    if separator:
        if not bus.isdigit() or int(bus) >= BUS_COUNT:
            raise ValueError(f'A panda has buses 0 to {BUS_COUNT - 1}, got {bus!r}')
        return serial.strip(), int(bus)
    return text, None


def _usb():
    import usb.core  # pyusb, an optional dependency
    import usb.util

    return usb


def _usb_backend():
    """libusb from the libusb-package wheel where installed (Windows, macOS), else the system's."""
    try:
        import libusb_package
    except ImportError:
        return None
    return libusb_package.get_libusb1_backend()


def find_pandas() -> List[Any]:
    try:
        usb = _usb()
    except ImportError:
        return []
    backend = _usb_backend()
    devices = []
    for vendor in USB_VIDS:
        try:
            devices += list(usb.core.find(find_all=True, idVendor=vendor, idProduct=USB_PID, backend=backend))
        except Exception:  # noqa: BLE001 - no libusb: no devices
            return []
    return devices


def device_serial(device: Any) -> str:
    try:
        return str(device.serial_number or '')
    except Exception:  # noqa: BLE001 - no permission to read the string descriptor
        return ''


class PandaBus(can.BusABC):
    """One or all CAN buses of a panda on USB. `device` replaces the pyusb device (tests)."""

    def __init__(
        self,
        channel: Any = None,
        bitrate: Optional[int] = None,
        receive_own_messages: bool = False,
        listen_only: bool = False,
        fd: bool = False,
        data_bitrate: Optional[int] = None,
        device: Any = None,
        queue_size: int = QUEUE_SIZE,
        **kwargs: Any,
    ):
        try:
            self.serial, self.bus = parse_channel(channel)
        except ValueError as e:
            raise can.CanInitializationError(str(e)) from e
        for rate, allowed, name in ((bitrate, BITRATES, 'bitrate'), (data_bitrate, DATA_BITRATES, 'data bitrate')):
            if rate is not None and rate not in allowed:
                raise can.CanInitializationError(
                    f'The panda supports the {name}s {", ".join(f"{value // 1000} kbit/s" for value in allowed)}'
                )
        self.receive_own_messages = receive_own_messages
        self.listen_only = listen_only
        self.fd = fd
        self.dropped = 0
        self.rejected = 0
        self._parser = PacketParser()
        self._queue: queue.Queue = queue.Queue(max(1, queue_size))
        self._closed = False
        self._write_lock = threading.Lock()
        self._device = device if device is not None else self._find()
        self._claimed = False
        try:
            self._setup(bitrate, data_bitrate)
        except BaseException:
            self._release()
            raise
        self._reader = threading.Thread(target=self._read, name=f'PandaBus {self.serial}', daemon=True)
        self._reader.start()
        super().__init__(channel=channel, **kwargs)

    # USB

    def _find(self) -> Any:
        try:
            _usb()
        except ImportError as e:
            raise can.CanInterfaceNotImplementedError(
                'The panda interface needs pyusb and libusb: run from source with `uv sync --extra drivers`'
            ) from e
        devices = find_pandas()
        for device in devices:
            if not self.serial or device_serial(device) == self.serial:
                return device
        found = ', '.join(device_serial(device) or '?' for device in devices) or 'none'
        wanted = f'panda {self.serial}' if self.serial else 'panda'
        raise can.CanInitializationError(f'No {wanted} on USB (found: {found})')

    def _control_out(self, request: int, value: int = 0, index: int = 0) -> None:
        self._device.ctrl_transfer(REQUEST_OUT, request, value, index, b'', USB_TIMEOUT_MS * 10)

    def _control_in(self, request: int, length: int, value: int = 0, index: int = 0) -> bytes:
        return bytes(self._device.ctrl_transfer(REQUEST_IN, request, value, index, length, USB_TIMEOUT_MS * 10))

    def _setup(self, bitrate: Optional[int], data_bitrate: Optional[int]) -> None:
        try:
            if hasattr(self._device, 'set_configuration'):
                try:
                    if self._device.is_kernel_driver_active(0):
                        self._device.detach_kernel_driver(0)
                except (NotImplementedError, AttributeError):
                    pass
                except Exception:  # noqa: BLE001 - Windows/macOS have no kernel driver to detach
                    pass
                self._device.set_configuration()
            if hasattr(self._device, 'claim_interface'):
                self._device.claim_interface(0)  # pyusb claims on first use otherwise; explicit is clearer
            self._claimed = True
            self.serial = self.serial or device_serial(self._device)
            hw_type = self._control_in(REQ_GET_HW_TYPE, 0x40)[:1]
            self.hw_type = hw_type[0] if hw_type else 0
            self.version = self._control_in(REQ_GET_VERSION, 0x40).split(b'\0', 1)[0].decode(errors='replace')
            versions = self._control_in(REQ_GET_PACKET_VERSIONS, 8)
            self.health_version, self.can_version = struct.unpack('<II', versions) if len(versions) == 8 else (0, 0)
        except can.CanError:
            raise
        except Exception as e:  # noqa: BLE001 - USB errors of pyusb
            raise can.CanInitializationError(f'Could not open the panda {self.serial}: {e}') from e
        if self.hw_type not in HW_TYPES:
            raise can.CanInitializationError(
                f'Panda hardware type 0x{self.hw_type:02X} is not supported (red panda, comma three/3X/four only)'
            )
        if self.can_version not in KNOWN_CAN_PACKET_VERSIONS:
            logger.warning(
                'panda %s: CAN packet version 0x%08X is not the one this interface was written for; if frames go '
                'missing, update the panda firmware',
                self.serial,
                self.can_version,
            )
        buses = range(BUS_COUNT) if self.bus is None else (self.bus,)
        try:
            self._control_out(REQ_DISABLE_HEARTBEAT)  # Allowed outside car safety modes, before setting one
            self._control_out(REQ_SET_POWER_SAVE, 0)
            self._set_safety_mode()
            self._control_out(REQ_CAN_RESET_COMMUNICATIONS)
            for bus in buses:
                self._control_out(REQ_SET_CANFD_AUTO, bus, 0)
                if bitrate:
                    self._control_out(REQ_SET_CAN_SPEED, bus, bitrate // 100)
                if self.fd:
                    self._control_out(REQ_SET_CAN_DATA_SPEED, bus, (data_bitrate or bitrate or 500_000) // 100)
            self._control_out(REQ_CAN_CLEAR, 0xFFFF)  # The receive queue, so old frames are not taken as new
        except Exception as e:  # noqa: BLE001 - USB errors of pyusb
            raise can.CanInitializationError(f'Could not configure the panda {self.serial}: {e}') from e
        bus_text = 'all buses' if self.bus is None else f'bus {self.bus}'
        self.channel_info = (
            f'panda {HW_TYPES[self.hw_type]} {self.serial} {bus_text} ({SAFETY_NAMES.get(self.safety_mode, "?")})'
        )

    def _safety_mode(self) -> Optional[int]:
        health = self._control_in(REQ_HEALTH, 0x40)
        return health[HEALTH_SAFETY_MODE_OFFSET] if len(health) > HEALTH_SAFETY_MODE_OFFSET else None

    def _set_safety_mode(self) -> None:
        if self.listen_only:
            self._control_out(REQ_SET_SAFETY_MODE, SAFETY_SILENT, 0)
            self.safety_mode = SAFETY_SILENT
            return
        self._control_out(REQ_SET_SAFETY_MODE, SAFETY_ALLOUTPUT, 0)
        mode = self._safety_mode()
        if mode == SAFETY_ALLOUTPUT:
            self.safety_mode = SAFETY_ALLOUTPUT
            return
        # Release firmware: no ALLOUTPUT. ELM327 mode with parameter 1 keeps the buses as they are (0 would switch
        # bus 1 to the OBD-II port's CAN lines).
        self._control_out(REQ_SET_SAFETY_MODE, SAFETY_ELM327, 1)
        self.safety_mode = self._safety_mode() if mode is not None else SAFETY_ELM327
        logger.warning(
            'panda %s: the firmware has no ALLOUTPUT safety mode (a debug build is needed); using %s, which sends '
            'only OBD/UDS diagnostic frames',
            self.serial,
            SAFETY_NAMES.get(self.safety_mode, self.safety_mode),
        )

    def _release(self) -> None:
        if self._claimed:
            try:
                usb = _usb()
                usb.util.release_interface(self._device, 0)
                usb.util.dispose_resources(self._device)
            except Exception:  # noqa: BLE001 - best effort; tests use a fake device
                pass
            self._claimed = False

    # Receiving

    def _put(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 10_000 == 0:
                logger.warning('%s: receive queue full, dropped %d frames', self.channel_info, self.dropped)

    def _messages(self, packets: Iterable[PandaPacket]) -> Iterable[can.Message]:
        now = time.time()
        for packet in packets:
            if self.bus is not None and packet.bus != self.bus:
                continue
            if packet.rejected:
                self.rejected += 1
                if self.rejected == 1:
                    logger.warning(
                        'panda %s: the %s safety mode refused to send 0x%X',
                        self.serial,
                        SAFETY_NAMES.get(self.safety_mode, self.safety_mode),
                        packet.address,
                    )
                continue
            if packet.returned and not self.receive_own_messages:
                continue
            yield can.Message(
                timestamp=now,
                arbitration_id=packet.address,
                is_extended_id=packet.extended,
                is_fd=packet.fd,
                bitrate_switch=packet.fd,
                data=packet.data,
                channel=packet.bus,
                is_rx=not packet.returned,
            )

    def _read(self) -> None:
        error = None
        try:
            while not self._closed:
                try:
                    data = self._device.read(ENDPOINT_IN, RX_SIZE, USB_TIMEOUT_MS)
                except Exception as e:  # noqa: BLE001 - pyusb raises USBTimeoutError when nothing came
                    if self._closed:
                        break
                    if 'timeout' in type(e).__name__.lower() or 'timed out' in str(e).lower():
                        continue
                    raise
                errors = self._parser.checksum_errors
                for message in self._messages(self._parser.feed(bytes(data))):
                    self._put(message)
                if self._parser.checksum_errors != errors and self._parser.checksum_errors == 1:
                    logger.warning('panda %s: a CAN packet had a wrong checksum; resynchronising', self.serial)
        except Exception as e:  # noqa: BLE001 - the panda was unplugged
            error = e
        if not self._closed:
            self._put(can.CanOperationError(f'Connection to the panda {self.serial} lost: {error}'))
        self._put(_CLOSED)

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        try:
            item = self._queue.get(timeout=timeout) if timeout is None or timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None, False
        if item is _CLOSED:
            self._put(_CLOSED)
            return None, False
        if isinstance(item, Exception):
            raise item
        return item, False

    # Sending

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        if self.listen_only:
            raise can.CanOperationError(f'{self.channel_info} is listen only (SILENT safety mode)')
        if msg.is_remote_frame or msg.is_error_frame:
            raise can.CanOperationError('A panda sends data frames only')
        if msg.is_fd and not self.fd:
            raise can.CanOperationError('Turn on CAN FD for the connection to send CAN FD frames')
        if not msg.is_fd and msg.dlc > 8:
            raise can.CanOperationError(f'Classic CAN frames have up to 8 bytes, got {msg.dlc}')
        bus = self.bus if self.bus is not None else msg.channel if isinstance(msg.channel, int) else 0
        if not 0 <= bus < BUS_COUNT:
            raise can.CanOperationError(f'A panda has buses 0 to {BUS_COUNT - 1}, got {bus}')
        packet = pack_packet(
            PandaPacket(msg.arbitration_id, bytes(msg.data[: msg.dlc]), bus, msg.is_extended_id, msg.is_fd)
        )
        wait = USB_TIMEOUT_MS if timeout is None else max(1, int(timeout * 1000))
        try:
            with self._write_lock:
                written = 0
                while written < len(packet):
                    written += self._device.write(ENDPOINT_OUT, packet[written:], wait)
        except Exception as e:  # noqa: BLE001 - USB errors and timeouts (the panda's send queue is full)
            raise can.CanOperationError(f'Could not send to the panda {self.serial}: {e}') from e

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader.join(1)
        try:
            self._control_out(REQ_SET_SAFETY_MODE, SAFETY_SILENT, 0)  # Leave the panda quiet
        except Exception:  # noqa: BLE001 - already unplugged
            pass
        self._release()
        super().shutdown()

    @staticmethod
    def _detect_available_configs() -> List[Dict[str, Any]]:
        return [{'interface': 'panda', 'channel': device_serial(device)} for device in find_pandas()]
