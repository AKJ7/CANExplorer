"""The Ethernet capture tool for QML: open a pcap file (or capture live, cross-platform via libpcap/Npcap) and show
the decoded packets (see can_explorer.core.ethernet). Not tied to a CAN connection — it is a standalone network
tool."""

import asyncio
import socket
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import can
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core import ethernet, gptp, log_formats, pcap_live
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_PACKETS = 200_000  # Packets kept in memory
DISPLAY_ROWS = 2000  # Matching rows shown in the table
REFRESH_INTERVAL_MS = 250


class LiveCapture:
    """A non-blocking raw-socket capture (Linux AF_PACKET), delivering decoded frames on the event loop."""

    def __init__(self, interface: str, on_packet: Callable[[float, bytes], None]):
        self._interface = interface
        self._on_packet = on_packet
        self._socket: Optional[socket.socket] = None

    def start(self) -> None:
        if not hasattr(socket, 'AF_PACKET'):
            raise OSError('Live capture needs Linux (AF_PACKET); open a pcap file instead')
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((self._interface, 0))
        sock.setblocking(False)
        asyncio.get_running_loop().add_reader(sock.fileno(), self._read)
        self._socket = sock

    def _read(self) -> None:
        try:
            while True:
                data = self._socket.recv(65535)
                self._on_packet(time.time(), data)
        except BlockingIOError:
            pass
        except OSError as e:
            logger.warning('Live capture read failed: %s', e)
            self.stop()

    def stop(self) -> None:
        if self._socket is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._socket.fileno())
            except (RuntimeError, ValueError):
                pass
            self._socket.close()
            self._socket = None


class PcapLiveCapture:
    """Cross-platform live capture through libpcap/Npcap (can_explorer.core.pcap_live). The capture runs on a
    background thread; frames are handed back to the asyncio loop with call_soon_threadsafe."""

    def __init__(self, interface: str, on_packet: Callable[[float, bytes], None]):
        self._loop = asyncio.get_running_loop()
        self._on_packet = on_packet
        self._capture = pcap_live.PcapCapture(interface, self._deliver)

    def _deliver(self, timestamp: float, data: bytes) -> None:
        # Called from the capture thread; marshal the frame onto the event loop.
        try:
            self._loop.call_soon_threadsafe(self._on_packet, timestamp, data)
        except RuntimeError:
            pass  # The event loop is shutting down

    def start(self) -> None:
        self._capture.start()

    def stop(self) -> None:
        self._capture.stop()


