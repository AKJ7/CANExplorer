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
