"""comma.ai pandas over USB against a simulated device: the packet format of opendbc/safety/can.h, the control
requests of board/main_comms.h and the safety mode fallback of release firmware."""

import hashlib
import queue
import struct

import can
import pytest

from can_explorer.core import panda_usb
from can_explorer.core.panda_usb import (
    PacketParser,
    PandaBus,
    PandaPacket,
    pack_packet,
    parse_channel,
)
from tests.unit.waiting import wait_until as wait_by_deadline

# opendbc/safety/can.h as read for this module; its hash is the firmware's CAN packet version
CAN_H = b"""#ifndef SAFETY_CAN_H
#define SAFETY_CAN_H

static const unsigned char dlc_to_len[] = {0U, 1U, 2U, 3U, 4U, 5U, 6U, 7U, 8U, 12U, 16U, 20U, 24U, 32U, 48U, 64U};

#define CANPACKET_DATA_SIZE_MAX 64U

typedef struct {
  unsigned char fd : 1;
  unsigned char bus : 3;
  unsigned char data_len_code : 4;  // lookup length with dlc_to_len
  unsigned char rejected : 1;
  unsigned char returned : 1;
  unsigned char extended : 1;
  unsigned int addr : 29;
  unsigned char checksum;
  unsigned char data[CANPACKET_DATA_SIZE_MAX];
} __attribute__((packed, aligned(4))) CANPacket_t;

#define GET_LEN(msg) ((unsigned int)dlc_to_len[(msg)->data_len_code])

#endif
"""


def test_known_packet_version_is_the_hash_of_can_h():
    version = int.from_bytes(hashlib.sha256(CAN_H).digest()[:4], 'little')
    assert version in panda_usb.KNOWN_CAN_PACKET_VERSIONS


def reference_pack(address, data, bus, fd=False):
    """panda's python/__init__.py pack_can_buffer for one frame, transcribed."""
    extended = 1 if address >= 0x800 else 0
    header = bytearray(6)
    word = (address << 3) | (extended << 2)
    header[0] = (panda_usb.LEN_TO_DLC[len(data)] << 4) | (bus << 1) | int(fd)
    header[1:5] = word.to_bytes(4, 'little')
    result = 0
    for byte in bytes(header[:5]) + data:
        result ^= byte
    header[5] = result
    return bytes(header) + data


@pytest.mark.parametrize(
    ('address', 'data', 'bus', 'fd'),
    [(0x123, b'\x01\x02\x03', 0, False), (0x18DAF110, bytes(range(8)), 2, False), (0x7E0, bytes(64), 1, True)],
)
def test_pack_matches_pandas_library(address, data, bus, fd):
    packet = PandaPacket(address, data, bus, extended=address >= 0x800, fd=fd)
    assert pack_packet(packet) == reference_pack(address, data, bus, fd)


def test_parser_handles_split_packets_flags_and_bad_checksums():
    parser = PacketParser()
    stream = pack_packet(PandaPacket(0x100, b'\xaa' * 8, 1)) + pack_packet(
        PandaPacket(0x1ABCDEF, bytes(range(12)), 2, extended=True, fd=True, returned=True)
    )
    first = parser.feed(stream[:10])
    rest = parser.feed(stream[10:])
    assert first == [] and len(rest) == 2  # The first packet spanned two transfers
    assert (rest[0].address, rest[0].bus, rest[0].data) == (0x100, 1, b'\xaa' * 8)
    assert rest[1].extended and rest[1].fd and rest[1].returned and rest[1].data == bytes(range(12))
    bad = bytearray(pack_packet(PandaPacket(0x200, b'\x01', 0)))
    bad[5] ^= 0xFF
    assert parser.feed(bytes(bad)) == [] and parser.checksum_errors == 1
    assert len(parser.feed(pack_packet(PandaPacket(0x201, b'\x02', 0)))) == 1  # Back in step


def test_pack_rejects_lengths_that_are_not_can_fd_lengths():
    with pytest.raises(can.CanOperationError):
        pack_packet(PandaPacket(0x100, bytes(9), 0, fd=True))


