import ctypes
import io
import json
import shutil
import subprocess
import sys
import time

import can
import pytest

from can_explorer.core import j2534, j2534_bridge
from can_explorer.core.j2534 import (
    BRIDGE_SCRIPT,
    J2534Bus,
    bridge_command,
    decode_message,
    encode_message,
    library_machine,
    registered_devices,
    resolve_channel,
)
from can_explorer.core.j2534_driver import CAN_29BIT_ID, CAN_ID_BOTH, PassThruDriver, PassThruMsg

X64, X86 = 0x8664, 0x014C


class FakeDriver:
    """A J2534 04.04 driver with a CAN channel: frames put in with frame() are read, written frames recorded and
    looped back when LOOPBACK is set."""

    def __init__(self, bitrates=(250_000, 500_000), both=True, loopback=True, connected=True):
        self.bitrates, self.both, self.loopback_supported, self.connected = bitrates, both, loopback, connected
        self.loopback = False
        self.connects, self.filters, self.written, self.calls = [], [], [], []
        self.rx = []
        self.write_result = 0
        self.timestamp = 1_000

    def frame(self, identifier, data, extended=False, status=0):
        message = PassThruMsg(ProtocolID=5, RxStatus=status | (CAN_29BIT_ID if extended else 0))
        payload = identifier.to_bytes(4, 'big') + bytes(data)
        message.DataSize = len(payload)
        ctypes.memmove(message.Data, payload, len(payload))
        self.timestamp += 500
        message.Timestamp = self.timestamp
        self.rx.append(message)

    def PassThruOpen(self, name, device_id):
        self.calls.append('open')
        if not self.connected:
            return 0x08
        device_id._obj.value = 7
        return 0

    def PassThruConnect(self, device_id, protocol, flags, bitrate, channel_id):
        self.connects.append((device_id, protocol, flags, bitrate))
        if flags & CAN_ID_BOTH and not self.both:
            return 0x06
        if bitrate not in self.bitrates:
            return 0x19
        channel_id._obj.value = 3
        return 0

    def PassThruStartMsgFilter(self, channel_id, kind, mask, pattern, flow, filter_id):
        self.filters.append((kind, mask._obj.TxFlags, bytes(mask._obj.Data[:4]), flow))
        return 0

    def PassThruIoctl(self, channel_id, ioctl, parameters, output):
        if ioctl == 0x02:
            config = parameters._obj.ConfigPtr[0]
            if not self.loopback_supported:
                return 0x01
            self.loopback = config.Parameter == 0x03 and bool(config.Value)
        return 0

    def PassThruReadVersion(self, device_id, firmware, dll, api):
        firmware.value, dll.value, api.value = b'1.2', b'3.4', b'04.04'
        return 0

    def PassThruGetLastError(self, buffer):
        buffer.value = b'Device says no'
        return 0

    def PassThruReadMsgs(self, channel_id, messages, count, timeout):
        wanted = count._obj.value
        taken, self.rx = self.rx[:wanted], self.rx[wanted:]
        for index, message in enumerate(taken):
            messages[index] = message
        count._obj.value = len(taken)
        if not taken:
            time.sleep(timeout / 1000)
            return 0x09 if timeout else 0x10
        return 0

    def PassThruWriteMsgs(self, channel_id, message, count, timeout):
        sent = message._obj
        self.written.append((sent.TxFlags, bytes(sent.Data[: sent.DataSize])))
        if self.write_result:
            return self.write_result
        if self.loopback:
            echo = PassThruMsg.from_buffer_copy(sent)
            echo.RxStatus = 0x01 | (sent.TxFlags & CAN_29BIT_ID)
            self.rx.append(echo)
        return 0

    def PassThruDisconnect(self, channel_id):
        self.calls.append('disconnect')
        return 0

    def PassThruClose(self, device_id):
        self.calls.append('close')
        return 0


