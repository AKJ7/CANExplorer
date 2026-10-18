import can

from can_explorer.core.protocols.j1939 import GLOBAL_ADDRESS, J1939Decoder, J1939Id


def msg(can_id, data):
    return can.Message(arbitration_id=can_id, is_extended_id=True, data=bytes(data))


def test_identifier_fields():
    assert J1939Id.from_can_id(0x18FEF100) == J1939Id(6, 0xFEF1, 0x00, GLOBAL_ADDRESS)
    assert J1939Id.from_can_id(0x18EA2133) == J1939Id(6, 0xEA00, 0x33, 0x21)
    assert J1939Id.from_can_id(0x0CF00400) == J1939Id(3, 0xF004, 0x00, GLOBAL_ADDRESS)


def test_request():
    description = J1939Decoder().describe(msg(0x18EAFF00, [0xEC, 0xFE, 0x00]))
    assert description == 'PGN 0x0EA00 Request SA=0x00 DA=0xFF P6: requests PGN 0x0FEEC Vehicle Identification'


def test_bam_transfer_is_reassembled():
    decoder = J1939Decoder()
    payload = bytes(range(1, 11))
    announce = decoder.describe(msg(0x1CECFF00, [32, 10, 0, 2, 0xFF, 0xCA, 0xFE, 0x00]))
    assert announce.endswith('TP.BAM 10 B in 2 packets for PGN 0x0FECA DM1')
    assert decoder.describe(msg(0x1CEBFF00, [1, *payload[:7]])).endswith('TP.DT #1/2')
    complete = decoder.describe(msg(0x1CEBFF00, [2, *payload[7:], 0xFF, 0xFF, 0xFF, 0xFF]))
    assert complete.endswith('complete PGN 0x0FECA DM1 (10 B): 01 02 03 04 05 06 07 08 09 0A')


def test_out_of_order_transport_data():
    decoder = J1939Decoder()
    decoder.describe(msg(0x1CECFF00, [32, 10, 0, 2, 0xFF, 0xCA, 0xFE, 0x00]))
    assert 'session aborted' in decoder.describe(msg(0x1CEBFF00, [2, 0, 0, 0, 0, 0, 0, 0]))


def test_standard_frames_are_ignored():
    assert J1939Decoder().describe(can.Message(arbitration_id=0x100, is_extended_id=False)) is None
