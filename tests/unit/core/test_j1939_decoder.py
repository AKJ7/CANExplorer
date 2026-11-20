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


def _fd(can_id, data):
    return can.Message(arbitration_id=can_id, is_extended_id=True, is_fd=True, data=bytes(data))


def _reference_frames(data_dir):
    """The J1939-22 frames python-can-j1939 sends (tests/data/j1939_22_frames.txt), in bus order."""
    frames = []
    for line in (data_dir / 'j1939_22_frames.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            identifier, *data = line.split()
            frames.append(_fd(int(identifier, 16), [int(byte, 16) for byte in data]))
    return frames


def test_j1939_22_decodes_the_reference_implementation(data_dir):
    """Frames from python-can-j1939's J1939-22 code: what CANExplorer reads matches what that library's receiver
    reassembled (Multi-PG contents, a 130 byte BAM and a 150 byte RTS/CTS transfer)."""
    from can_explorer.core.protocols.j1939 import J1939_22Decoder

    decoder = J1939_22Decoder()
    texts = [decoder.describe(frame) for frame in _reference_frames(data_dir)]
    assert texts[0] == (
        'PGN 0x02500 Multi-PG SA=0x00 DA=0xFF P3: 2 PGs: PGN 0x0FEF1 CCVS1: FF 00 3C 00 00 00 00 FF'
        ' | PGN 0x0F004 EEC1: 01 02 03 04 05 06 07 08'
    )
    # A destination-specific PG inside a Multi-PG: the frame's DA addresses it, its contents decode as a request
    assert texts[1] == (
        'PGN 0x02500 Multi-PG SA=0x00 DA=0x42 P6: 1 PG: PGN 0x0EA00 Request: EC FE 00'
        ' — requests PGN 0x0FEEC Vehicle Identification'
    )
    assert texts[2].endswith('FD.TP.BAM session 0: 130 B in 3 segments for PGN 0x0FEEC Vehicle Identification')
    assert texts[4].endswith('FD.TP.DT session 0 #2/3')
    assert 'complete PGN 0x0FEEC Vehicle Identification (130 B)' in texts[5]
    assert 'text "1HGCM82633A004352*' in texts[5] and texts[5].endswith('*1234"')
    assert texts[6].endswith(
        'FD.TP.EndOfMsgStatus session 0: 130 B in 3 segments for PGN 0x0FEEC Vehicle Identification'
    )
    assert texts[7].endswith('FD.TP.RTS session 0: 150 B in 3 segments for PGN 0x0EF00, at most 3 per CTS')
    assert (
        texts[8] == 'PGN 0x04D00 FD.TP.CM SA=0x42 DA=0x00 P7: FD.TP.CTS session 0: 3 segments from #1 for PGN 0x0EF00'
    )
    assert 'FD.TP.DT session 0 #3/3: complete PGN 0x0EF00 (150 B): 00 01 02' in texts[11]
    assert texts[13].endswith('FD.TP.EndOfMsgAck session 0: 150 B in 3 segments for PGN 0x0EF00')


def test_j1939_22_reassembled_payloads_are_exact(data_dir):
    from can_explorer.core.protocols.j1939 import FdTransportTracker, J1939Id

    tracker = FdTransportTracker()
    complete = []
    for frame in _reference_frames(data_dir):
        result = tracker.feed(J1939Id.from_can_id(frame.arbitration_id), bytes(frame.data))
        if result is not None and result[1] is not None:
            complete.append(result[1])
    assert complete == [
        (0xFEEC, 0x00, 0xFF, b'1HGCM82633A004352*' * 7 + b'1234'),
        (0xEF00, 0x00, 0x42, bytes(range(150))),
    ]


def test_split_multi_pg_stops_at_padding_and_flags_assurance_data():
    from can_explorer.core.protocols.j1939 import CPg, split_multi_pg

    plain = [0x40, 0xFE, 0xF1, 2, 0xAB, 0xCD]
    secured = [(3 << 5) | (1 << 2), 0xF0, 0x04, 3, 1, 2, 3]  # Type of service 3, trailer format 1
    padding = [0x00, 0x00, 0x00, 0xAA, 0xAA]  # Type of service 0: the rest is padding
    pgs = split_multi_pg(bytes(plain + secured + padding))
    assert pgs == [CPg(2, 0, 0xFEF1, b'\xab\xcd'), CPg(3, 1, 0xF004, b'\x01\x02\x03')]
    assert pgs[0].plain and not pgs[1].plain
    assert split_multi_pg(bytes([0x40, 0xFE, 0xF1, 8, 1, 2])) == [CPg(2, 0, 0xFEF1, b'\x01\x02', True)]
    # The CPGN's top two bits (data page and extended data page) come from byte 0
    assert split_multi_pg(bytes([0x41, 0xFE, 0xF1, 0]))[0].pgn == 0x1FEF1


def test_j1939_22_multi_pg_labels_what_it_does_not_decode():
    from can_explorer.core.protocols.j1939 import J1939_22Decoder

    decoder = J1939_22Decoder()
    secured = [(3 << 5) | (1 << 2), 0xF0, 0x04, 3, 1, 2, 3]
    text = decoder.describe(_fd(0x18250000 | 0xFF00, secured + [0x40, 0xFE, 0xF1, 4, 1]))
    assert 'PGN 0x0F004 EEC1 (type of service 3, trailer format 1: assurance data not decoded): 01 02 03' in text
    assert 'PGN 0x0FEF1 CCVS1: 01 (truncated)' in text
    assert decoder.describe(_fd(0x1825FF00, [0, 0, 0, 0xAA])).endswith(': no parameter groups (padding only)')


def test_j1939_22_fd_transport_retransmission_abort_and_errors():
    from can_explorer.core.protocols.j1939 import J1939_22Decoder

    decoder = J1939_22Decoder()
    rts = [0x10 | 0, 100, 0, 0, 2, 0, 0, 2, 0, 0x00, 0xEF, 0x00]  # Session 1: 100 B in 2 segments of PGN 0xEF00
    assert 'FD.TP.RTS session 1: 100 B in 2 segments for PGN 0x0EF00, at most 2 per CTS' in decoder.describe(
        _fd(0x184D4200, rts)
    )
    decoder.describe(_fd(0x1C4E4200, [0x10, 1, 0, 0, *b'x' * 60]))
    # The receiver asks for segment 1 again: the repeated segment replaces the first
    cts = [0x10 | 1, 0xFF, 0xFF, 0xFF, 1, 0, 0, 2, 0, 0x00, 0xEF, 0x00]
    assert 'FD.TP.CTS session 1: 2 segments from #1' in decoder.describe(_fd(0x1C4D0042, cts))
    decoder.describe(_fd(0x1C4E4200, [0x10, 1, 0, 0, *range(60)]))
    done = decoder.describe(_fd(0x1C4E4200, [0x10, 2, 0, 0, *range(60, 100), *[0xFF] * 4]))
    assert 'FD.TP.DT session 1 #2/2: complete PGN 0x0EF00 (100 B): 00 01 02' in done
    hold = [0x10 | 1, 0xFF, 0xFF, 0xFF, 1, 0, 0, 0, 0, 0x00, 0xEF, 0x00]
    assert 'hold the connection open' in decoder.describe(_fd(0x1C4D0042, hold))
    # A segment out of order ends the session; data without a session is reported
    decoder.describe(_fd(0x184D4200, rts))
    assert 'expected #1, session aborted' in decoder.describe(_fd(0x1C4E4200, [0x10, 2, 0, 0, 0]))
    assert '(no session)' in decoder.describe(_fd(0x1C4E4200, [0x10, 1, 0, 0, 0]))
    abort = [0x10 | 15, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 3, 0x00, 0xEF, 0x00]
    assert 'FD.TP.Abort session 1 (timeout) for PGN 0x0EF00' in decoder.describe(_fd(0x1C4D0042, abort))
    assert 'FD.TP.CM too short (8 B)' in decoder.describe(_fd(0x1C4D0042, bytes(8)))
    # Assurance data is named, not checked
    secured_bam = [0x20 | 4, 70, 0, 0, 2, 0, 0, 0xFF, 1, 0xEC, 0xFE, 0x00]
    assert 'with manufacturer cybersecurity assurance data (not checked)' in decoder.describe(
        _fd(0x184DFF00, secured_bam)
    )
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