ENTRIES = [
    {
        'Key': 'Mongoose',
        'Name': 'Mongoose Pro',
        'Vendor': 'Drew',
        'FunctionLibrary': r'C:\Drew\mongoose64.dll',
        'CAN': 1,
    },
    {
        'Key': 'Mongoose',
        'Name': 'Mongoose Pro',
        'Vendor': 'Drew',
        'FunctionLibrary': r'C:\Drew\mongoose32.dll',
        'CAN': 1,
    },
    {'Key': 'OP20', 'Name': 'OpenPort 2.0', 'Vendor': 'Tactrix', 'FunctionLibrary': r'C:\Tactrix\op20pt32.dll'},
    {'Key': 'KLine', 'Name': 'K-Line only', 'FunctionLibrary': r'C:\K\kline.dll', 'CAN': 0},
]
MACHINE_OF = {
    r'C:\Drew\mongoose64.dll': X64,
    r'C:\Drew\mongoose32.dll': X86,
    r'C:\Tactrix\op20pt32.dll': X86,
    r'C:\K\kline.dll': X64,
}


@pytest.fixture
def driver(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(j2534, '_registry_entries', lambda: ENTRIES)
    monkeypatch.setattr(j2534, 'library_machine', lambda path: MACHINE_OF.get(path))
    monkeypatch.setattr(j2534, 'process_machine', lambda: X64)
    monkeypatch.setattr(j2534, 'load_library', lambda path: driver)
    monkeypatch.setattr(j2534, 'bridge_command', lambda: None)
    return driver


def test_messages():
    tx_flags, data = encode_message(can.Message(arbitration_id=0x18DAF110, is_extended_id=True, data=[2, 0x10, 3]))
    assert tx_flags == CAN_29BIT_ID and data == bytes.fromhex('18daf110021003')
    assert decode_message((5, CAN_29BIT_ID | 0x01, 0, data)) == (0x18DAF110, True, b'\x02\x10\x03', True)
    assert decode_message((5, 0x02, 0, data)) is None  # START_OF_MESSAGE indication
    assert decode_message((6, 0, 0, data)) is None  # Another protocol
    with pytest.raises(can.CanOperationError, match='data frames only'):
        encode_message(can.Message(arbitration_id=1, is_remote_frame=True))


def test_library_machine(tmp_path):
    dll = tmp_path / 'driver.dll'
    dll.write_bytes(b'MZ' + bytes(0x3A) + (0x40).to_bytes(4, 'little') + b'PE\0\0' + X86.to_bytes(2, 'little'))
    assert library_machine(str(dll)) == X86
    (tmp_path / 'text.dll').write_text('not a library')
    assert library_machine(str(tmp_path / 'text.dll')) is None
    assert library_machine(str(tmp_path / 'missing.dll')) is None


def test_registered_devices_and_detection(driver):
    devices = registered_devices()
    assert [(d.name, d.loadable) for d in devices] == [
        ('Mongoose Pro', True),
        ('Mongoose Pro', False),
        ('OpenPort 2.0', False),
        ('K-Line only', True),
    ]
    assert J2534Bus._detect_available_configs() == [{'interface': 'j2534', 'channel': 'Mongoose Pro'}]
    assert resolve_channel('mongoose pro').library == r'C:\Drew\mongoose64.dll'
    with pytest.raises(can.CanInitializationError, match=r'32-bit \(x86\).*64-bit \(x64\)'):
        resolve_channel('OpenPort 2.0')
    with pytest.raises(can.CanInitializationError, match='registered: K-Line only, Mongoose Pro, OpenPort 2.0'):
        resolve_channel('Nothing')
    assert resolve_channel(r'D:\drivers\other.dll').name == 'other.dll'


def test_bus(driver):
    bus = can.Bus(interface='j2534', channel='Mongoose Pro', bitrate=250_000, receive_own_messages=True)
    try:
        assert isinstance(bus, J2534Bus) and bus.channel_info == 'J2534 Mongoose Pro'
        assert bus.info == j2534.DeviceInfo('1.2', '3.4', '04.04')
        assert driver.connects == [(7, 5, CAN_ID_BOTH, 250_000)]
        assert [(kind, flags, mask) for kind, flags, mask, _ in driver.filters] == [
            (1, 0, bytes(4)),
            (1, CAN_29BIT_ID, bytes(4)),
        ]
        assert driver.loopback
        assert bus.recv(0.05) is None
        driver.frame(0x7E8, [3, 0x41, 0x0D, 0x2A])
        driver.frame(0x18DAF110, [1], extended=True)
        first, second = bus.recv(1), bus.recv(1)
        assert first.arbitration_id == 0x7E8 and not first.is_extended_id and first.data == b'\x03\x41\x0d\x2a'
        assert second.is_extended_id and second.is_rx and abs(second.timestamp - first.timestamp - 0.0005) < 1e-6
        bus.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=[2, 1, 0]))
        assert driver.written == [(0, bytes.fromhex('000007df020100'))]
        echo = bus.recv(1)
        assert echo.arbitration_id == 0x7DF and not echo.is_rx
        driver.write_result = 0x09
        with pytest.raises(can.CanOperationError, match='timed out'):
            bus.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=[1]))
        driver.write_result = 0x08
        with pytest.raises(can.CanOperationError, match='device not connected \\(Device says no\\)'):
            bus.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=[1]))
    finally:
        bus.shutdown()
    assert driver.calls == ['open', 'disconnect', 'close']
    with pytest.raises(can.CanOperationError, match='closed'):
        bus.send(can.Message(arbitration_id=1, data=[1]))


