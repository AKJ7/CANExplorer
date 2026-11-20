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
        'BrakePressed',
        'ClutchPressed',
        'WheelSpeeds',
        'LoadAxis',
        'IdleTarget',
        'FanOn',
        'InjectorScale',
        'FanMode',
        'IgnitionCurve',
        'FuelMap',
        'GearRatios',
        'VariantCode',
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


def test_lists_unreadable_characteristics_and_skips_addressless_measurements():
    text = """
    /begin PROJECT P ""
      /begin MODULE M ""
        /begin RECORD_LAYOUT RL FNC_VALUES 1 UBYTE ROW_DIR DIRECT /end RECORD_LAYOUT
        /begin CHARACTERISTIC Map "a map" MAP 0x30 RL 0 IDENT 0 1 /end CHARACTERISTIC
        /begin MEASUREMENT Virtual "no address" UBYTE IDENT 0 0 0 1 /end MEASUREMENT
      /end MODULE
    /end PROJECT
    """
    (variable,) = parse_a2l(text)  # The map is listed, marked unreadable; the measurement has no address
    assert variable.name == 'Map' and variable.unsupported == 'a map needs 2 AXIS_DESCR'


def test_example_details():
    from can_explorer.core.a2l import load_a2l

    document = load_a2l(DATA / 'example.a2l')
    assert document.epk == 'EX-1.0' and document.epk_address == 0x3000 and document.byte_order == 'little'
    xcp = document.xcp
    assert xcp.max_cto == 8 and xcp.byte_order == 'little' and xcp.daq_dynamic and xcp.timestamp_size == 4
    assert [(e.name, e.channel, e.cycle) for e in xcp.events] == [('10ms', 0, 0.01), ('100ms', 1, 0.1)]
    can = xcp.transport('CAN')
    assert (can.master_id, can.slave_id, can.extended, can.baudrate) == (0x7E0, 0x7E1, False, 500000)
    assert xcp.transport('UDP').host == '127.0.0.1' and xcp.transport('UDP').port == 5555
    ccp = document.ccp
    assert (ccp.cro_id, ccp.dto_id, ccp.station_address, ccp.byte_order_code) == (0x7E0, 0x7E1, 1, 1)
    assert ccp.rasters == (('10ms', 0),)
    brake = document.variable('BrakePressed')
    assert brake.bit_mask == 0x01 and brake.decode(bytes([0x03])) == 1.0
    clutch = document.variable('ClutchPressed')
    assert clutch.decode(bytes([0x02])) == 1.0 and clutch.encode(0, current=bytes([0x03])) == bytes([0x01])
    assert document.variable('WheelSpeeds').shape == (4,) and document.variable('WheelSpeeds').size == 8
    fan = document.variable('FanMode')
    assert fan.decode(bytes([2])) == 'On' and fan.decode(bytes([7])) == 'Invalid' and fan.encode('auto') == bytes([1])
    curve = document.variable('IgnitionCurve')
    assert curve.kind == 'CURVE' and curve.axes[0].attribute == 'STD_AXIS' and curve.axes[0].max_points == 8
    fuel = document.variable('FuelMap')
    assert [a.attribute for a in fuel.axes] == ['STD_AXIS', 'COM_AXIS'] and fuel.axes[1].reference == 'LoadAxis'
    assert document.variable('GearRatios').shape == (6,) and document.variable('VariantCode').kind == 'ASCII'


def _module(body: str) -> str:
    return f'/begin PROJECT P "" /begin MODULE M "" {body} /end MODULE /end PROJECT'


def test_conversions():
    from can_explorer.core.a2l import evaluate_formula, parse_a2l_document

    document = parse_a2l_document(
        _module(
            """
        /begin COMPU_METHOD Q "" RAT_FUNC "%6.2" "" COEFFS 1 0 0 0 0 1 /end COMPU_METHOD
        /begin COMPU_METHOD T "" TAB_INTP "%6.2" "" COMPU_TAB_REF TAB /end COMPU_METHOD
        /begin COMPU_TAB TAB "" TAB_INTP 3 0 0 10 100 20 400 /end COMPU_TAB
        /begin COMPU_METHOD N "" TAB_NOINTP "%6.2" "" COMPU_TAB_REF TAB2 /end COMPU_METHOD
        /begin COMPU_TAB TAB2 "" TAB_NOINTP 2 1 5 2 7 DEFAULT_VALUE_NUMERIC -1 /end COMPU_TAB
        /begin COMPU_METHOD R "" TAB_VERB "%6.2" "" COMPU_TAB_REF RANGES /end COMPU_METHOD
        /begin COMPU_VTAB_RANGE RANGES "" 2 0 9 "low" 10 255 "high" /end COMPU_VTAB_RANGE
        /begin COMPU_METHOD F "" FORM "%6.2" "degC"
          /begin FORMULA "X1 * 0.5 - 40" FORMULA_INV "(X1 + 40) / 0.5" /end FORMULA
        /end COMPU_METHOD
        /begin MEASUREMENT q "" UBYTE Q 0 0 0 255 ECU_ADDRESS 1 /end MEASUREMENT
        /begin MEASUREMENT t "" UBYTE T 0 0 0 255 ECU_ADDRESS 2 /end MEASUREMENT
        /begin MEASUREMENT n "" UBYTE N 0 0 0 255 ECU_ADDRESS 3 /end MEASUREMENT
        /begin MEASUREMENT r "" UBYTE R 0 0 0 255 ECU_ADDRESS 4 BYTE_ORDER MSB_FIRST ECU_ADDRESS_EXTENSION 2 /end MEASUREMENT
        /begin MEASUREMENT f "" UBYTE F 0 0 -40 100 ECU_ADDRESS 5 /end MEASUREMENT
        """
        )
    )
    q, t, n, r, f = (document.variable(name) for name in 'qtnrf')
    assert q.decode(bytes([16])) == 4.0 and q.encode(5.0) == bytes([25])  # raw = phys², non-linear
    assert t.decode(bytes([15])) == 250.0 and t.encode(250.0) == bytes([15])  # Interpolated both ways
    assert n.decode(bytes([2])) == 7.0 and n.decode(bytes([9])) == -1.0
    assert r.decode(bytes([12])) == 'high' and r.encode('low') == bytes([0])
    assert r.byte_order == 'big' and r.extension == 2
    assert f.decode(bytes([100])) == 10.0 and f.encode(10.0) == bytes([100]) and f.unit == 'degC'
    assert evaluate_formula('sqrt(X) + (X > 3)', 4.0) == 3.0
    assert evaluate_formula('(X > 3) && !(X == 1) || X1 < 0', 4.0) == 1.0
    with pytest.raises(Exception, match='unknown name'):
        evaluate_formula('__import__', 1.0)


def test_include_and_layout_alignment(tmp_path):
    from can_explorer.core.a2l import load_a2l

    (tmp_path / 'part.a2l').write_text(
        '/begin MEASUREMENT Inc "" UWORD NO_COMPU_METHOD 0 0 0 1 ECU_ADDRESS 0x40 /end MEASUREMENT'
    )
    (tmp_path / 'main.a2l').write_text(_module('/include "part.a2l"'))
    assert load_a2l(tmp_path / 'main.a2l').variable('Inc').address == 0x40
