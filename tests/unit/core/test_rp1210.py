import ctypes

import can
import pytest

from can_explorer.core import rp1210
from can_explorer.core.rp1210 import (
    Rp1210Bus,
    decode_message,
    encode_message,
    installed_apis,
    installed_vendors,
    read_vendor,
    resolve_channel,
)

RP121032 = """
[RP1210Support]
APIImplementations=NULN2R32 , DGDPA5MA
"""

NEXIQ = """
[VendorInformation]
Name=NEXIQ Technologies USB-Link 2
TimeStampWeight=1
Devices=1,2
Protocols=1,2

[DeviceInformation1]
DeviceID=1
DeviceDescription=USB-Link 2 USB
DeviceName=USB-Link 2

[DeviceInformation2]
DeviceID=2
DeviceDescription=USB-Link 2 Bluetooth

[ProtocolInformation1]
ProtocolString=J1939
ProtocolDescription=SAE J1939
ProtocolSpeed=250,500,Auto

[ProtocolInformation2]
ProtocolString=CAN
ProtocolDescription=Generic CAN
ProtocolSpeed=125,250,500,1000
"""

# A driver that offers only J1939, not the generic CAN protocol
DG = """
[VendorInformation]
Name=DG Technologies DPA 5
Devices=1
Protocols=1

[DeviceInformation1]
DeviceID=1
DeviceDescription=DPA 5 USB

[ProtocolInformation1]
ProtocolString=J1939
ProtocolDescription=SAE J1939
"""


@pytest.fixture
def windir(tmp_path, monkeypatch):
    (tmp_path / 'RP121032.INI').write_text(RP121032)
    (tmp_path / 'NULN2R32.INI').write_text(NEXIQ)
    (tmp_path / 'DGDPA5MA.INI').write_text(DG)
    monkeypatch.setattr(rp1210, '_windir', lambda: tmp_path)
    return tmp_path


class FakeDll:
    """A minimal stand-in for a vendor RP1210 DLL: records connects/commands/sends and replays queued frames."""

    def __init__(self):
        self.connected = False
        self.protocol = None
        self.device_id = None
        self.commands = []
        self.sent = []
        self.rx = []
        self.client_id = 3

    def RP1210_ClientConnect(self, hwnd, device_id, protocol, tx_size, rx_size, packetizing):
        self.device_id, self.protocol, self.connected = device_id, bytes(protocol), True
        return self.client_id

    def RP1210_SendCommand(self, number, client_id, data, size):
        self.commands.append((number, bytes(data[:size]) if data else b''))
        return 0

    def RP1210_ReadMessage(self, client_id, buffer, size, block):
        if not self.rx:
            return 0
        payload = self.rx.pop(0)
        ctypes.memmove(buffer, payload, len(payload))
        return len(payload)

    def RP1210_SendMessage(self, client_id, data, size, notify, block):
        self.sent.append(bytes(data[:size]))
        return 0

    def RP1210_ClientDisconnect(self, client_id):
        self.connected = False
        return 0

    def RP1210_GetErrorMsg(self, code, buffer):
        return 1  # No driver text; the bus falls back to its own table


def rp1210_frame(identifier, data, extended=False, timestamp=1000):
    body = bytes([1 if extended else 0]) + identifier.to_bytes(4, 'big') + bytes(data)
    return timestamp.to_bytes(4, 'big') + body


# INI enumeration


def test_installed_apis_strips_whitespace(windir):
    assert installed_apis() == ['NULN2R32', 'DGDPA5MA']


def test_read_vendor(windir):
    vendor = read_vendor('NULN2R32')
    assert vendor.name == 'NEXIQ Technologies USB-Link 2'
    assert [(d.id, d.description) for d in vendor.devices] == [(1, 'USB-Link 2 USB'), (2, 'USB-Link 2 Bluetooth')]
    assert vendor.supports_can() and {p.string for p in vendor.protocols} == {'J1939', 'CAN'}
    assert read_vendor('DGDPA5MA').supports_can() is False
    assert read_vendor('MISSING') is None


def test_resolve_channel(windir):
    assert resolve_channel('NULN2R32:2')[1] == 2  # Explicit device id
    vendor, device_id = resolve_channel('NULN2R32')
    assert device_id == 1 and vendor.api == 'NULN2R32'  # Defaults to the first device
    for bad in ('NULN2R32:x', 'NOPE', ''):
        with pytest.raises(can.CanInitializationError):
            resolve_channel(bad)


