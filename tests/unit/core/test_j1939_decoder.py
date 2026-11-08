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
    assert 'complete PGN 0x0FECA DM1 (10 B): 01 02 03 04 05 06 07 08 09 0A — PL on: SPN 1027, FMI 5' in complete


def test_out_of_order_transport_data():
    decoder = J1939Decoder()
    decoder.describe(msg(0x1CECFF00, [32, 10, 0, 2, 0xFF, 0xCA, 0xFE, 0x00]))
    assert 'session aborted' in decoder.describe(msg(0x1CEBFF00, [2, 0, 0, 0, 0, 0, 0, 0]))


def test_standard_frames_are_ignored():
    assert J1939Decoder().describe(can.Message(arbitration_id=0x100, is_extended_id=False)) is None


def test_j1939_22_single_pg_on_can_fd():
    from can_explorer.core.protocols.j1939 import J1939_22Decoder

    decoder = J1939_22Decoder()
    # An ordinary PG (CCVS1) on a 20-byte CAN FD frame decodes like J1939
    fd = can.Message(arbitration_id=0x18FEF100, is_extended_id=True, is_fd=True, data=bytes(range(20)))
    assert decoder.describe(fd) == 'PGN 0x0FEF1 CCVS1 SA=0x00 DA=0xFF P6'


def test_j1939_22_multi_pg_and_fd_transport_are_labelled():
    from can_explorer.core.protocols.j1939 import J1939_22Decoder

    decoder = J1939_22Decoder()
    multi = can.Message(
        arbitration_id=(6 << 26) | (0x2500 << 8) | 0x00, is_extended_id=True, is_fd=True, data=bytes(32)
    )
    assert (
        decoder.describe(multi)
        == 'PGN 0x02500 Multi-PG SA=0x00 DA=0x00 P6: multiplexed PGs, 32 bytes (contained PGs not expanded)'
    )
    tp = can.Message(arbitration_id=(6 << 26) | (0x4D00 << 8) | 0x00, is_extended_id=True, is_fd=True, data=bytes(16))
    assert 'FD.TP.CM' in decoder.describe(tp) and '(not reassembled)' in decoder.describe(tp)
    assert decoder.describe(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes(2))) is None
