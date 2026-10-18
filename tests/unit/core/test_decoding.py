import can
import pytest

from can_explorer.core.decoding import DbcDecoder, FrameDecoder
from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder


@pytest.fixture
def dbc(data_dir):
    return DbcDecoder(data_dir / 'example.dbc')


def test_decodes_standard_frame(dbc):
    msg = can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([0x00, 0x10, 90, 2, 0, 0, 0, 0]))
    assert dbc.describe(msg) == 'EngineData: EngineSpeed=1024 rpm, CoolantTemp=50 degC, GearPosition=Second'
    assert dbc.decode(msg)['CoolantTemp'] == 50


def test_decodes_extended_frame(dbc):
    msg = can.Message(arbitration_id=0x18FEF1FE, data=bytes([0, 0x00, 0x30, 0, 0, 0, 0, 0]))
    assert dbc.describe(msg) == 'VehicleSpeed: WheelSpeed=48 km/h'


def test_unknown_or_mismatched_frames(dbc):
    assert dbc.describe(can.Message(arbitration_id=0x555, is_extended_id=False)) is None
    assert dbc.describe(can.Message(arbitration_id=0x100, is_extended_id=True, data=bytes(8))) is None


def test_frame_decoder_combines_protocol_and_database(dbc):
    decoder = FrameDecoder(create_protocol_decoder('J1939'), dbc)
    msg = can.Message(arbitration_id=0x18FEF1FE, data=bytes([0, 0x00, 0x30, 0, 0, 0, 0, 0]))
    assert decoder.describe(msg) == 'PGN 0x0FEF1 CCVS1 SA=0xFE DA=0xFF P6 | VehicleSpeed: WheelSpeed=48 km/h'
    assert decoder.describe(can.Message(is_error_frame=True)) == ''


def test_protocol_registry():
    assert create_protocol_decoder('Raw') is None
    for name in PROTOCOL_DECODERS:
        decoder = create_protocol_decoder(name)
        assert decoder is None or decoder.name == name
    with pytest.raises(ValueError):
        create_protocol_decoder('Unknown')
