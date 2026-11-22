import can

from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder
from can_explorer.core.protocols.tp20 import Tp20Decoder

# The example exchange of the community TP 2.0 description (https://jazdw.net/tp20)
EXAMPLE = """
200 01 C0 00 10 00 03 01
201 00 D0 00 03 40 07 01
740 A0 0F 8A FF 32 FF
300 A1 0F 8A FF 4A FF
740 10 00 02 10 89
300 B1
300 10 00 02 50 89
740 B1
740 11 00 02 21 01
300 B2
300 21 00 1A 61 01 01 00 00
300 22 27 00 00 22 00 80 1A
300 23 32 4B 25 02 7A 25 00
300 14 00 25 00 00 25 00 00
740 B5
740 A8
"""


def frame(can_id: int, data: str, extended: bool = False) -> can.Message:
    return can.Message(arbitration_id=can_id, is_extended_id=extended, data=bytes.fromhex(data))


def replay(decoder, text=EXAMPLE):
    lines = [line.split(maxsplit=1) for line in text.strip().splitlines()]
    return [decoder.describe(frame(int(can_id, 16), data)) for can_id, data in lines]


def test_registered():
    assert PROTOCOL_DECODERS['VW TP 2.0'] is Tp20Decoder
    assert isinstance(create_protocol_decoder('VW TP 2.0'), Tp20Decoder)


def test_example_exchange():
    out = replay(Tp20Decoder())
    assert out[0] == 'TP2.0 channel setup request to module 0x01: listen on any, transmit on 0x300, application 0x01'
    assert 'accepted' in out[1] and 'listen on 0x300, transmit on 0x740' in out[1]
    assert out[2] == 'TP2.0 tester to 0x01 parameters request: block size 15, T1 100 ms, T3 5 ms'
    assert out[3] == 'TP2.0 0x01 to tester parameters response: block size 15, T1 100 ms, T3 10 ms'
    assert out[4].endswith('| KWP StartDiagnosticSession 89')
    assert out[5] == 'TP2.0 0x01 to tester ACK, ready, next sequence 1'
    assert out[6].endswith('| KWP Positive response to StartDiagnosticSession: 89')
    assert out[10] == 'TP2.0 0x01 to tester data #1 (more follow), 26 B message'
    assert out[13].startswith('TP2.0 0x01 to tester data #4 (last, ACK requested)')
    assert '| KWP Positive response to ReadDataByLocalIdentifier: 01 01 00 00 27' in out[13]
    assert out[-1] == 'TP2.0 tester to 0x01 disconnect'


def test_data_needs_a_setup_first():
    decoder = Tp20Decoder()
    assert decoder.describe(frame(0x740, '10 00 02 10 89')) is None
    assert decoder.describe(frame(0x300, 'B1')) is None


def test_not_tp20():
    decoder = Tp20Decoder()
    assert decoder.describe(frame(0x123, '01 C0 00 10 00 03 01')) is None
    assert decoder.describe(frame(0x200, '01 C0')) is None  # Too short for a setup
    assert decoder.describe(frame(0x200, '01 55 00 00 00 00 00')) is None
    assert decoder.describe(frame(0x200, '01 C0 00 10 00 03 01', extended=True)) is None
    assert decoder.describe(can.Message(arbitration_id=0x200, is_extended_id=False, data=b'')) is None


def test_negative_setup_and_broadcast_are_labelled():
    decoder = Tp20Decoder()
    assert 'refused (0xD6' in decoder.describe(frame(0x201, '00 D6 00 10 00 10 01'))
    text = decoder.describe(frame(0x200, '01 23 1A 9B 00 55 55'))
    assert text.startswith('TP2.0 broadcast request, module 0x01') and 'not expanded' in text


def test_sequence_gap_drops_the_message():
    decoder = Tp20Decoder()
    replay(decoder, '\n'.join(EXAMPLE.strip().splitlines()[:2]))
    decoder.describe(frame(0x300, '21 00 1A 61 01 01 00 00'))
    gap = decoder.describe(frame(0x300, '23 32 4B 25 02 7A 25 00'))  # #2 missing
    assert 'sequence gap' in gap
    assert '| KWP' not in decoder.describe(frame(0x300, '14 00 25 00 00 25 00 00'))


def test_reset_forgets_channels():
    decoder = Tp20Decoder()
    replay(decoder)
    decoder.reset()
    assert decoder.describe(frame(0x740, '10 00 02 10 89')) is None
