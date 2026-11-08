"""Cross-platform live packet capture through libpcap, via ctypes (no third-party dependency), following the same
ctypes-DLL pattern as `core/j2534.py`.

libpcap is the common capture abstraction on every desktop platform: `libpcap` on Linux, the system `libpcap`
(BPF) on macOS, and `wpcap.dll` from **Npcap** on Windows. This module loads whichever is present, lists the
capturable interfaces (`list_interfaces`) and runs a capture on a background thread (`PcapCapture`), delivering
each Ethernet frame to a callback. It is Qt-free, so the capture loop is unit-tested directly — against a pcap
*savefile* opened through the very same libpcap read path, which needs no capture privileges — while live capture
on a real interface additionally needs root / `CAP_NET_RAW` (Linux), BPF access (macOS) or Npcap (Windows).

Only the Ethernet link type is handled, matching the rest of the Ethernet tool.
"""

import ctypes
import ctypes.util
import sys
import threading
from typing import Callable, List, Optional

LINKTYPE_ETHERNET = 1

# Candidate shared-library names per platform, tried in order after ctypes.util.find_library('pcap').
_LIBRARY_NAMES = {
    'linux': ['libpcap.so.1', 'libpcap.so.0.8', 'libpcap.so'],
    'darwin': ['libpcap.dylib', 'libpcap.A.dylib'],
    'win32': ['wpcap.dll', 'Packet.dll'],
}


class PcapError(OSError):
    """A libpcap error, or that libpcap/Npcap is not available."""


class _timeval(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long), ('tv_usec', ctypes.c_long)]


class _pkthdr(ctypes.Structure):
    """struct pcap_pkthdr: the per-packet header libpcap returns."""

    _fields_ = [('ts', _timeval), ('caplen', ctypes.c_uint32), ('len', ctypes.c_uint32)]


class _pcap_if(ctypes.Structure):
    pass


_pcap_if._fields_ = [
    ('next', ctypes.POINTER(_pcap_if)),
    ('name', ctypes.c_char_p),
    ('description', ctypes.c_char_p),
    ('addresses', ctypes.c_void_p),
    ('flags', ctypes.c_uint),
]


def _load_library() -> Optional[ctypes.CDLL]:
    candidates: List[str] = []
    found = ctypes.util.find_library('pcap') or ctypes.util.find_library('wpcap')
    if found:
        candidates.append(found)
    candidates += _LIBRARY_NAMES.get(sys.platform, ['libpcap.so'])
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


_lib = _load_library()


def _bind(lib: ctypes.CDLL) -> None:
    lib.pcap_findalldevs.argtypes = [ctypes.POINTER(ctypes.POINTER(_pcap_if)), ctypes.c_char_p]
    lib.pcap_findalldevs.restype = ctypes.c_int
    lib.pcap_freealldevs.argtypes = [ctypes.POINTER(_pcap_if)]
    lib.pcap_open_live.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
    lib.pcap_open_live.restype = ctypes.c_void_p
    lib.pcap_open_offline.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.pcap_open_offline.restype = ctypes.c_void_p
    lib.pcap_next_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(_pkthdr)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    lib.pcap_next_ex.restype = ctypes.c_int
    lib.pcap_datalink.argtypes = [ctypes.c_void_p]
    lib.pcap_datalink.restype = ctypes.c_int
    lib.pcap_breakloop.argtypes = [ctypes.c_void_p]
    lib.pcap_close.argtypes = [ctypes.c_void_p]


if _lib is not None:
    _bind(_lib)


def available() -> bool:
    """Whether libpcap (or Npcap's wpcap.dll) could be loaded, so live capture is possible on this machine."""
    return _lib is not None


def _require() -> ctypes.CDLL:
    if _lib is None:
        raise PcapError('libpcap is not available (install libpcap on Linux/macOS, or Npcap on Windows)')
    return _lib


