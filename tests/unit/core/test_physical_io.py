import sys
import types

import pytest

from can_explorer.core.physical_io import (
    SIMULATED,
    AioUsbBackend,
    DeviceInfo,
    LineConfig,
    PhysicalIo,
    Snapshot,
    find_aio_devices,
    list_devices,
    open_device,
    version_text,
)


def test_structures():
    caps = bytes([4, 2, 12, 0]) + b''.join(v.to_bytes(4, 'little') for v in (0x0F, 0x03, 0x000102, 2, 0x7))
    info = DeviceInfo.from_caps('IO', caps)
    assert (info.io_count, info.analog_count, info.analog_bits, info.analog_full_scale) == (4, 2, 12, 4095)
    assert info.can_input(3) and info.can_output(1) and not info.can_output(2)
    assert version_text(info.sw_version) == '0.1.2'
    assert LineConfig(output=True, default_state=True).encode() == bytes([1, 1, 0, 0, 0, 0, 0, 0])
    assert LineConfig(pull='down', active_low=True).encode() == bytes([0, 0, 0x06, 0, 0, 0, 0, 0])
    with pytest.raises(ValueError):
        LineConfig(pull='sideways').encode()
    report = (5000).to_bytes(4, 'little') + (0b1010).to_bytes(4, 'little') + (0b0011).to_bytes(4, 'little')
    snapshot = Snapshot.from_report(report + (4095).to_bytes(2, 'little') + (0).to_bytes(2, 'little'), 2)
    assert (snapshot.device_ms, snapshot.level(1), snapshot.level(2), snapshot.is_output(0)) == (
        5000,
        True,
        False,
        True,
    )
    assert snapshot.analog_volts(info, 3.3) == [pytest.approx(3.3), 0.0]
    assert Snapshot.from_report(report, 2).analog == []  # A device with fewer analogue inputs
    with pytest.raises(ConnectionError):
        DeviceInfo.from_caps('IO', b'\x00' * 10)


def test_simulated_device_loopback():
    io = open_device(SIMULATED)
    assert list_devices()[-1] == (SIMULATED, 'Simulated I/O (no hardware)')
    io.configure_line(0, LineConfig(output=True))
    io.configure_line(4, LineConfig())
    io.configure_line(5, LineConfig(pull='up'))
    io.configure_line(6, LineConfig(pull='up', active_low=True))
    status = io.read_status()
    assert status.is_output(0) and not status.is_output(4)
    assert (status.level(4), status.level(5), status.level(6)) == (False, True, False)
    io.set_outputs(0b1, 0b1)
    status = io.read_status()
    assert status.level(0) and status.level(4)  # Line 4 is wired to line 0
    io.set_outputs(0b10000, 0)  # Inputs cannot be driven
    assert io.read_status().level(4)
    assert len(status.analog) == 4 and all(0 <= value <= 4095 for value in status.analog)
    with pytest.raises(ValueError, match='cannot be an output'):
        io.configure_line(5, LineConfig(output=True))
    with pytest.raises(ValueError, match='lines 0 to 7'):
        io.configure_line(8, LineConfig())


async def test_physical_io_worker():
    io = PhysicalIo(open_device(SIMULATED), SIMULATED)
    try:
        await io.configure_line(1, LineConfig(output=True, default_state=True))
        assert io.outputs == 0b10
        with pytest.raises(ValueError, match='not an output'):
            await io.set_output(2, True)
        await io.set_output(1, False)
        snapshot = await io.poll()
        assert not snapshot.level(1) and io.snapshot is snapshot and list(io.history) == [snapshot]
        await io.set_outputs(0b10, 0b10)
        assert (await io.poll()).level(5)  # Wired to line 1
        await io.identify()
        assert io.backend.identified == 1
    finally:
        io.close()


# aio_usb against a fake USB device implementing the firmware's control requests


class FakeUsbError(Exception):
    def __init__(self, text, errno=None):
        super().__init__(text)
        self.errno = errno


