import can

from can_explorer.core.protocols.j1587 import J1587Decoder, decode_j1587


def checksum(body: bytes) -> int:
    return (-sum(body)) & 0xFF


def test_single_byte_and_two_byte_pids():
    # MID 128 (Engine #1), PID 84 road speed 60 mph (raw 120), PID 190 engine speed 1500 rpm (raw 6000)
    body = bytes([128, 84, 120, 190, *(6000).to_bytes(2, 'little')])
    message = body + bytes([checksum(body)])
    description = decode_j1587(message)
    assert description == 'MID 128 Engine #1, PID 84 Road Speed 60 mph, PID 190 Engine Speed 1500 rpm'


def test_variable_length_pid():
    # PID 200 is variable length: a count byte (3) precedes its 3 data bytes
    body = bytes([130, 200, 3, 0xAA, 0xBB, 0xCC])
    description = decode_j1587(body + bytes([checksum(body)]))
    assert description == 'MID 130 Transmission, PID 200=aabbcc'


def test_unknown_pid_shows_raw_bytes():
    body = bytes([128, 100, 0x40])  # PID 100 Engine Oil Pressure: named but not scaled -> raw
    description = decode_j1587(body + bytes([checksum(body)]))
    assert description == 'MID 128 Engine #1, PID 100 Engine Oil Pressure=40'


def test_decoder_describe_and_short_message():
    description = J1587Decoder().describe(can.Message(arbitration_id=0, data=bytes([140, 91, 125])))
    assert description == 'MID 140 Instrument Cluster, PID 91 Accelerator Pedal 50 %'
    assert J1587Decoder().describe(can.Message(arbitration_id=0, data=b'')) is None
