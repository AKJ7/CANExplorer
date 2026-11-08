"""Classic Bluetooth (RFCOMM / SPP) transport for the ELM327 adapter backend, so a Bluetooth OBD dongle can be
opened directly by its address — channel ``bt://AA:BB:CC:DD:EE:FF`` (optionally ``…/<channel>``) — without first
pairing it to an rfcomm serial port.

This is Linux only: it uses ``AF_BLUETOOTH`` RFCOMM sockets. On other platforms, pair the adapter and open its
serial port instead (``COM7``, ``/dev/cu.*-SPP``). Bluetooth Low Energy dongles (e.g. OBDLink CX) speak a different
(GATT) protocol and are not covered here.
"""

import re
import socket
from typing import Tuple

_MAC = re.compile(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$')


def is_bluetooth_channel(channel: str) -> bool:
    """Whether a channel names a classic Bluetooth adapter (``bt://``/``rfcomm://`` prefix, or a bare MAC)."""
    text = channel.strip()
    if text.lower().startswith(('bt://', 'rfcomm://')):
        return True
    return bool(_MAC.match(text))


def parse_channel(channel: str) -> Tuple[str, int]:
    """A ``bt://AA:BB:CC:DD:EE:FF[/<rfcomm channel>]`` channel into (address, rfcomm channel). SPP defaults to 1."""
    text = channel.strip()
    for prefix in ('bt://', 'rfcomm://'):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    rfcomm_channel = 1
    if '/' in text:
        text, _, number = text.rpartition('/')
        rfcomm_channel = int(number)
    if not _MAC.match(text):
        raise ValueError(f'Not a Bluetooth address: {channel!r} (use bt://AA:BB:CC:DD:EE:FF[/channel])')
    return text.upper(), rfcomm_channel


class RfcommSerial:
    """A minimal pyserial-like wrapper over a classic Bluetooth RFCOMM (SPP) socket, exposing just what the ELM327
    backend uses: read, write, in_waiting, reset_input_buffer, close and a (no-op) baudrate."""

    is_bluetooth = True

    def __init__(self, channel: str, timeout: float = 0.02, connect_timeout: float = 10.0):
        if not hasattr(socket, 'AF_BLUETOOTH'):
            raise OSError('Direct Bluetooth needs Linux (AF_BLUETOOTH); pair the adapter and open its serial port')
        address, rfcomm_channel = parse_channel(channel)
        self.baudrate = 0  # Not meaningful for Bluetooth; kept for the serial-like interface
        self.timeout = timeout
        self._sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self._sock.settimeout(connect_timeout)
        self._sock.connect((address, rfcomm_channel))
        self._sock.settimeout(timeout)

    @property
    def in_waiting(self) -> int:
        import fcntl
        import struct
        import termios

        try:
            return struct.unpack('I', fcntl.ioctl(self._sock.fileno(), termios.FIONREAD, b'\x00\x00\x00\x00'))[0]
        except OSError:
            return 0

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b''
        try:
            return self._sock.recv(size)
        except (TimeoutError, BlockingIOError):
            return b''
        except OSError:
            return b''

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self._sock.setblocking(False)
        try:
            while self._sock.recv(4096):
                pass
        except OSError:
            pass
        finally:
            self._sock.settimeout(self.timeout)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
