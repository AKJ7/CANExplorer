from pathlib import Path

import pytest

from can_explorer.core.a2l import A2lVariable, parse_a2l, read_a2l

DATA = Path(__file__).resolve().parents[2] / 'data'


def test_reads_measurements_and_characteristics():
    variables = {v.name: v for v in read_a2l(DATA / 'example.a2l')}
    assert set(variables) == {
        'EngineSpeed',
        'CoolantTemp',
        'BatteryVoltage',
        'IdleTarget',
        'FanOn',
        'InjectorScale',
    }
    speed = variables['EngineSpeed']
    assert (speed.address, speed.data_type, speed.writable, speed.factor, speed.unit) == (
        0x1000,
        'UWORD',
        False,
        0.25,
        'rpm',
    )
    assert variables['CoolantTemp'].offset == -40.0 and variables['CoolantTemp'].data_type == 'SBYTE'
    assert variables['IdleTarget'].writable and variables['IdleTarget'].maximum == 8000
    assert variables['InjectorScale'].data_type == 'FLOAT32_IEEE' and variables['InjectorScale'].is_float


def test_decode_and_encode_round_trip():
    speed = A2lVariable('EngineSpeed', '', 0x1000, 'UWORD', False, 0.25, 0.0, 'rpm')
    assert speed.decode((2000).to_bytes(2, 'little')) == 500.0  # 2000 * 0.25
    assert speed.encode(500.0) == (2000).to_bytes(2, 'little')

    temp = A2lVariable('CoolantTemp', '', 0x1002, 'SBYTE', False, 1.0, -40.0, 'degC')
    assert temp.decode(bytes([60])) == 20.0  # 60 - 40
    assert temp.encode(20.0) == bytes([60])
    assert temp.decode(bytes([0])) == -40.0

    volts = A2lVariable('BatteryVoltage', '', 0x1004, 'UWORD', False, 0.001, 0.0, 'V')
    assert volts.decode((13800).to_bytes(2, 'little')) == pytest.approx(13.8)

    scale = A2lVariable('InjectorScale', '', 0x2004, 'FLOAT32_IEEE', True)
    assert scale.decode(scale.encode(1.25)) == pytest.approx(1.25)

    big = A2lVariable('EngineSpeed', '', 0x1000, 'UWORD', False, 0.25, 0.0)
    assert big.decode((0x1234).to_bytes(2, 'big'), byte_order='big') == 0x1234 * 0.25


def test_encode_clamps_to_the_type():
    fan = A2lVariable('FanOn', '', 0x2002, 'UBYTE', True)
    assert fan.encode(5) == bytes([5]) and fan.encode(300) == bytes([255]) and fan.encode(-1) == bytes([0])


def test_rat_func_linear_conversion():
    text = """
    /begin PROJECT P ""
      /begin MODULE M ""
        /begin COMPU_METHOD RF "" RAT_FUNC "%6.2" "kph"
          COEFFS 0 1 0 0 0 100
        /end COMPU_METHOD
        /begin RECORD_LAYOUT RL FNC_VALUES 1 UWORD ROW_DIR DIRECT /end RECORD_LAYOUT
        /begin MEASUREMENT Speed "" UWORD RF 0 0 0 300 ECU_ADDRESS 0x10 /end MEASUREMENT
      /end MODULE
    /end PROJECT
    """
    speed = parse_a2l(text)[0]
    # int = (1*phys + 0)/100  ->  phys = 100*int
    assert speed.factor == 100.0 and speed.offset == 0.0 and speed.unit == 'kph'


def test_skips_non_scalar_characteristics_and_addressless_measurements():
    text = """
    /begin PROJECT P ""
      /begin MODULE M ""
        /begin RECORD_LAYOUT RL FNC_VALUES 1 UBYTE ROW_DIR DIRECT /end RECORD_LAYOUT
        /begin CHARACTERISTIC Map "a map" MAP 0x30 RL 0 IDENT 0 1 /end CHARACTERISTIC
        /begin MEASUREMENT Virtual "no address" UBYTE IDENT 0 0 0 1 /end MEASUREMENT
      /end MODULE
    /end PROJECT
    """
    assert parse_a2l(text) == []
