import can
import pytest

from can_explorer.core.database_io import DatabaseFormatError, convert_database, load_database
from can_explorer.core.decoding import DbcDecoder, FrameDecoder, open_signal_database
from can_explorer.core.diagnostic_description import DiagnosticDescription
from can_explorer.core.frames import Frame
from can_explorer.core.lin import LinDecoder
from can_explorer.core.signals import SignalTracker
from can_explorer.core.uds import UdsDecoder, describe_request, describe_response

ENGINE_DATA = bytes.fromhex('a00f02001f4083ff')  # EngineSpeed 1000, Second, OilPressure 150, Torque -12.5


def message(can_id, data, extended=False, fd=False):
    return can.Message(arbitration_id=can_id, is_extended_id=extended, is_fd=fd, data=data)


# ARXML


@pytest.fixture
def arxml(data_dir):
    return DbcDecoder(data_dir / 'example.arxml')


def test_arxml_buses_and_identifier_clash(arxml):
    assert arxml.buses == ['Powertrain', 'Chassis']
    assert arxml.bus is None and arxml.conflicting_identifiers
    # Without a bus, the first definition of 0x100 is used
    assert arxml.message_definition(message(0x100, ENGINE_DATA)).name == 'EngineData'
    arxml.set_bus('Chassis')
    assert (
        arxml.describe(message(0x100, bytes([0x10, 0x27, 0, 0, 0, 0, 0, 0])))
        == 'WheelSpeeds: WheelSpeedFrontLeft=100 km/h'
    )
    assert arxml.message_definition(message(0x200, bytes(64), fd=True)) is None  # Not on the Chassis bus
    arxml.set_bus('Powertrain')
    assert not arxml.conflicting_identifiers
    with pytest.raises(ValueError, match='Unknown bus'):
        arxml.set_bus('Body')


def test_arxml_signals_and_byte_orders(arxml):
    arxml.set_bus('Powertrain')
    described = arxml.describe(message(0x100, ENGINE_DATA))
    assert described == 'EngineData: EngineSpeed=1000 rpm, GearPosition=Second, OilPressure=150 kPa'
    keys = [signal.key for signal in arxml.signals()]
    assert 'EngineData.OilPressure' in keys and 'StatusPdu.AliveCounter' in keys and 'EnginePdu.EngineSpeed' in keys
    assert 'WheelSpeeds.WheelSpeedFrontLeft' not in keys


def test_arxml_container_frames(arxml):
    arxml.set_bus('Powertrain')
    bundle = arxml.database.get_message_by_name('Bundle')
    payload = bundle.encode(
        [
            (
                bundle.get_contained_message_by_name('EnginePdu'),
                {'EngineSpeed': 500, 'GearPosition': 1, 'OilPressure': 0},
            ),
            (bundle.get_contained_message_by_name('StatusPdu'), {'AliveCounter': 7}),
        ]
    )
    msg = message(0x200, payload.ljust(64, b'\x00'), fd=True)
    assert arxml.describe(msg) == (
        'Bundle [EnginePdu: EngineSpeed=500 rpm, GearPosition=First, OilPressure=0 kPa; StatusPdu: AliveCounter=7]'
    )
    assert arxml.decode(msg, decode_choices=False)['AliveCounter'] == 7
    tracker = SignalTracker()
    tracker.track('StatusPdu', 'AliveCounter')
    tracker.track('EnginePdu', 'EngineSpeed', 'rpm')
    msg.timestamp = 1.0
    tracker.update(msg, arxml)
    assert tracker.series['StatusPdu.AliveCounter'].values == [7.0]
    assert tracker.series['EnginePdu.EngineSpeed'].values == [500.0]


# KCD and SYM


def test_kcd(data_dir):
    kcd = open_signal_database(data_dir / 'example.kcd')
    assert kcd.buses == ['Powertrain', 'Chassis']
    kcd.set_bus('Powertrain')
    assert kcd.describe(message(0x100, ENGINE_DATA)) == (
        'EngineData: EngineSpeed=1000 rpm, GearPosition=Second, OilPressure=150 kPa, Torque=-12.5 Nm'
    )
    assert (
        kcd.describe(message(0x200, bytes([0, 0xB8, 0x30, 0, 0, 0, 0, 0])))
        == 'Diagnosis: Page=0, BatteryVoltage=12.472 V'
    )
    assert kcd.describe(message(0x200, bytes([1, 0x10, 0, 0, 0, 0, 0, 0]))) == 'Diagnosis: Page=1, OperatingHours=16 h'
    kcd.set_bus('Chassis')
    assert kcd.describe(message(0x100, bytes([0x10, 0x27]) + bytes(6))) == 'WheelSpeeds: WheelSpeedFrontLeft=100 km/h'