def list_interfaces() -> List[str]:
    """The names of the interfaces libpcap can capture on, or [] when libpcap is unavailable or enumeration fails.
    On Windows these are Npcap device names (e.g. ``\\Device\\NPF_{GUID}``)."""
    if _lib is None:
        return []
    errbuf = ctypes.create_string_buffer(256)
    alldevs = ctypes.POINTER(_pcap_if)()
    if _lib.pcap_findalldevs(ctypes.byref(alldevs), errbuf) != 0:
        return []
    names: List[str] = []
    try:
        device = alldevs
        while device:
            contents = device.contents
            if contents.name:
                names.append(contents.name.decode('utf-8', 'replace'))
            device = contents.next
    finally:
        _lib.pcap_freealldevs(alldevs)
    return names


def _drain(
    lib: ctypes.CDLL, handle: int, on_packet: Callable[[float, bytes], None], running: Callable[[], bool]
) -> None:
    """Read packets from an open pcap handle (live or a savefile) until `running()` is false or the source ends,
    delivering each as (timestamp seconds, bytes). The packet data is copied out immediately, since libpcap owns
    the buffer only until the next call."""
    header = ctypes.POINTER(_pkthdr)()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    while running():
        result = lib.pcap_next_ex(handle, ctypes.byref(header), ctypes.byref(data))
        if result == 1:
            pkthdr = header.contents
            timestamp = pkthdr.ts.tv_sec + pkthdr.ts.tv_usec * 1e-6
            on_packet(timestamp, bytes(ctypes.string_at(data, pkthdr.caplen)))
        elif result == 0:
            continue  # Live read timed out; loop back to re-check `running()`
        else:
            break  # -1 error, -2 end of a savefile


def read_offline(path: str, on_packet: Callable[[float, bytes], None]) -> None:
    """Read an Ethernet pcap savefile through libpcap, delivering each frame to `on_packet`. Exercises the same
    read path as live capture, without needing capture privileges. Raises PcapError."""
    lib = _require()
    errbuf = ctypes.create_string_buffer(256)
    handle = lib.pcap_open_offline(str(path).encode('utf-8'), errbuf)
    if not handle:
        raise PcapError(errbuf.value.decode('utf-8', 'replace') or 'could not open the capture file')
    try:
        if lib.pcap_datalink(handle) != LINKTYPE_ETHERNET:
            raise PcapError('not an Ethernet capture')
        _drain(lib, handle, on_packet, lambda: True)
    finally:
        lib.pcap_close(handle)


class PcapCapture:
    """A live capture on one interface through libpcap, run on a background thread so it works identically on
    Linux, macOS and Windows (where pcap handles are not selectable file descriptors). `on_packet(timestamp, data)`
    is called from that thread, so the callback must hand the data to its own loop/queue in a thread-safe way."""

    def __init__(
        self,
        interface: str,
        on_packet: Callable[[float, bytes], None],
        *,
        snaplen: int = 65535,
        promiscuous: bool = True,
        timeout_ms: int = 100,
    ):
        self._interface = interface
        self._on_packet = on_packet
        self._snaplen = snaplen
        self._promiscuous = promiscuous
        self._timeout_ms = timeout_ms
        self._handle: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """Open the interface and begin capturing. Raises PcapError (e.g. no permission, or no such interface)."""
        lib = _require()
        errbuf = ctypes.create_string_buffer(256)
        handle = lib.pcap_open_live(
            self._interface.encode('utf-8'), self._snaplen, 1 if self._promiscuous else 0, self._timeout_ms, errbuf
        )
        if not handle:
            raise PcapError(errbuf.value.decode('utf-8', 'replace') or f'could not open {self._interface}')
        if lib.pcap_datalink(handle) != LINKTYPE_ETHERNET:
            lib.pcap_close(handle)
            raise PcapError(f'{self._interface} is not an Ethernet interface')
        self._handle = handle
        self._running = True
        self._thread = threading.Thread(target=self._run, name='pcap-capture', daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            _drain(_lib, self._handle, self._on_packet, lambda: self._running)
        except Exception:  # noqa: BLE001 - a capture thread must never crash the app
            pass

    def stop(self) -> None:
        self._running = False
        if self._handle is not None and _lib is not None:
            _lib.pcap_breakloop(self._handle)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._handle is not None and _lib is not None:
            _lib.pcap_close(self._handle)
            self._handle = None
