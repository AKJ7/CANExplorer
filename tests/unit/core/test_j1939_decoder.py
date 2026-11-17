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


def test_rts_cts_with_retransmission():
    decoder = J1939Decoder()
    payload = bytes(range(20))  # 3 packets
    assert 'TP.RTS 20 B in 3 packets for PGN 0x0EF00' in decoder.describe(
        msg(0x1CEC4200, [16, 20, 0, 3, 0xFF, 0x00, 0xEF, 0x00])
    )
    assert 'TP.CTS 3 packets from #1' in decoder.describe(msg(0x1CEC0042, [17, 3, 1, 0xFF, 0xFF, 0x00, 0xEF, 0x00]))
    decoder.describe(msg(0x1CEB4200, [1, *payload[0:7]]))
    decoder.describe(msg(0x1CEB4200, [2, *b'garbage']))
    # The receiver asks for packet 2 again; the repeated packet replaces the bad one
    assert 'TP.CTS 2 packets from #2' in decoder.describe(msg(0x1CEC0042, [17, 2, 2, 0xFF, 0xFF, 0x00, 0xEF, 0x00]))
    decoder.describe(msg(0x1CEB4200, [2, *payload[7:14]]))
    complete = decoder.describe(msg(0x1CEB4200, [3, *payload[14:20], 0xFF]))
    assert 'TP.DT #3: complete PGN 0x0EF00 (20 B): 00 01 02' in complete
    eoma = decoder.describe(msg(0x1CEC0042, [19, 20, 0, 3, 0xFF, 0x00, 0xEF, 0x00]))
    assert eoma.endswith('TP.EndOfMsgAck 20 B in 3 packets for PGN 0x0EF00')
    hold = decoder.describe(msg(0x1CEC0042, [17, 0, 0xFF, 0xFF, 0xFF, 0x00, 0xEF, 0x00]))
    assert 'hold the connection open' in hold


def test_extended_transport_protocol():
    decoder = J1939Decoder()
    payload = bytes(i % 251 for i in range(1800))  # 258 packets, over the 1785 bytes of TP
    size = len(payload).to_bytes(4, 'little')
    assert decoder.describe(msg(0x1CC84200, [20, *size, 0x00, 0xD7, 0x00])).endswith(
        'ETP.RTS 1800 B in 258 packets for PGN 0x0D700'
    )
    complete = None
    for first in range(1, 259, 255):  # Two blocks: packets 1-255 and 256-258
        count = min(255, 258 - first + 1)
        cts = decoder.describe(msg(0x1CC80042, [21, count, *first.to_bytes(3, 'little'), 0x00, 0xD7, 0x00]))
        assert f'ETP.CTS {count} packets from #{first}' in cts
        offset = (first - 1).to_bytes(3, 'little')
        assert f'ETP.DPO {count} packets after offset {first - 1}' in decoder.describe(
            msg(0x1CC84200, [22, count, *offset, 0x00, 0xD7, 0x00])
        )
        for sequence in range(1, count + 1):
            start = (first - 1 + sequence - 1) * 7
            complete = decoder.describe(msg(0x1CC74200, [sequence, *payload[start : start + 7].ljust(7, b'\xff')]))
    assert 'ETP.DT #3: complete PGN 0x0D700 (1800 B): 00 01 02' in complete
    eoma = decoder.describe(msg(0x1CC80042, [23, *size, 0x00, 0xD7, 0x00]))
    assert eoma.endswith('ETP.EndOfMsgAck 1800 B for PGN 0x0D700')
    assert 'ETP.DT #1 (no session)' in decoder.describe(msg(0x1CC74200, [1, 0, 0, 0, 0, 0, 0, 0]))


def test_abort_acknowledgment_and_commanded_address():
    from can_explorer.core.j1939_tools import J1939Name

    decoder = J1939Decoder()
    abort = decoder.describe(msg(0x1CEC0042, [255, 3, 0xFF, 0xFF, 0xFF, 0x00, 0xEF, 0x00]))
    assert abort.endswith('TP.Abort (timeout) for PGN 0x0EF00')
    nack = decoder.describe(msg(0x18E8FF00, [1, 0xFF, 0xFF, 0xFF, 0xF9, 0xDA, 0xFE, 0x00]))
    assert nack.endswith('Acknowledgment SA=0x00 DA=0xFF P6: NACK for PGN 0x0FEDA requested by 0xF9')
    name = J1939Name(0x8000_0000_0012_3456)
    commanded = decoder.describe(msg(0x1CECFFF9, [32, 9, 0, 2, 0xFF, 0xD8, 0xFE, 0x00]))  # Nine bytes: BAM
    assert 'TP.BAM 9 B in 2 packets for PGN 0x0FED8 Commanded Address' in commanded
    decoder.describe(msg(0x1CEBFFF9, [1, *name.to_bytes()[:7]]))
    complete = decoder.describe(msg(0x1CEBFFF9, [2, name.to_bytes()[7], 0x80, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]))
    assert complete.endswith('→ address 0x80')
