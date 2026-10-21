import zipfile

import can
import pytest

from can_explorer.core.diagnostic_description import DiagnosticDescription
from can_explorer.core.lin import LinDecoder
from can_explorer.core.odx import load_odx
from can_explorer.core.uds import UdsDecoder, describe_request, describe_response


def message(can_id, data):
    return can.Message(arbitration_id=can_id, is_extended_id=False, data=bytes(data))


@pytest.fixture(scope='module')
def odx(data_dir):
    return DiagnosticDescription(data_dir / 'example.odx')


@pytest.fixture
def pdx(data_dir, tmp_path):
    """A packaged ODX: the example ODX document and an index catalog in a ZIP."""
    path = tmp_path / 'example.pdx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('index.xml', '<CATALOG/>')
        archive.writestr('ExampleEcu.odx-d', (data_dir / 'example.odx').read_bytes())
    return path


def test_odx_identifiers_and_decoding(odx):
    assert odx.identifiers() == {
        0x1001: 'EngineSpeed',
        0x1004: 'Calibration',
        0xF100: 'EngineStatus',
        0xF190: 'VIN',
    }
    # Big endian linear value (0.25 rpm)
    assert odx.describe(0x1001, bytes([0x0F, 0xA0])) == 'EngineSpeed: EngineSpeed=1000 rpm'
    # An offset value, a text table and a little endian value in one response
    assert odx.describe(0xF100, bytes([90, 2, 0xE8, 0x03])) == (
        'EngineStatus: CoolantTemperature=50 degC, DrivingMode=Sport, BatteryVoltage=1 V'
    )
    assert odx.describe(0xF190, b'WDB1234567890ABCD') == 'VIN: VehicleIdentificationNumber=WDB1234567890ABCD'
    assert odx.fields(0xF100) == [
        {'name': 'CoolantTemperature', 'unit': 'degC'},
        {'name': 'DrivingMode', 'unit': ''},
        {'name': 'BatteryVoltage', 'unit': 'V'},
    ]
    assert odx.name(0x1001) == 'EngineSpeed' and odx.name(0x4321) is None
    assert odx.describe(0x9999, b'\x00') is None  # Unknown identifier
    assert odx.describe(0x1001, b'\x00') is None  # Too short


def test_odx_structured_data_is_shown_as_hex(odx):
    """A DID whose data uses a structure (not a simple data object property) is named but shown in hex."""
    assert odx.decode(0x1004, bytes([1, 2, 3, 4])) is None
    assert odx.describe(0x1004, bytes([1, 2, 3, 4])) == 'Calibration: 01 02 03 04'


def test_pdx_archive(pdx):
    description = DiagnosticDescription(pdx)
    assert set(description.identifiers()) == {0x1001, 0x1004, 0xF100, 0xF190}
    assert description.describe(0x1001, bytes([0x10, 0x00])) == 'EngineSpeed: EngineSpeed=1024 rpm'


def test_uds_and_lin_decoders_use_the_odx(odx):
    response = bytes([0x62, 0x10, 0x01, 0x0F, 0xA0])
    assert describe_response(response, odx).endswith('EngineSpeed: EngineSpeed=1000 rpm')
    assert describe_request(bytes([0x22, 0x10, 0x01]), odx) == 'ReadDataByIdentifier 10 01 (EngineSpeed)'
    uds = UdsDecoder()
    uds.description = odx
    frame = message(0x7E8, bytes([0x05, *response]))
    assert uds.describe(frame).endswith('ReadDataByIdentifier: EngineSpeed: EngineSpeed=1000 rpm')
    lin = LinDecoder()
    lin.description = odx
    request = lin.describe(message(0x3C, bytes([0x01, 0x03, 0x22, 0x10, 0x01, 0xFF, 0xFF, 0xFF])))
    assert request.endswith('ReadDataByIdentifier 10 01 (EngineSpeed)')


def test_odx_rejects_and_reports_bad_files(data_dir, tmp_path):
    # Valid XML that is not ODX (an ARXML database), and malformed XML
    with pytest.raises(ValueError, match='no ODX root element'):
        load_odx(data_dir / 'example.arxml')
    broken = tmp_path / 'broken.odx'
    broken.write_text('<ODX><DIAG-LAYER-CONTAINER')
    with pytest.raises(ValueError, match='Invalid XML'):
        DiagnosticDescription(broken)
    not_zip = tmp_path / 'fake.pdx'
    not_zip.write_bytes(b'not a zip file')
    with pytest.raises(ValueError, match='Not a PDX archive'):
        DiagnosticDescription(not_zip)
    empty = tmp_path / 'empty.pdx'
    with zipfile.ZipFile(empty, 'w') as archive:
        archive.writestr('readme.txt', 'no odx here')
    with pytest.raises(ValueError, match='no ODX documents'):
        DiagnosticDescription(empty)


def test_odx_scalings(tmp_path):
    """IS-HIGHLOW-BYTE-ORDER, signed base type, float base type and an unsupported variable length type."""
    path = tmp_path / 'scalings.odx'
    path.write_text(_odx_document(DOPS))
    description = DiagnosticDescription(path)
    assert description.describe(0x2001, bytes([0xFF, 0xFE])) == 'Signed: Value=-2'  # A_INT32, big endian (0xFFFE)
    assert description.describe(0x2002, bytes.fromhex('3fc00000')) == 'Float: Value=1.5'  # A_FLOAT32
    # A leading-length-info (variable length) type cannot be decoded: shown as hex
    assert description.describe(0x2003, bytes([2, 0x41, 0x42])) == 'Variable: 02 41 42'