def test_parse_channel():
    assert parse_channel('') == ('', None)
    assert parse_channel(None) == ('', None)
    assert parse_channel('@2') == ('', 2)
    assert parse_channel('abc123@1') == ('abc123', 1)
    with pytest.raises(ValueError):
        parse_channel('abc@3')


class FakePanda:
    """A panda's USB device as pyusb presents it."""

    def __init__(self, hw_type=0x07, debug_firmware=False, can_version=0x83E460B9):
        self.serial_number = '0123456789abcdef01234567'
        self.hw_type, self.debug_firmware, self.can_version = hw_type, debug_firmware, can_version
        self.safety_mode = panda_usb.SAFETY_SILENT
        self.controls = []
        self.sent = bytearray()
        self.rx: queue.Queue = queue.Queue()

    def ctrl_transfer(self, request_type, request, value, index, data_or_length, timeout):
        if request_type == panda_usb.REQUEST_OUT:
            self.controls.append((request, value, index))
            if request == panda_usb.REQ_SET_SAFETY_MODE:
                # set_safety_mode: an unknown mode (ALLOUTPUT without ALLOW_DEBUG) falls back to SILENT
                allowed = {panda_usb.SAFETY_SILENT, panda_usb.SAFETY_ELM327, panda_usb.SAFETY_NOOUTPUT}
                if self.debug_firmware:
                    allowed.add(panda_usb.SAFETY_ALLOUTPUT)
                self.safety_mode = value if value in allowed else panda_usb.SAFETY_SILENT
            return 0
        if request == panda_usb.REQ_GET_HW_TYPE:
            return bytes([self.hw_type])
        if request == panda_usb.REQ_GET_VERSION:
            return b'DEV-abcdef-RELEASE\0'
        if request == panda_usb.REQ_GET_PACKET_VERSIONS:
            return struct.pack('<II', 0x1234, self.can_version)
        if request == panda_usb.REQ_HEALTH:
            health = bytearray(48)
            health[panda_usb.HEALTH_SAFETY_MODE_OFFSET] = self.safety_mode
            return bytes(health)
        raise AssertionError(f'unexpected request 0x{request:02X}')

    def read(self, endpoint, size, timeout):
        assert endpoint == panda_usb.ENDPOINT_IN
        try:
            return self.rx.get(timeout=timeout / 1000)
        except queue.Empty:
            raise TimeoutError('USBTimeoutError: timed out') from None

    def write(self, endpoint, data, timeout):
        assert endpoint == panda_usb.ENDPOINT_OUT
        chunk = bytes(data[:5])  # Short writes, as USB may do: the bus must send the rest
        self.sent += chunk
        return len(chunk)

    def receive(self, *packets):
        self.rx.put(b''.join(pack_packet(packet) for packet in packets))


def requests(device, request):
    return [(value, index) for code, value, index in device.controls if code == request]


async def test_opens_all_buses_and_receives():
    device = FakePanda(debug_firmware=True)
    bus = PandaBus(bitrate=500_000, device=device)
    try:
        assert requests(device, panda_usb.REQ_DISABLE_HEARTBEAT) == [(0, 0)]
        assert requests(device, panda_usb.REQ_SET_CAN_SPEED) == [(0, 5000), (1, 5000), (2, 5000)]
        assert requests(device, panda_usb.REQ_SET_CANFD_AUTO) == [(0, 0), (1, 0), (2, 0)]
        assert bus.safety_mode == panda_usb.SAFETY_ALLOUTPUT
        assert 'red panda' in bus.channel_info and device.serial_number in bus.channel_info
        device.receive(
            PandaPacket(0x123, b'\x01', 0),
            PandaPacket(0x18FEF100, b'\x02' * 8, 2, extended=True),
            PandaPacket(0x124, b'\x03', 1, returned=True),  # An echo: hidden without receive_own_messages
        )
        first, second = bus.recv(2), bus.recv(2)
        assert (first.arbitration_id, first.channel, first.data, first.is_rx) == (0x123, 0, b'\x01', True)
        assert (second.arbitration_id, second.channel, second.is_extended_id) == (0x18FEF100, 2, True)
        assert bus.recv(0.05) is None
    finally:
        bus.shutdown()
    assert requests(device, panda_usb.REQ_SET_SAFETY_MODE)[-1] == (panda_usb.SAFETY_SILENT, 0)