class EthernetController(QObject):
    """Opens pcap files and (on Linux) captures live, decoding automotive Ethernet traffic."""

    changed = pyqtSignal()  # Busy, file, error, capture state
    data_changed = pyqtSignal()  # Packets

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._raw: List[ethernet.RawPacket] = []
        self._decoded: List[ethernet.EthernetPacket] = []
        self._filter = ''
        self._error = ''
        self._status = ''
        self._file = ''
        self._busy = False
        self._capture: Optional[Union[LiveCapture, PcapLiveCapture]] = None
        self._nm_port: Optional[int] = None  # AUTOSAR UDP-NM has no standard port; the user sets it
        self._decoder = ethernet.StreamDecoder(self._nm_port)  # Reassembles SOME/IP over TCP and SOME/IP-TP
        self._base_time: Optional[float] = None
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._flush)
        self._dirty = False

    # State

    @pyqtProperty(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(bool, notify=changed)
    def capturing(self) -> bool:
        return self._capture is not None

    @pyqtProperty(str, notify=changed)
    def file_name(self) -> str:
        return self._file

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._error

    @pyqtProperty(str, notify=changed)
    def status(self) -> str:
        return self._status

    @pyqtProperty('QStringList', constant=True)
    def interfaces(self) -> List[str]:
        # libpcap/Npcap lists the capturable devices on any platform; fall back to the OS interface names.
        return pcap_live.list_interfaces() if pcap_live.available() else ethernet.interfaces()

    @pyqtProperty(str, notify=changed)
    def filter(self) -> str:
        return self._filter

    @pyqtSlot(str)
    def set_filter(self, text: str) -> None:
        self._filter = text.strip()
        self.changed.emit()
        self.data_changed.emit()

    @pyqtProperty(int, notify=changed)
    def nm_port(self) -> int:
        return self._nm_port or 0

    @pyqtSlot(int)
    def set_nm_port(self, port: int) -> None:
        """Set the UDP port carrying AUTOSAR UDP-NM (0 to disable), then re-decode the loaded capture."""
        new_port = port if port > 0 else None
        if new_port == self._nm_port:
            return
        self._nm_port = new_port
        self.changed.emit()
        if self._raw and self._capture is None and not self._busy:
            asyncio.ensure_future(self._redecode())

    async def _redecode(self) -> None:
        """Re-run the decoder over the already-loaded raw packets (after the UDP-NM port changed)."""
        self._busy, self._status = True, 'Re-decoding…'
        self.changed.emit()
        decoder = ethernet.StreamDecoder(self._nm_port)
        raw = self._raw
        decoded = await asyncio.to_thread(
            lambda: [decoder.decode(packet.data, packet.timestamp, index + 1) for index, packet in enumerate(raw)]
        )
        self._decoder, self._decoded = decoder, decoded
        self._status, self._busy = f'{len(decoded):,} packets', False
        self.changed.emit()
        self.data_changed.emit()

    @pyqtSlot(int, result='QVariantList')
    def dissect(self, number: int) -> List[Dict[str, Any]]:
        """The field-by-field dissection of the packet with this number, flattened to rows carrying a tree `depth`,
        a `label` and a `value`, for the details pane."""
        if not 1 <= number <= len(self._raw):
            return []
        raw = self._raw[number - 1]
        tree = ethernet.dissect(raw.data, raw.timestamp, number, self._nm_port)
        rows: List[Dict[str, Any]] = []

        def walk(node: ethernet.DissectionField, depth: int) -> None:
            rows.append({'depth': depth, 'label': node.label, 'value': node.value})
            for child in node.children:
                walk(child, depth + 1)

        walk(tree, 0)
        return rows

    @pyqtProperty(int, notify=data_changed)
    def count(self) -> int:
        return len(self._decoded)

    @pyqtProperty('QVariantList', notify=data_changed)
    def protocols(self) -> List[Dict[str, Any]]:
        counts: Dict[str, int] = {}
        for packet in self._decoded:
            counts[packet.protocol] = counts.get(packet.protocol, 0) + 1
        return [{'protocol': name, 'count': count} for name, count in sorted(counts.items(), key=lambda item: -item[1])]

    @pyqtProperty(bool, notify=data_changed)
    def has_gptp(self) -> bool:
        return any(packet.gptp is not None for packet in self._decoded)

    @pyqtProperty('QVariantMap', notify=data_changed)
    def gptp(self) -> Dict[str, Any]:
        """A time-sync (gPTP / 802.1AS) summary of the capture: grandmaster, clocks, Sync rate and intervals."""
        messages = [packet.gptp for packet in self._decoded if packet.gptp is not None]
        return gptp.summarize(messages).as_dict() if messages else {}

    @pyqtProperty(int, notify=data_changed)
    def can_frame_count(self) -> int:
        """How many CAN frames are tunnelled in AVTP ACF (TSCF/NTSCF) across the capture."""
        return sum(len(packet.can_frames) for packet in self._decoded)

    @pyqtProperty(bool, notify=data_changed)
    def has_avtp_streams(self) -> bool:
        return any(packet.avtp is not None for packet in self._decoded)

    @pyqtProperty('QVariantList', notify=data_changed)
    def avtp_streams(self) -> List[Dict[str, Any]]:
        """Per-stream AVTP health (packets, inferred lost frames, loss %, rate) for the stream-health table."""
        rows = []
        for stat in ethernet.avtp_stream_stats(self._decoded):
            rows.append(
                {
                    'stream': stat.stream_id,
                    'format': stat.subtype_name,
                    'packets': stat.packets,
                    'lost': stat.lost,
                    'loss': f'{stat.loss_percent:.1f}%',
                    'issues': stat.duplicates + stat.reordered,
                    'rate': f'{stat.rate_hz:.0f} Hz' if stat.rate_hz else '',
                }
            )
        return rows

    @pyqtProperty(bool, notify=data_changed)
    def has_someip(self) -> bool:
        return bool(self._decoder.services)

    @pyqtProperty('QVariantList', notify=data_changed)
    def someip_services(self) -> List[Dict[str, Any]]:
        """The SOME/IP services offered in the capture's Service Discovery, for a services table."""
        rows = []
        for service in sorted(self._decoder.services.values(), key=lambda s: s.key):
            rows.append(
                {
                    'service': f'{service.service_id:04X}',
                    'instance': f'{service.instance_id:04X}',
                    'version': f'{service.major_version}.{service.minor_version}',
                    'endpoints': ', '.join(str(endpoint) for endpoint in service.endpoints.values()),
                    'offers': service.offers,
                }
            )
        return rows

    @pyqtProperty('QVariantList', notify=data_changed)
    def packets(self) -> List[Dict[str, Any]]:
        needle = self._filter.lower()
        rows = []
        for packet in self._decoded:
            if needle and not self._matches(packet, needle):
                continue
            rows.append(self._row(packet))
        return rows[-DISPLAY_ROWS:]

    def _matches(self, packet: ethernet.EthernetPacket, needle: str) -> bool:
        fields = (
            packet.protocol,
            packet.src,
            packet.dst,
            packet.info,
            str(packet.src_port or ''),
            str(packet.dst_port or ''),
        )
        return any(needle in field.lower() for field in fields)

    def _endpoint(self, address: str, mac: str, port: Optional[int]) -> str:
        if address:
            return f'{address}:{port}' if port is not None else address
        return mac

    def _row(self, packet: ethernet.EthernetPacket) -> Dict[str, Any]:
        base = self._base_time if self._base_time is not None else packet.timestamp
        return {
            'number': packet.number,
            'time': f'{packet.timestamp - base:.6f}',
            'source': self._endpoint(packet.src, packet.src_mac, packet.src_port),
            'destination': self._endpoint(packet.dst, packet.dst_mac, packet.dst_port),
            'protocol': packet.protocol,
            'length': packet.length,
            'info': packet.info,
            'vlan': packet.vlan if packet.vlan is not None else '',
        }

    # Actions

    @pyqtSlot(QUrl, result=str)
    def open_pcap(self, url: QUrl) -> str:
        """Load and decode a pcap file. Returns an error, or ''."""
        if self._busy:
            return 'Busy'
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        self.stop_capture()
        self._busy, self._error, self._status = True, '', 'Reading…'
        self.changed.emit()
        asyncio.ensure_future(self._open_pcap(path))
        return ''

    async def _open_pcap(self, path: str) -> None:
        decoder = ethernet.StreamDecoder(self._nm_port)
        try:
            raw = await asyncio.to_thread(lambda: list(ethernet.read_pcap(path))[:MAX_PACKETS])
            decoded = await asyncio.to_thread(
                lambda: [decoder.decode(packet.data, packet.timestamp, index + 1) for index, packet in enumerate(raw)]
            )
        except (ethernet.PcapError, OSError) as e:
            self._error, self._status, self._busy = str(e), '', False
            self.changed.emit()
            return
        self._decoder = decoder
        self._raw, self._decoded = raw, decoded
        self._base_time = raw[0].timestamp if raw else None
        self._file = Path(path).name
        self._status = f'{len(decoded):,} packets'
        self._busy = False
        self.changed.emit()
        self.data_changed.emit()

    @pyqtSlot(str, result=str)
    def start_capture(self, interface: str) -> str:
        """Start capturing live on an interface. Uses libpcap/Npcap when available (Linux/macOS/Windows), else the
        Linux AF_PACKET socket. Returns an error, or ''."""
        if self._capture is not None:
            return ''
        if not interface:
            return 'Choose an interface'
        self.clear()
        self._decoder = ethernet.StreamDecoder(self._nm_port)
        capture: Union[LiveCapture, PcapLiveCapture]
        if pcap_live.available():
            capture = PcapLiveCapture(interface, self._on_live_packet)
        elif hasattr(socket, 'AF_PACKET'):
            capture = LiveCapture(interface, self._on_live_packet)
        else:
            return 'Live capture needs libpcap (Linux/macOS) or Npcap (Windows); install it, or open a saved pcap'
        try:
            capture.start()
        except (OSError, PermissionError) as e:
            return f'Could not capture on {interface}: {e} (root/CAP_NET_RAW, BPF access, or Npcap may be needed)'
        self._capture = capture
        self._file = f'live: {interface}'
        self._status = 'Capturing…'
        self._timer.start()
        self.changed.emit()
        return ''

    def _on_live_packet(self, timestamp: float, data: bytes) -> None:
        if len(self._raw) >= MAX_PACKETS:
            return
        if self._base_time is None:
            self._base_time = timestamp
        self._raw.append(ethernet.RawPacket(timestamp, bytes(data)))
        self._decoded.append(self._decoder.decode(bytes(data), timestamp, len(self._raw)))
        self._dirty = True

    @pyqtSlot()
    def stop_capture(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
            self._timer.stop()
            self._status = f'{len(self._decoded):,} packets captured'
            self._flush()
            self.changed.emit()

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.data_changed.emit()

    @pyqtSlot()
    def clear(self) -> None:
        self._raw = []
        self._decoded = []
        self._decoder = ethernet.StreamDecoder(self._nm_port)
        self._base_time = None
        if self._capture is None:
            self._file = ''
            self._status = ''
        self._error = ''
        self.changed.emit()
        self.data_changed.emit()

    @pyqtSlot(QUrl, result=str)
    def export_pcap(self, url: QUrl) -> str:
        """Save the captured packets to a pcap file. Returns a message."""
        if not self._raw:
            return 'Nothing to save'
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        try:
            ethernet.write_pcap(path, self._raw)
        except OSError as e:
            return f'Could not write {Path(path).name}: {e}'
        return f'Saved {len(self._raw):,} packets to {Path(path).name}'

    @pyqtSlot(QUrl, result=str)
    def export_can_log(self, url: QUrl) -> str:
        """Save the CAN frames tunnelled in AVTP ACF (TSCF/NTSCF) to a CAN log file, so they can be opened or
        replayed in a CAN connection and run through the ordinary CAN views. Returns a message."""
        path = url.toLocalFile() if url.isLocalFile() else url.toString()
        if not log_formats.is_log_file(path):
            return log_formats.unsupported_message(path)
        frames = [(packet.timestamp, frame) for packet in self._decoded for frame in packet.can_frames]
        if not frames:
            return 'No tunnelled CAN frames to save'
        try:
            with can.Logger(path) as writer:
                for timestamp, frame in frames:
                    message = frame.to_message(timestamp)
                    try:
                        writer.on_message_received(message)
                    except Exception:  # Some writers (e.g. BLF) reject an unexpected channel; drop it and retry
                        message.channel = None
                        writer.on_message_received(message)
        except (OSError, ValueError) as e:
            return f'Could not write {Path(path).name}: {e}'
        return f'Saved {len(frames):,} CAN frames to {Path(path).name}'

    def shutdown(self) -> None:
        self.stop_capture()