def _odx_document(body: str) -> str:
    return (
        '<ODX xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><DIAG-LAYER-CONTAINER ID="dlc">'
        '<SHORT-NAME>C</SHORT-NAME><ECU-VARIANTS><ECU-VARIANT ID="ecu"><SHORT-NAME>E</SHORT-NAME>'
        f'{body}</ECU-VARIANT></ECU-VARIANTS></DIAG-LAYER-CONTAINER></ODX>'
    )


def _service(name, identifier, dop_ref):
    """A ReadDataByIdentifier service, its request and response, referencing a data object property."""
    return (
        f'<DIAG-SERVICE ID="svc_{name}"><SHORT-NAME>{name}</SHORT-NAME><LONG-NAME>{name}</LONG-NAME>'
        f'<REQUEST-REF ID-REF="req_{name}"/><POS-RESPONSE-REFS><POS-RESPONSE-REF ID-REF="resp_{name}"/>'
        '</POS-RESPONSE-REFS></DIAG-SERVICE>',
        f'<REQUEST ID="req_{name}"><SHORT-NAME>q</SHORT-NAME><PARAMS>'
        '<PARAM xsi:type="CODED-CONST"><SHORT-NAME>S</SHORT-NAME><BYTE-POSITION>0</BYTE-POSITION>'
        '<CODED-VALUE>34</CODED-VALUE><DIAG-CODED-TYPE xsi:type="STANDARD-LENGTH-TYPE" BASE-DATA-TYPE="A_UINT32">'
        '<BIT-LENGTH>8</BIT-LENGTH></DIAG-CODED-TYPE></PARAM>'
        f'<PARAM xsi:type="CODED-CONST"><SHORT-NAME>D</SHORT-NAME><BYTE-POSITION>1</BYTE-POSITION>'
        f'<CODED-VALUE>{identifier}</CODED-VALUE><DIAG-CODED-TYPE xsi:type="STANDARD-LENGTH-TYPE" '
        'BASE-DATA-TYPE="A_UINT32"><BIT-LENGTH>16</BIT-LENGTH></DIAG-CODED-TYPE></PARAM></PARAMS></REQUEST>',
        f'<POS-RESPONSE ID="resp_{name}"><SHORT-NAME>r</SHORT-NAME><PARAMS>'
        '<PARAM xsi:type="CODED-CONST"><SHORT-NAME>S</SHORT-NAME><BYTE-POSITION>0</BYTE-POSITION>'
        '<CODED-VALUE>98</CODED-VALUE><DIAG-CODED-TYPE xsi:type="STANDARD-LENGTH-TYPE" BASE-DATA-TYPE="A_UINT32">'
        '<BIT-LENGTH>8</BIT-LENGTH></DIAG-CODED-TYPE></PARAM>'
        f'<PARAM xsi:type="CODED-CONST"><SHORT-NAME>D</SHORT-NAME><BYTE-POSITION>1</BYTE-POSITION>'
        f'<CODED-VALUE>{identifier}</CODED-VALUE><DIAG-CODED-TYPE xsi:type="STANDARD-LENGTH-TYPE" '
        'BASE-DATA-TYPE="A_UINT32"><BIT-LENGTH>16</BIT-LENGTH></DIAG-CODED-TYPE></PARAM>'
        '<PARAM xsi:type="VALUE"><SHORT-NAME>Value</SHORT-NAME><BYTE-POSITION>3</BYTE-POSITION>'
        f'<DOP-REF ID-REF="{dop_ref}"/></PARAM></PARAMS></POS-RESPONSE>',
    )


def _dop(dop_id, coded_type, compu='<COMPU-METHOD><CATEGORY>IDENTICAL</CATEGORY></COMPU-METHOD>'):
    return f'<DATA-OBJECT-PROP ID="{dop_id}"><SHORT-NAME>V</SHORT-NAME>{coded_type}{compu}</DATA-OBJECT-PROP>'


_SIGNED = '<DIAG-CODED-TYPE xsi:type="STANDARD-LENGTH-TYPE" BASE-DATA-TYPE="A_INT32"><BIT-LENGTH>16</BIT-LENGTH></DIAG-CODED-TYPE>'
_FLOAT = '<DIAG-CODED-TYPE xsi:type="STANDARD-LENGTH-TYPE" BASE-DATA-TYPE="A_FLOAT32"><BIT-LENGTH>32</BIT-LENGTH></DIAG-CODED-TYPE>'
_VARIABLE = '<DIAG-CODED-TYPE xsi:type="LEADING-LENGTH-INFO-TYPE" BASE-DATA-TYPE="A_ASCIISTRING"><BIT-LENGTH>8</BIT-LENGTH></DIAG-CODED-TYPE>'

_services = [
    _service('Signed', 0x2001, 'dop_signed'),
    _service('Float', 0x2002, 'dop_float'),
    _service('Variable', 0x2003, 'dop_variable'),
]
DOPS = (
    '<DIAG-COMMS>' + ''.join(service for service, _, _ in _services) + '</DIAG-COMMS>'
    '<REQUESTS>' + ''.join(request for _, request, _ in _services) + '</REQUESTS>'
    '<POS-RESPONSES>' + ''.join(response for _, _, response in _services) + '</POS-RESPONSES>'
    '<DIAG-DATA-DICTIONARY-SPEC><DATA-OBJECT-PROPS>'
    + _dop('dop_signed', _SIGNED)
    + _dop('dop_float', _FLOAT)
    + _dop('dop_variable', _VARIABLE)
    + '</DATA-OBJECT-PROPS></DIAG-DATA-DICTIONARY-SPEC>'
)