class FakeAioDevice:
    """An adapter with CAN (0), LIN (1) and AIO (2) interfaces, 4 lines (0-1 outputs) and 2 analogue inputs."""

    def __init__(self, serial='0042', aio=True):
        self.serial_number, self.bus, self.address = serial, 1, 7
        protocols = [0xFF, 0x01] + ([0x02] if aio else [])
        self.interfaces = [
            types.SimpleNamespace(
                bInterfaceNumber=n, bInterfaceClass=0xFF, bInterfaceSubClass=0xFF, bInterfaceProtocol=p
            )
            for n, p in enumerate(protocols)
        ]
        self.requests = []
        self.modes = {}
        self.states = 0
        self.claimed = False
        self.unplugged = False

    def get_active_configuration(self):
        return self.interfaces

    def is_kernel_driver_active(self, interface):
        return False

    def ctrl_transfer(self, request_type, request, value, index, data, timeout):
        if self.unplugged:
            raise FakeUsbError('No such device', errno=19)
        assert index == 2  # Always the AIO interface
        self.requests.append((request_type, request, value, bytes(data) if not isinstance(data, int) else data))
        if request_type == 0xC1 and request == 1:
            return bytes([4, 2, 12, 0]) + b''.join(v.to_bytes(4, 'little') for v in (0xF, 0x3, 0x000100, 2, 7))
        if request_type == 0xC1 and request == 6:
            directions = sum(1 << line for line, mode in self.modes.items() if mode == 1)
            header = (123).to_bytes(4, 'little') + self.states.to_bytes(4, 'little') + directions.to_bytes(4, 'little')
            return header + (2048).to_bytes(2, 'little') + (1024).to_bytes(2, 'little')
        if request == 4:
            if value >= 4:
                raise FakeUsbError('Pipe error', errno=32)  # STALL
            self.modes[value] = data[0]
        if request == 5:
            mask, values = int.from_bytes(data[0:4], 'little'), int.from_bytes(data[4:8], 'little')
            self.states = self.states & ~mask | values & mask
        return len(data)


@pytest.fixture
def fake_usb(monkeypatch):
    devices = [FakeAioDevice('0042'), FakeAioDevice('0099', aio=False)]
    util = types.SimpleNamespace(
        claim_interface=lambda device, interface: setattr(device, 'claimed', True),
        release_interface=lambda device, interface: setattr(device, 'claimed', False),
        dispose_resources=lambda device: None,
    )
    core = types.SimpleNamespace(USBError=FakeUsbError, find=lambda **kwargs: iter(devices))
    module = types.SimpleNamespace(core=core, util=util)
    monkeypatch.setitem(sys.modules, 'usb', module)
    monkeypatch.setitem(sys.modules, 'usb.core', core)
    monkeypatch.setitem(sys.modules, 'usb.util', util)
    monkeypatch.setitem(sys.modules, 'libusb_package', None)  # The system's libusb
    return devices


def test_aio_usb_backend(fake_usb):
    device = fake_usb[0]
    assert find_aio_devices() == [('aio:0042', 'aio_usb I/O adapter 0042')]  # Plain candleLight left out
    backend = AioUsbBackend('aio:0042')
    assert device.claimed and backend.info.name == 'aio_usb 0042'
    assert (backend.info.io_count, backend.info.analog_count, backend.info.output_mask) == (4, 2, 0x3)
    assert device.requests[0] == (0x41, 0, 0, (0xBEEF).to_bytes(4, 'little'))  # HOST_FORMAT first
    backend.configure_line(1, LineConfig(output=True))
    assert device.requests[-1] == (0x41, 4, 1, bytes([1, 0, 0, 0, 0, 0, 0, 0]))
    backend.set_outputs(0b10, 0b10)
    assert device.requests[-1] == (0x41, 5, 0, (2).to_bytes(4, 'little') + (2).to_bytes(4, 'little'))
    status = backend.read_status()
    assert device.requests[-1] == (0xC1, 6, 0, 16)  # 12 + 2 × 2 bytes
    assert status.level(1) and status.is_output(1) and status.analog == [2048, 1024]
    with pytest.raises(ValueError, match='lines 0 to 3'):
        backend.configure_line(4, LineConfig())
    backend.info.io_count = 8  # A host bug past the check: the device's STALL becomes a rejection
    with pytest.raises(ValueError, match='rejected'):
        backend.configure_line(5, LineConfig())
    backend.identify()
    assert device.requests[-1] == (0x41, 3, 0, b'')
    device.unplugged = True
    with pytest.raises(ConnectionError, match='No such device'):
        backend.read_status()
    backend.close()
    assert not device.claimed
    with pytest.raises(ConnectionError, match='no aio_usb interface'):
        AioUsbBackend('aio:0099')
    with pytest.raises(ConnectionError, match='not connected'):
        AioUsbBackend('aio:7777')