def test_detect_available_configs_lists_only_can_devices(windir):
    configs = Rp1210Bus._detect_available_configs()
    # The Nexiq offers CAN (both devices); the DG driver offers only J1939 and is left out
    assert configs == [
        {'interface': 'rp1210', 'channel': 'NULN2R32:1'},
        {'interface': 'rp1210', 'channel': 'NULN2R32:2'},
    ]


def test_installed_vendors_skips_unreadable(windir, tmp_path):
    (tmp_path / 'RP121032.INI').write_text('[RP1210Support]\nAPIImplementations=NULN2R32,GHOST\n')
    assert [vendor.api for vendor in installed_vendors()] == ['NULN2R32']  # GHOST.INI does not exist


# Message translation


def test_encode_message():
    assert encode_message(can.Message(arbitration_id=0x123, data=b'\x01\x02', is_extended_id=False)) == (
        b'\x00\x00\x00\x01\x23\x01\x02'
    )
    assert encode_message(can.Message(arbitration_id=0x18FEF100, data=b'', is_extended_id=True)) == (
        b'\x01\x18\xfe\xf1\x00'
    )
    for bad in (
        can.Message(arbitration_id=1, is_fd=True),
        can.Message(arbitration_id=1, is_remote_frame=True),
        can.Message(arbitration_id=1, data=bytes(9), dlc=9),
    ):
        with pytest.raises(can.CanOperationError):
            encode_message(bad)


def test_decode_message():
    assert decode_message(rp1210_frame(0x7E8, b'\xaa\xbb', timestamp=5), 1.0) == (5, 0x7E8, False, b'\xaa\xbb')
    # A 29-bit identifier, and the timestamp scaled by the vendor's weight
    assert decode_message(rp1210_frame(0x18DAF110, b'', extended=True, timestamp=10), 2.0) == (
        20,
        0x18DAF110,
        True,
        b'',
    )
    assert decode_message(b'\x00\x00\x00', 1.0) is None  # Too short


# Bus (with a fake DLL — no hardware)


@pytest.fixture
def fake_bus(windir, monkeypatch):
    dll = FakeDll()
    monkeypatch.setattr(rp1210, 'load_api', lambda api: dll)
    bus = Rp1210Bus('NULN2R32:1', bitrate=250_000)
    yield bus, dll
    bus.shutdown()


def test_bus_connect_sets_protocol_and_pass_filter(fake_bus):
    bus, dll = fake_bus
    assert dll.connected and dll.device_id == 1
    assert dll.protocol == b'CAN:Baud=250'
    assert dll.commands == [(rp1210.CMD_SET_ALL_FILTERS_TO_PASS, b'')]  # Opens the receive gate


def test_bus_send_and_receive(fake_bus):
    bus, dll = fake_bus
    bus.send(can.Message(arbitration_id=0x244, data=b'\x10\x20', is_extended_id=False))
    assert dll.sent == [b'\x00\x00\x00\x02\x44\x10\x20']

    dll.rx.append(rp1210_frame(0x7E8, b'\x03\x41\x0c', timestamp=1500))
    message = bus.recv(timeout=1)
    assert message is not None
    assert (message.arbitration_id, message.is_extended_id, bytes(message.data)) == (0x7E8, False, b'\x03\x41\x0c')
    assert message.is_rx
    assert bus.recv(timeout=0) is None  # Nothing more queued


def test_bus_receive_own_messages_echoes(windir, monkeypatch):
    dll = FakeDll()
    monkeypatch.setattr(rp1210, 'load_api', lambda api: dll)
    bus = Rp1210Bus('NULN2R32:1', receive_own_messages=True)
    try:
        bus.send(can.Message(arbitration_id=0x55, data=b'\x01', is_extended_id=False))
        echo = bus.recv(timeout=1)
        assert echo is not None and echo.arbitration_id == 0x55 and not echo.is_rx
    finally:
        bus.shutdown()


def test_bus_rejects_fd_and_listen_only(fake_bus):
    # fd and listen_only are refused in __init__ before any driver is loaded
    with pytest.raises(can.CanInterfaceNotImplementedError):
        Rp1210Bus('NULN2R32:1', fd=True)
    with pytest.raises(can.CanInitializationError):
        Rp1210Bus('NULN2R32:1', listen_only=True)