def test_fallbacks_and_errors(driver):
    driver.both, driver.loopback_supported = False, False
    bus = can.Bus(interface='j2534', channel='Mongoose Pro', receive_own_messages=True)
    try:
        assert [flags for _, _, flags, _ in driver.connects] == [CAN_ID_BOTH, 0]
        bus.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=[1]))
        echo = bus.recv(1)
        assert echo.arbitration_id == 0x123 and not echo.is_rx
    finally:
        bus.shutdown()
    driver.both = True
    driver.calls.clear()
    with pytest.raises(can.CanInitializationError, match='invalid bitrate'):
        can.Bus(interface='j2534', channel='Mongoose Pro', bitrate=1_000_000)
    assert driver.calls == ['open', 'close']
    driver.connected = False
    with pytest.raises(can.CanInitializationError, match='device not connected'):
        can.Bus(interface='j2534', channel='Mongoose Pro')
    with pytest.raises(can.CanInterfaceNotImplementedError, match='classic CAN'):
        can.Bus(interface='j2534', channel='Mongoose Pro', fd=True)
    with pytest.raises(can.CanInitializationError, match='listen only'):
        can.Bus(interface='j2534', channel='Mongoose Pro', listen_only=True)


def test_missing_library():
    with pytest.raises(can.CanInitializationError, match='Could not load the J2534 driver'):
        can.Bus(interface='j2534', channel='/does/not/exist/driver.so')


# 32-bit bridge


def test_bridge_protocol(monkeypatch):
    fake = FakeDriver()
    fake.frame(0x7E8, [1, 2])
    monkeypatch.setattr(j2534_bridge, 'load_library', lambda path: fake)
    requests = [
        ['open'],
        ['load', 'driver.dll'],
        ['open'],
        ['connect', 7, 5, CAN_ID_BOTH, 500_000],
        ['read', 3, 4, 0],
        ['write', 3, 5, 0, '000007df0201', 100],
        ['read_version', 7],
        ['last_error'],
        ['unknown'],
        ['exit'],
        ['open'],  # Not answered: the bridge has ended
    ]
    output = io.BytesIO()
    j2534_bridge.serve(io.BytesIO(b''.join(json.dumps(r).encode() + b'\n' for r in requests)), output)
    hello, *answers = [json.loads(line) for line in output.getvalue().splitlines()]
    assert hello == {'bridge': 1, 'machine': j2534_bridge.process_machine() or 0}  # 0 outside Windows
    assert answers == [
        {'error': 'ValueError: No driver loaded'},
        {'result': None},
        {'result': [0, 7]},
        {'result': [0, 3]},
        {'result': [0, [[5, 0, 1500, '000007e80102']]]},
        {'result': 0},
        {'result': [0, ['1.2', '3.4', '04.04']]},
        {'result': 'Device says no'},
        {'error': "ValueError: Unknown call 'unknown'"},
        {'result': None},
    ]
    assert fake.written == [(0, bytes.fromhex('000007df0201'))]