def test_sym(data_dir):
    sym = open_signal_database(data_dir / 'example.sym')
    assert sym.buses == []
    engine = sym.database.get_message_by_name('EngineData')
    data = engine.encode({'EngineSpeed': 1000, 'GearPosition': 'Second', 'OilPressure': 150, 'Torque': -12.5})
    assert sym.describe(message(0x100, data)) == (
        'EngineData: EngineSpeed=1000 rpm, GearPosition=Second, OilPressure=150 kPa, Torque=-12.5 Nm'
    )
    assert sym.describe(message(0x200, bytes([1, 0x10, 0, 0, 0, 0, 0, 0]))) == 'Diagnosis: Page0=1, OperatingHours=16 h'
    speed = message(0x18FEF1FE, bytes([0, 0, 0x30, 0, 0, 0, 0, 0]), extended=True)
    assert sym.describe(speed) == 'VehicleSpeed: WheelSpeed=48 km/h'
    assert sym.describe(message(0x18FEF1FE & 0x7FF, bytes(8))) is None  # Standard frame with the same number


# Conversion


@pytest.mark.parametrize('suffix', ['.dbc', '.dbf', '.kcd', '.sym'])
def test_multi_bus_conversion_warns_or_selects_a_bus(data_dir, tmp_path, suffix):
    report = convert_database(data_dir / 'example.arxml', tmp_path / f'all{suffix}')
    assert report.signals == 8  # Including the signals of the contained PDUs
    assert any('2 buses (Powertrain, Chassis) merged into one; identifiers 0x100 clash' in w for w in report.warnings)
    assert any("Container message 'Bundle'" in w for w in report.warnings)
    report = convert_database(data_dir / 'example.kcd', tmp_path / f'powertrain{suffix}', bus='Powertrain')
    database, _ = load_database(tmp_path / f'powertrain{suffix}')
    assert sorted(m.name for m in database.messages) == ['Diagnosis', 'EngineData']
    assert not any('buses' in warning or 'Container' in warning for warning in report.warnings)
    with pytest.raises(DatabaseFormatError, match='Unknown bus'):
        convert_database(data_dir / 'example.kcd', tmp_path / f'x{suffix}', bus='Body')


# CDD diagnostic descriptions


@pytest.fixture
def cdd(data_dir):
    return DiagnosticDescription(data_dir / 'example.cdd')


def test_cdd_identifiers_and_decoding(cdd):
    assert cdd.identifiers() == {0x0123: 'OperatingHours', 0xF100: 'EngineStatus', 0xF190: 'VIN'}
    assert cdd.describe(0xF100, bytes([0x0F, 0xA0, 90, 2])) == (
        'EngineStatus: EngineSpeed=1000 rpm, CoolantTemperature=50 degC, DrivingMode=Sport'
    )
    assert cdd.describe(0xF190, b'WDB1234567890ABCD') == 'VIN: VehicleIdentificationNumber=WDB1234567890ABCD'
    assert cdd.fields(0xF100)[0] == {'name': 'EngineSpeed', 'unit': 'rpm'}
    assert cdd.describe(0x9999, b'\x00') is None
    assert cdd.describe(0xF100, b'\x00') is None  # Too short


def test_cdd_rejects_other_files(data_dir):
    with pytest.raises(ValueError, match='use .cdd'):
        DiagnosticDescription(data_dir / 'example.dbc')


def test_uds_and_lin_decoders_use_the_description(cdd):
    response = bytes([0x62, 0xF1, 0x00, 0x0F, 0xA0, 90, 2])
    assert describe_response(response, cdd).endswith(
        'EngineStatus: EngineSpeed=1000 rpm, CoolantTemperature=50 degC, DrivingMode=Sport'
    )
    assert describe_request(bytes([0x22, 0xF1, 0x00]), cdd) == 'ReadDataByIdentifier F1 00 (EngineStatus)'
    uds = UdsDecoder()
    uds.description = cdd
    frame = message(0x7E8, bytes([0x07, *response]))
    assert uds.describe(frame).endswith(
        'UDS Positive response to ReadDataByIdentifier: EngineStatus: EngineSpeed=1000 rpm, CoolantTemperature=50 degC, DrivingMode=Sport'
    )
    lin = LinDecoder()
    lin.description = cdd
    request = lin.describe(message(0x3C, bytes([0x01, 0x03, 0x22, 0xF1, 0x00, 0xFF, 0xFF, 0xFF])))
    assert request.endswith('ReadDataByIdentifier F1 00 (EngineStatus)')


