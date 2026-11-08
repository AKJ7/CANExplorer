import can

from can_explorer.core.protocols.nm import NmDecoder, parse


def _frame(data):
    return can.Message(arbitration_id=0x400, is_extended_id=False, data=bytes(data))


def test_parse_source_and_control_flags():
    pdu = parse(bytes([0x12, 0x11, 0, 0, 0, 0, 0, 0]))  # control 0x11 = RepeatMessage + ActiveWakeup
    assert pdu.source_node == 0x12
    assert pdu.repeat_message and pdu.active_wakeup
    assert not pdu.sleep_ready and not pdu.partial_network_info


def test_partial_network_requests():
    # Control 0x80 = Partial Network Information; bytes 2-3 are the PNC bitmask (PNC 0, 2, then 8+1 = 9)
    pdu = parse(bytes([0x05, 0x80, 0b00000101, 0b00000010, 0, 0, 0, 0]))
    assert pdu.partial_network_info and pdu.pn_requests == (0, 2, 9)


def test_describe_and_decoder():
    decoder = NmDecoder()
    assert (
        decoder.describe(_frame([0x12, 0x01, 0, 0, 0, 0, 0, 0]))
        == 'NM node 0x12 [RepeatMessage], data 00 00 00 00 00 00'
    )
    assert 'ActiveWakeup' in decoder.describe(_frame([0x0A, 0x10, 0, 0, 0, 0, 0, 0]))
    assert decoder.describe(_frame([0x05, 0x80, 0x05, 0, 0, 0, 0, 0])) == 'NM node 0x05 PN requests #0, #2'
    assert decoder.describe(_frame([])) is None


def test_configurable_positions():
    # Some projects put the control vector at byte 0 and the node id at byte 1
    pdu = parse(bytes([0x01, 0x7F, 0, 0]), source_pos=1, control_pos=0)
    assert pdu.source_node == 0x7F and pdu.repeat_message
    assert parse(bytes([0x00]), source_pos=1, control_pos=0) is None  # Too short