def test_bus_through_the_bridge(driver, monkeypatch):
    monkeypatch.setattr(j2534, 'bridge_command', lambda: ('bridge',))
    monkeypatch.setattr(j2534, 'BridgeDriver', lambda command, library: PassThruDriver(driver))
    assert J2534Bus._detect_available_configs() == [
        {'interface': 'j2534', 'channel': 'Mongoose Pro'},
        {'interface': 'j2534', 'channel': 'OpenPort 2.0'},
    ]
    assert resolve_channel('Mongoose Pro').library == r'C:\Drew\mongoose64.dll'  # Loaded directly if possible
    bus = can.Bus(interface='j2534', channel='OpenPort 2.0')
    try:
        assert bus.info.bridge and bus.channel_info == 'J2534 OpenPort 2.0 (32-bit bridge)'
        driver.frame(0x100, [9])
        assert bus.recv(1).data == b'\x09'
    finally:
        bus.shutdown()


def test_without_bridge(driver):
    with pytest.raises(can.CanInitializationError, match='Install a 32-bit Python'):
        resolve_channel('OpenPort 2.0')
    arm = j2534.J2534Device('Arm', '', 'arm.dll', machine=0xAA64)
    assert not arm.loadable and not arm.bridgeable


def test_bridge_command(monkeypatch):
    bridge_command.cache_clear()
    monkeypatch.setenv(j2534.BRIDGE_ENVIRONMENT, r'C:\Python312-32\python.exe')
    try:
        assert bridge_command() == (r'C:\Python312-32\python.exe', str(BRIDGE_SCRIPT))
        bridge_command.cache_clear()
        monkeypatch.setenv(j2534.BRIDGE_ENVIRONMENT, r'C:\Tools\j2534-bridge32.exe')
        assert bridge_command() == (r'C:\Tools\j2534-bridge32.exe',)
        bridge_command.cache_clear()
        monkeypatch.delenv(j2534.BRIDGE_ENVIRONMENT)
        if sys.platform != 'win32':
            assert bridge_command() is None
    finally:
        bridge_command.cache_clear()


def test_bridge_start_failures():
    script = BRIDGE_SCRIPT.read_text()
    assert 'import can' not in script and 'from can' not in script  # Standard library only
    with pytest.raises(can.CanOperationError, match='stopped: boom'):
        j2534.BridgeDriver((sys.executable, '-c', 'import sys; sys.exit("boom")'), 'x.dll')
    with pytest.raises(can.CanInitializationError, match=r'runs as 64-bit \(x64\)'):
        j2534.BridgeDriver((sys.executable, '-c', 'print(\'{"bridge": 1, "machine": 34404}\')'), 'x.dll')
    if j2534_bridge.process_machine() in (None, X86):  # The test's Python can be a bridge: it loads the driver
        with pytest.raises(can.CanOperationError, match='J2534 bridge: OSError'):
            j2534.BridgeDriver((sys.executable, str(BRIDGE_SCRIPT)), '/does/not/exist/driver.so')
    else:  # A 64-bit Python on Windows is refused as a bridge for 32-bit drivers
        with pytest.raises(can.CanInitializationError, match='needs a 32-bit Python'):
            j2534.BridgeDriver((sys.executable, str(BRIDGE_SCRIPT)), 'driver.dll')
    with pytest.raises(can.CanInitializationError, match='Could not start the J2534 bridge'):
        j2534.BridgeDriver(('/does/not/exist/bridge',), 'x.dll')


