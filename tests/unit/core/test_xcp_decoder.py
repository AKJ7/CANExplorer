import can

from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder
from can_explorer.core.protocols.xcp import CcpDecoder, XcpDecoder


def frame(identifier, data):
    return can.Message(arbitration_id=identifier, is_extended_id=False, data=bytes(data))


def test_registered():
    assert 'XCP' in PROTOCOL_DECODERS and 'CCP' in PROTOCOL_DECODERS
    assert isinstance(create_protocol_decoder('XCP'), XcpDecoder)


def test_xcp_learns_the_pair_from_connect():
    decoder = XcpDecoder()
    assert decoder.describe(frame(0x100, [0x11, 0x22])) is None
    assert decoder.describe(frame(0x7E0, [0xFF, 0x00])) == 'XCP? CONNECT mode 0'
    connect = decoder.describe(frame(0x7E1, [0xFF, 0x05, 0x01, 0x08, 0x00, 0x08, 0x01, 0x01]))
    assert connect == 'XCP RES CONNECT: CAL/PAG, DAQ, big endian, granularity 1, MAX_CTO 8, MAX_DTO 8'
    assert decoder.describe(frame(0x7E0, [0xF4, 2, 0, 0, 0, 0, 0x10, 0x00])) == 'XCP SHORT_UPLOAD 2 @ 0x00001000 ext 0'
    assert decoder.describe(frame(0x7E1, [0xFF, 0x17, 0x70, 0, 0, 0, 0, 0])) == 'XCP RES data: 17 70'
    assert decoder.describe(frame(0x7E1, [0xFE, 0x25])) == 'XCP ERR access locked (seed & key required)'
    assert decoder.describe(frame(0x7E1, [0x03, 1, 2])) == 'XCP DAQ PID 3: 01 02'
    assert decoder.describe(frame(0x7E1, [0xFD, 0x06])) == 'XCP EV DAQ overload'
    assert decoder.describe(frame(0x200, [0xFF])) is None
    decoder.reset()
    assert decoder.describe(frame(0x7E1, [0xFF])) is None  # Learnt again after a reset


def test_xcp_configured():
    decoder = XcpDecoder()
    decoder.configure(0x600, 0x601)
    assert decoder.describe(frame(0x600, [0xF8, 0, 0x01])) == 'XCP GET_SEED first part, CAL/PAG'
    assert decoder.describe(frame(0x601, [0xFF, 4, 1, 2, 3, 4])) == 'XCP RES seed (4 bytes left): 01 02 03 04'
    decoder.reset()
    assert decoder.describe(frame(0x600, [0xFD])) == 'XCP GET_STATUS'  # A configured pair survives a reset


def test_ccp():
    decoder = CcpDecoder()
    assert decoder.describe(frame(0x7E0, [0x01, 0x05, 0x39, 0x00])) == 'CCP? CONNECT #5 station 0x0039'
    assert decoder.describe(frame(0x7E1, [0xFF, 0x00, 0x05])) == 'CCP CRM #5 ok'
    assert decoder.describe(frame(0x7E0, [0x0F, 6, 2, 0, 0, 0, 0x10, 0])) == 'CCP SHORT_UP #6 2 @ 0x00001000 ext 0'
    assert decoder.describe(frame(0x7E1, [0xFF, 0x00, 6, 0x12, 0x34])) == 'CCP CRM #6 ok: 12 34'
    assert decoder.describe(frame(0x7E1, [0xFF, 0x35, 7])) == 'CCP CRM #7 access locked (seed & key required)'
    assert decoder.describe(frame(0x7E1, [0xFE, 0x20])) == 'CCP event cold start request'
    assert decoder.describe(frame(0x7E1, [0x08, 1, 2])) == 'CCP DAQ PID 8: 01 02'