def test_frame_decoder_with_arxml(arxml):
    arxml.set_bus('Powertrain')
    decoder = FrameDecoder(None, arxml)
    frame = Frame(message(0x100, ENGINE_DATA))
    assert decoder.full_description(frame).startswith('EngineData: EngineSpeed=1000 rpm')


def test_cdd_encodings(tmp_path):
    from can_explorer.core.diagnostic_description import DataType, decode_field

    signed = DataType('s16', 16, 'sgn', '21', factor=0.5)
    assert decode_field(bytes([0xFF, 0xFE]), 0, signed) == -1.0
    little = DataType('u16le', 16, 'uns', '12')
    assert decode_field(bytes([0x34, 0x12]), 0, little) == 0x1234
    nibble = DataType('u4', 4, 'uns', '21')
    assert decode_field(bytes([0xAB]), 4, nibble) == 0xA  # CDD offsets count from the least significant bit
    real = DataType('f32', 32, 'flt', '21')
    assert decode_field(bytes.fromhex('3fc00000'), 0, real) == 1.5
    # Shared DID structures (DIDDATAREF) and nested structures
    path = tmp_path / 'structures.cdd'
    path.write_text(
        '<CANDELA><ECUDOC><DATATYPES>'
        '<IDENT id="u8"><NAME><TUV>u8</TUV></NAME><CVALUETYPE bl="8" bo="21" enc="uns"/></IDENT>'
        '<IDENT id="u4"><NAME><TUV>u4</TUV></NAME><CVALUETYPE bl="4" bo="21" enc="uns"/></IDENT>'
        '<STRUCTDT id="struct"><NAME><TUV>s</TUV></NAME></STRUCTDT>'
        '</DATATYPES>'
        '<DIDS><DID id="lib"><STRUCTURE><DATAOBJ dtref="u8"><QUAL>A</QUAL></DATAOBJ>'
        '<DATAOBJ dtref="u8"><QUAL>B</QUAL></DATAOBJ></STRUCTURE></DID></DIDS>'
        '<ECU><VAR><DIAGCLASS>'
        '<DIAGINST><QUAL>Shared</QUAL><STATICVALUE v="4096"/><SIMPLECOMPCONT><DIDDATAREF didRef="lib"/></SIMPLECOMPCONT></DIAGINST>'
        '<DIAGINST><QUAL>Nested</QUAL><STATICVALUE v="4097"/><SIMPLECOMPCONT><STRUCT>'
        '<DATAOBJ dtref="u8"><QUAL>C</QUAL></DATAOBJ></STRUCT></SIMPLECOMPCONT></DIAGINST>'
        '<DIAGINST><QUAL>Union</QUAL><STATICVALUE v="4099"/><SIMPLECOMPCONT><UNION>'
        '<DATAOBJ dtref="u8"><QUAL>Whole</QUAL></DATAOBJ><STRUCT><DATAOBJ dtref="u4"><QUAL>Low</QUAL></DATAOBJ>'
        '<DATAOBJ dtref="u4"><QUAL>High</QUAL></DATAOBJ></STRUCT></UNION></SIMPLECOMPCONT></DIAGINST>'
        '<DIAGINST><QUAL>Structured</QUAL><STATICVALUE v="4098"/><SIMPLECOMPCONT>'
        '<DATAOBJ dtref="struct"><QUAL>D</QUAL></DATAOBJ></SIMPLECOMPCONT></DIAGINST>'
        '</DIAGCLASS></VAR></ECU></ECUDOC></CANDELA>'
    )
    description = DiagnosticDescription(path)
    assert description.describe(0x1000, b'\x01\x02') == 'Shared: A=1, B=2'
    assert description.describe(0x1001, b'\x07') == 'Nested: C=7'
    assert description.describe(0x1002, b'\x0a\x0b') == 'Structured: 0A 0B'  # Undecodable type: hex
    # Union alternatives overlay the same bits: the whole byte and its two nibbles
    assert description.describe(0x1003, b'\xab') == 'Union: Whole=171, Low=11, High=10'
    broken = tmp_path / 'broken.cdd'
    broken.write_text('<CANDELA><nothing/></CANDELA>')
    with pytest.raises(ValueError, match='no ECUDOC'):
        DiagnosticDescription(broken)