STUB = r'''
#include <stdint.h>
#include <string.h>
typedef uint32_t U;
typedef struct { U ProtocolID, RxStatus, TxFlags, Timestamp, DataSize, ExtraDataIndex; unsigned char Data[4128]; } MSG;
typedef struct { U Parameter, Value; } SCONFIG;
typedef struct { U NumOfParams; SCONFIG *ConfigPtr; } SCONFIG_LIST;
static int loopback = 0, pending = 0; static MSG queue[16];
int32_t PassThruOpen(void *name, U *dev) { *dev = 1; return 0; }
int32_t PassThruClose(U dev) { return 0; }
int32_t PassThruConnect(U dev, U proto, U flags, U baud, U *ch) {
  if (proto != 5 || baud != 500000) return 0x19;
  *ch = 2; MSG *m = &queue[pending++]; memset(m, 0, sizeof *m); m->ProtocolID = 5; m->Timestamp = 100; m->DataSize = 6;
  unsigned char d[] = {0, 0, 7, 0xE8, 0xAB, 0xCD}; memcpy(m->Data, d, 6); return 0; }
int32_t PassThruDisconnect(U ch) { return 0; }
int32_t PassThruStartMsgFilter(U ch, U type, MSG *mask, MSG *pattern, MSG *fc, U *id) {
  if (!mask || !pattern || mask->DataSize != 4 || fc) return 4; *id = 1; return 0; }
int32_t PassThruIoctl(U ch, U ioctl, void *in, void *out) {
  if (ioctl == 2) { SCONFIG_LIST *l = in; if (l->NumOfParams == 1 && l->ConfigPtr[0].Parameter == 3) loopback = l->ConfigPtr[0].Value; }
  return 0; }
int32_t PassThruReadMsgs(U ch, MSG *msgs, U *num, U timeout) {
  U n = 0; while (n < *num && n < (U)pending) { msgs[n] = queue[n]; n++; }
  memmove(queue, queue + n, (pending - n) * sizeof(MSG)); pending -= n; *num = n; return n ? 0 : (timeout ? 9 : 0x10); }
int32_t PassThruWriteMsgs(U ch, MSG *msg, U *num, U timeout) {
  if (*num != 1 || msg->ProtocolID != 5) return 0xA;
  if (loopback) { queue[pending] = *msg; queue[pending].RxStatus = 1 | (msg->TxFlags & 0x100); queue[pending].Timestamp = 350; pending++; }
  return 0; }
int32_t PassThruReadVersion(U dev, char *fw, char *dll, char *api) { strcpy(fw, "FW 9"); strcpy(dll, "Stub 1"); strcpy(api, "04.04"); return 0; }
int32_t PassThruGetLastError(char *e) { strcpy(e, "stub error"); return 0; }
'''


@pytest.mark.skipif(sys.platform == 'win32' or shutil.which('gcc') is None, reason='Builds a J2534 stub with gcc')
def test_compiled_driver_in_process_and_through_the_bridge(tmp_path, monkeypatch):
    source, library = tmp_path / 'stub.c', tmp_path / 'libj2534stub.so'
    source.write_text(STUB)
    subprocess.run(['gcc', '-shared', '-fPIC', '-o', str(library), str(source)], check=True)
    for bridged in (False, True):
        if bridged:  # Pretend the library is 32-bit and CANExplorer 64-bit: it runs in the bridge process
            monkeypatch.setattr(j2534, 'process_machine', lambda: X64)
            monkeypatch.setattr(j2534, 'library_machine', lambda path: X86)
            monkeypatch.setattr(j2534, 'bridge_command', lambda: (sys.executable, str(BRIDGE_SCRIPT)))
        bus = can.Bus(interface='j2534', channel=str(library), receive_own_messages=True)
        try:
            assert bus.info.bridge == bridged and bus.info.dll == 'Stub 1'
            first = bus.recv(1)
            assert first.arbitration_id == 0x7E8 and first.data == b'\xab\xcd' and first.is_rx
            bus.send(can.Message(arbitration_id=0x18DB33F1, is_extended_id=True, data=[2, 1, 0]))
            echo = bus.recv(1)
            assert echo.is_extended_id and not echo.is_rx and echo.data == b'\x02\x01\x00'
            assert abs(echo.timestamp - first.timestamp - 0.00025) < 1e-6
        finally:
            bus.shutdown()
    with pytest.raises(can.CanInitializationError, match=r'invalid bitrate \(stub error\)'):
        can.Bus(interface='j2534', channel=str(library), bitrate=250_000)