async def test_sends_on_the_message_channel_with_short_writes():
    device = FakePanda(debug_firmware=True)
    bus = PandaBus(device=device)
    try:
        bus.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, data=b'\x02\x10\x03', channel=2))
        assert bytes(device.sent) == reference_pack(0x7E0, b'\x02\x10\x03', 2)
        with pytest.raises(can.CanOperationError, match='CAN FD'):
            bus.send(can.Message(arbitration_id=0x1, is_fd=True, data=bytes(12)))
        with pytest.raises(can.CanOperationError):
            bus.send(can.Message(arbitration_id=0x1, is_remote_frame=True))
    finally:
        bus.shutdown()


async def test_single_bus_fd_and_own_messages():
    device = FakePanda(debug_firmware=True)
    bus = PandaBus('@1', bitrate=500_000, fd=True, data_bitrate=2_000_000, device=device, receive_own_messages=True)
    try:
        assert requests(device, panda_usb.REQ_SET_CAN_SPEED) == [(1, 5000)]
        assert requests(device, panda_usb.REQ_SET_CAN_DATA_SPEED) == [(1, 20000)]
        bus.send(can.Message(arbitration_id=0x100, is_fd=True, data=bytes(range(20)), channel=0))
        assert bytes(device.sent)[0] & 0x0E == 1 << 1  # Sent on the connection's bus 1, not the message's 0
        device.receive(
            PandaPacket(0x100, bytes(range(20)), 1, fd=True, returned=True),
            PandaPacket(0x200, b'\x01', 0),  # Another bus: filtered out
        )
        echo = bus.recv(2)
        assert echo.is_fd and not echo.is_rx and echo.data == bytes(range(20))
        assert bus.recv(0.05) is None
    finally:
        bus.shutdown()


async def test_release_firmware_falls_back_to_elm327_and_reports_rejected_frames():
    device = FakePanda(debug_firmware=False)
    bus = PandaBus(device=device)
    try:
        assert bus.safety_mode == panda_usb.SAFETY_ELM327
        assert requests(device, panda_usb.REQ_SET_SAFETY_MODE) == [
            (panda_usb.SAFETY_ALLOUTPUT, 0),
            (panda_usb.SAFETY_ELM327, 1),
        ]
        device.receive(PandaPacket(0x321, b'\x01', 0, rejected=True), PandaPacket(0x7E8, b'\x02', 0))
        assert bus.recv(2).arbitration_id == 0x7E8
        assert await wait_by_deadline(lambda: bus.rejected == 1)
    finally:
        bus.shutdown()


async def test_listen_only_is_silent():
    device = FakePanda()
    bus = PandaBus(listen_only=True, device=device)
    try:
        assert requests(device, panda_usb.REQ_SET_SAFETY_MODE) == [(panda_usb.SAFETY_SILENT, 0)]
        with pytest.raises(can.CanOperationError, match='listen only'):
            bus.send(can.Message(arbitration_id=0x1, data=b'\x01'))
    finally:
        bus.shutdown()


def test_refuses_unsupported_hardware_and_settings():
    with pytest.raises(can.CanInitializationError, match='hardware type'):
        PandaBus(device=FakePanda(hw_type=0x03))
    with pytest.raises(can.CanInitializationError, match='bitrates'):
        PandaBus(bitrate=33_333, device=FakePanda())
    with pytest.raises(can.CanInitializationError):
        PandaBus('@5', device=FakePanda())
    bus = PandaBus(device=FakePanda(debug_firmware=True, can_version=0xDEADBEEF))  # Unknown version: a warning
    bus.shutdown()


def test_entry_points_are_registered():
    from importlib.metadata import entry_points

    names = {entry.name: entry.value for entry in entry_points(group='can.interface')}
    assert names['panda'] == 'can_explorer.core.panda_usb:PandaBus'
    assert names['plin'] == 'can_explorer.core.plin:PlinBus'
    assert names['kvaser_lin'] == 'can_explorer.core.kvaser_lin:KvaserLinBus'
    assert names['vector_lin'] == 'can_explorer.core.vector_lin:VectorLinBus'
