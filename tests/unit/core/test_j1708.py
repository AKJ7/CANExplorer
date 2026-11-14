import queue

import can
import pytest

from can_explorer.core.j1708 import (
    DEFAULT_BAUDRATE,
    MAX_MESSAGE_LENGTH,
    J1708Bus,
    J1708FrameAssembler,
    frame_to_message,
    has_valid_checksum,
    j1708_checksum,
    message_to_frame,
    parse_channel,
)


def _frame(*body: int) -> bytes:
    """A J1708 message: the given bytes plus the checksum that makes the whole sum a multiple of 256."""
    data = bytes(body)
    return data + bytes([j1708_checksum(data)])


def test_checksum():
    assert j1708_checksum(b'\x80\x54\x64') == (-(0x80 + 0x54 + 0x64)) & 0xFF
    frame = _frame(0x80, 0x54, 0x64)
    assert sum(frame) % 256 == 0
    assert has_valid_checksum(frame)
    assert not has_valid_checksum(frame[:-1] + bytes([(frame[-1] + 1) & 0xFF]))
    assert not has_valid_checksum(b'\x80')  # Too short


def test_parse_channel():
    assert parse_channel('/dev/ttyUSB0') == ('/dev/ttyUSB0', DEFAULT_BAUDRATE)
    assert parse_channel('COM5@19200') == ('COM5', 19200)
    with pytest.raises(can.CanInitializationError):
        parse_channel('/dev/ttyUSB0@fast')


def test_frame_message_round_trip():
    frame = _frame(0x80, 0x54, 0x64)
    msg = frame_to_message(frame, 'j1708')
    assert msg.arbitration_id == 0x80 and not msg.is_extended_id
    assert bytes(msg.data) == frame and msg.dlc == len(frame)
    assert not msg.is_fd

    long_frame = _frame(0x80, *range(10))  # Over eight bytes → CAN FD so it fits
    assert frame_to_message(long_frame).is_fd

    # A frame already carrying a valid checksum is sent unchanged; otherwise the checksum is appended.
    assert message_to_frame(can.Message(arbitration_id=0x80, data=frame)) == frame
    body = can.Message(arbitration_id=0x80, data=bytes([0x80, 0x54, 0x64]))
    assert message_to_frame(body) == frame


def test_assembler():
    assembler = J1708FrameAssembler()
    assert assembler.feed(b'\x80\x54\x64') == []  # Buffered, no boundary yet
    assert assembler.flush() == b'\x80\x54\x64'  # The idle gap completes the message
    assert assembler.flush() is None

    # The buffer never grows past the J1708 maximum: a frame is emitted at that length.
    emitted = assembler.feed(bytes(range(MAX_MESSAGE_LENGTH + 3)))
    assert len(emitted) == 1 and len(emitted[0]) == MAX_MESSAGE_LENGTH
    assert assembler.flush() == bytes(range(MAX_MESSAGE_LENGTH, MAX_MESSAGE_LENGTH + 3))


class FakeSerial:
    """A serial port fed test bytes: `feed` queues a chunk, `gap` queues an idle period so the next read times out
    (returns no data), exactly as the worker uses to end a message."""

    def __init__(self):
        self._items: queue.Queue = queue.Queue()
        self.written = bytearray()
        self.baudrate = DEFAULT_BAUDRATE

    def feed(self, data):
        self._items.put(bytes(data))

    def gap(self):
        self._items.put(None)

    @property
    def in_waiting(self):
        return 0

    def read(self, _n):
        try:
            item = self._items.get(timeout=0.05)
        except queue.Empty:
            return b''
        return b'' if item is None else item

    def write(self, data):
        self.written += bytes(data)
        return len(data)

    def close(self):
        pass


@pytest.fixture
def fake_serial(monkeypatch):
    import serial

    fake = FakeSerial()
    monkeypatch.setattr(serial, 'serial_for_url', lambda *a, **k: fake)
    return fake


def test_j1708_bus_receive(fake_serial):
    frame = _frame(0x80, 0x54, 0x64)  # MID 128 (Engine #1), PID 84 road speed
    bus = J1708Bus('/dev/ttyUSB0')
    try:
        assert bus.baudrate == DEFAULT_BAUDRATE
        fake_serial.feed(frame)
        fake_serial.gap()  # The line falls idle: the buffered bytes are one complete message
        msg = bus.recv(2)
        assert msg is not None
        assert msg.arbitration_id == 0x80 and bytes(msg.data) == frame
    finally:
        bus.shutdown()


def test_j1708_bus_send(fake_serial):
    bus = J1708Bus('/dev/ttyUSB0@9600')
    try:
        bus.send(can.Message(arbitration_id=0x80, data=bytes([0x80, 0x54, 0x64])))
        assert bytes(fake_serial.written) == _frame(0x80, 0x54, 0x64)  # Checksum appended
        with pytest.raises(can.CanOperationError):
            bus.send(can.Message(arbitration_id=0x80, is_remote_frame=True))
    finally:
        bus.shutdown()


def test_j1708_detect_is_empty():
    assert J1708Bus._detect_available_configs() == []


def test_fd_rejected(fake_serial):
    with pytest.raises(can.CanInterfaceNotImplementedError):
        J1708Bus('/dev/ttyUSB0', fd=True)
