import can

from can_explorer.core.protocols.isotp import IsoTpDecoder


def msg(data, can_id=0x7E8, fd=False):
    return can.Message(arbitration_id=can_id, is_extended_id=False, is_fd=fd, data=bytes(data))


def test_single_frame():
    assert IsoTpDecoder().describe(msg([0x03, 0x41, 0x0D, 0x32, 0, 0, 0, 0])) == 'ISO-TP SF (3 B): 41 0D 32'


def test_segmented_transfer_is_reassembled():
    decoder = IsoTpDecoder()
    payload = bytes(range(20))
    assert decoder.describe(msg([0x10, 20, *payload[:6]])) == 'ISO-TP FF (total 20 B)'
    assert decoder.describe(msg([0x30, 0, 0])) == 'ISO-TP FC CTS BS=0 STmin=0x00'
    assert decoder.describe(msg([0x21, *payload[6:13]])) == 'ISO-TP CF sn=1 (13/20 B)'
    expected = ' '.join(f'{b:02X}' for b in payload)
    assert decoder.describe(msg([0x22, *payload[13:20], 0xAA])) == f'ISO-TP complete (20 B): {expected}'


def test_wrong_sequence_number_aborts():
    decoder = IsoTpDecoder()
    decoder.describe(msg([0x10, 20, 0, 0, 0, 0, 0, 0]))
    assert 'transfer aborted' in decoder.describe(msg([0x22, 0, 0, 0, 0, 0, 0, 0]))
    assert 'no transfer' in decoder.describe(msg([0x23, 0, 0, 0, 0, 0, 0, 0]))


def test_can_fd_single_frame_escape():
    data = [0x00, 12, *range(12)] + [0] * 10
    assert IsoTpDecoder().describe(msg(data, fd=True)).startswith('ISO-TP SF (12 B): 00 01')


def test_non_isotp_frames():
    decoder = IsoTpDecoder()
    assert decoder.describe(msg([])) is None
    assert decoder.describe(msg([0x00, 1, 2])) is None
    assert decoder.describe(msg([0x07, 1, 2])) is None
    assert decoder.describe(msg([0xF0, 1, 2])) is None


def test_extended_addressing_decoder():
    from can_explorer.core.protocols import PROTOCOL_DECODERS

    decoder = PROTOCOL_DECODERS['ISO-TP (extended)']()

    def frame(data, can_id=0x6F1):
        return can.Message(arbitration_id=can_id, is_extended_id=False, data=bytes(data))

    assert decoder.describe(frame([0x12, 0x02, 0x10, 0x03])) == 'ISO-TP [12] SF (2 B): 10 03'
    assert decoder.describe(frame([0x12, 0x10, 0x09, 1, 2, 3, 4, 5, 6])) == 'ISO-TP [12] FF (total 9 B)'
    assert decoder.describe(frame([0xF1, 0x30, 0x00, 0x00], 0x612)) == 'ISO-TP [F1] FC CTS BS=0 STmin=0x00'
    # A transfer to another node on the same identifier does not mix with the first
    assert 'no transfer in progress' in decoder.describe(frame([0x40, 0x21, 7, 8, 9]))
    assert (
        decoder.describe(frame([0x12, 0x21, 7, 8, 9, 0, 0, 0]))
        == 'ISO-TP [12] complete (9 B): 01 02 03 04 05 06 07 08 09'
    )


def test_uds_and_kwp_with_extended_addressing():
    from can_explorer.core.protocols import PROTOCOL_DECODERS

    uds = PROTOCOL_DECODERS['UDS (extended)']()
    text = uds.describe(can.Message(arbitration_id=0x6F1, is_extended_id=False, data=bytes([0x12, 0x02, 0x10, 0x03])))
    assert text.startswith('ISO-TP [12] SF (2 B): 10 03 | UDS DiagnosticSessionControl')
    assert hasattr(uds, 'description')  # Takes a diagnostic description like the UDS decoder
    kwp = PROTOCOL_DECODERS['KWP2000 (extended)']()
    text = kwp.describe(can.Message(arbitration_id=0x6F1, is_extended_id=False, data=bytes([0x12, 0x02, 0x10, 0x81])))
    assert '| KWP StartDiagnosticSession' in text


def test_classic_frame_zero_length_is_not_an_fd_escape():
    decoder = IsoTpDecoder()
    assert (
        decoder.describe(can.Message(arbitration_id=0x7E0, is_extended_id=False, data=bytes([0, 2, 0x10, 3]))) is None
    )
