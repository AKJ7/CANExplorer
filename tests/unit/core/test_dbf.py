import random

import cantools
import pytest

from can_explorer.core.database_io import (
    DatabaseFormatError,
    convert_database,
    load_database,
    save_database,
)
from can_explorer.core.dbf import (
    DbfError,
    dbf_position_to_start,
    dump_dbf,
    load_dbf,
    parse_dbf,
    start_to_dbf_position,
)
from can_explorer.db_convert import main as convert_main

DBF_HEADER = '[DATABASE_VERSION] 1.3\n[PROTOCOL] CAN\n[NUMBER_OF_MESSAGES] 1\n'


def busmaster_dbc_to_dbf(start: int, length: int, big_endian: bool, dlc: int = 8) -> tuple[int, int]:
    """Literal port of BUSMASTER's DBC -> DBF bit position conversion (DBFSignal.cpp), used as reference."""
    start_x = start
    if big_endian:
        temp = 0 if start == 0 else start % 4
        start = start + 7 - temp * 2 if start % 8 <= 3 else start - 1 - temp * 2
        start = start + length - 1
        start_x = dlc * 8 - 1 - start
    return start // 8 + 1, start_x % 8


def fits(start: int, length: int, big_endian: bool, size: int = 64) -> bool:
    if not big_endian:
        return start + length <= size
    msb = (start // 8) * 8 + (7 - start % 8)
    return msb + length <= size


@pytest.fixture
def complex_db(data_dir):
    return cantools.database.load_file(str(data_dir / 'complex.dbc'), strict=False)


# Bit positions


@pytest.mark.parametrize('big_endian', [False, True])
def test_positions_match_busmaster_and_round_trip(big_endian):
    checked = 0
    for length in range(1, 65):
        for start in range(64):
            if not fits(start, length, big_endian):
                continue
            position = start_to_dbf_position(start, length, big_endian)
            assert position == busmaster_dbc_to_dbf(start, length, big_endian), (start, length)
            assert dbf_position_to_start(*position, length, big_endian) == start
            checked += 1
    assert checked > 1000


def test_busmaster_reference_lines():
    # Intel example from busmaster issue #570 (expected DBF line confirmed by the maintainers)
    db = cantools.database.load_string(
        'BO_ 1 Message: 6 Vector__XXX\n SG_ signal1 : 27|12@1+ (0.045332,0) [0|185.63454] "-" Knoten\n',
        database_format='dbc',
        strict=False,
    )
    text, _ = dump_dbf(db)
    assert '[START_SIGNALS] signal1,12,4,3,U,4095,0,1,0,0.045332,-,,Knoten' in text.splitlines()
    assert '[START_MSG] Message,1,6,1,1,S,Vector__XXX' in text.splitlines()
    # Motorola example from busmaster issue #756: 16 bits on bytes 4 and 5
    database, warnings = parse_dbf(
        DBF_HEADER + '[START_MSG] msg01,258,8,1,0,S\n[START_SIGNALS] Sig04,16,5,0,U,65535,0,0,0.000000,1.000000,,\n'
        '[END_MSG]\n'
    )
    signal = database.messages[0].signals[0]
    assert (signal.start, signal.byte_order, signal.minimum, signal.maximum) == (31, 'big_endian', 0, 65535)
    assert database.messages[0].encode({'Sig04': 0x1234}) == bytes([0, 0, 0, 0x12, 0x34, 0, 0, 0])
    assert warnings == []


# Round trips


def test_round_trip_preserves_definitions(complex_db):
    text, warnings = dump_dbf(complex_db)
    assert warnings == []
    result, parse_warnings = parse_dbf(text)
    assert parse_warnings == []
    assert [node.name for node in result.nodes] == ['Engine', 'Gateway', 'Dashboard']
    assert result.get_node_by_name('Engine').comment == 'Engine control unit'
    for original in complex_db.messages:
        converted = result.get_message_by_name(original.name)
        assert (converted.frame_id, converted.is_extended_frame, converted.length, converted.senders) == (
            original.frame_id,
            original.is_extended_frame,
            original.length,
            original.senders,
        )
        assert converted.comment == original.comment
        for signal in original.signals:
            other = converted.get_signal_by_name(signal.name)
            for attribute in (
                'start',
                'length',
                'byte_order',
                'is_signed',
                'is_float',
                'unit',
                'receivers',
                'comment',
                'is_multiplexer',
                'multiplexer_ids',
                'multiplexer_signal',
            ):
                assert getattr(other, attribute) == getattr(signal, attribute), (signal.name, attribute)
            assert other.scale == pytest.approx(signal.scale) and other.offset == pytest.approx(signal.offset)
            assert other.minimum == pytest.approx(signal.minimum, abs=signal.scale)
            assert other.maximum == pytest.approx(signal.maximum, abs=signal.scale)
            assert dict(other.choices or {}) == {key: str(value) for key, value in (signal.choices or {}).items()}


def test_round_trip_decodes_identically(complex_db):
    result, _ = parse_dbf(dump_dbf(complex_db)[0])
    rng = random.Random(42)
    for original in complex_db.messages:
        converted = result.get_message_by_name(original.name)
        for _ in range(50):
            data = bytes(rng.randrange(256) for _ in range(original.length))
            try:
                expected = original.decode(data, decode_choices=False, scaling=False)
            except cantools.database.DecodeError:  # Undefined multiplexer value
                with pytest.raises(cantools.database.DecodeError):
                    converted.decode(data, decode_choices=False, scaling=False)
                continue
            assert converted.decode(data, decode_choices=False, scaling=False) == expected


def test_dbc_dbf_dbc_text_round_trip(complex_db, tmp_path):
    report = convert_database(_write(complex_db, tmp_path), tmp_path / 'a.dbf')
    assert (report.messages, report.signals, report.warnings) == (5, 14, [])
    second = convert_database(tmp_path / 'a.dbf', tmp_path / 'b.dbc')
    reloaded = cantools.database.load_file(str(tmp_path / 'b.dbc'), strict=False)
    assert second.warnings == [] and len(reloaded.messages) == 5
    assert reloaded.get_message_by_name('FloatData').get_signal_by_name('Pressure').is_float
    raw = (tmp_path / 'a.dbf').read_bytes()
    assert b'\r\n' in raw and raw.startswith(b'//****')


def _write(database, tmp_path):
    path = tmp_path / 'source.dbc'
    path.write_text(database.as_dbc_string())
    return path


@pytest.mark.parametrize('suffix', ['.kcd', '.sym', '.dbc', '.dbf'])
def test_convert_to_every_format(complex_db, tmp_path, suffix):
    source = tmp_path / 'source.dbf'
    save_database(complex_db, source)
    report = convert_database(source, tmp_path / f'out{suffix}')
    database, _ = load_database(tmp_path / f'out{suffix}')
    assert report.messages == len(database.messages) == 5


# Reading details and errors


def test_parses_busmaster_file_with_crlf_comments_and_skipped_sections(tmp_path):
    content = (
        '//******************************BUSMASTER Messages and signals Database ******************************//\r\n'
        '\r\n[DATABASE_VERSION] 1.3\r\n\r\n[PROTOCOL] CAN\r\n\r\n[BUSMASTER_VERSION] [3.2.2]\r\n'
        '[NUMBER_OF_MESSAGES] 2\r\n\r\n'
        '[START_MSG] Temp,291,8,1,1,S,ECU\r\n'
        '[START_SIGNALS] Celsius,8,1,0,I,127,-128,1,0.000000,1.000000,°C,,Display,Logger\r\n'
        '[VALUE_DESCRIPTION] "Sensor, open",-128\r\n'
        '[END_MSG]\r\n\r\n'
        '[START_VALUE_TABLE]\r\n[START_TABLE] Onoff\r\n[END_TABLE]\r\n[END_VALUE_TABLE]\r\n\r\n'
        '[NODE] ECU,Display,Logger\r\n\r\n'
        '[START_DESC]\r\n[START_DESC_NET]\r\n"Network";\r\n[END_DESC_NET]\r\n'
        '[START_DESC_NODE]\r\nECU "Main\r\nunit";\r\n[END_DESC_NODE]\r\n'
        '[START_DESC_MSG]\r\n291 S "Temperature";\r\n[END_DESC_MSG]\r\n'
        '[START_DESC_SIG]\r\n291 S Celsius "Ambient \\"air\\"";\r\n[END_DESC_SIG]\r\n[END_DESC]\r\n'
        '[START_PARAM]\r\n[START_PARAM_NET]\r\n"BusType",STRING,"CAN"\r\n[END_PARAM_NET]\r\n[END_PARAM]\r\n'
    )
    path = tmp_path / 'busmaster.dbf'
    path.write_bytes(content.encode('cp1252'))
    database, warnings = load_dbf(path)
    message = database.messages[0]
    signal = message.signals[0]
    assert (message.name, message.frame_id, message.senders, message.comment) == ('Temp', 291, ['ECU'], 'Temperature')
    assert (signal.is_signed, signal.minimum, signal.maximum, signal.unit) == (True, -128, 127, '°C')
    assert signal.receivers == ['Display', 'Logger'] and signal.comment == 'Ambient "air"'
    assert signal.choices == {-128: 'Sensor, open'}
    assert database.get_node_by_name('ECU').comment == 'Main\nunit'
    assert any('[NUMBER_OF_MESSAGES]' in warning for warning in warnings)
    assert any('global value tables' in warning for warning in warnings)
    assert any('attribute definitions' in warning for warning in warnings)
    assert any('Network comments' in warning for warning in warnings)


def test_j1939_protocol(complex_db):
    text, _ = dump_dbf(complex_db, protocol='J1939')
    assert '[START_MSG] VehicleSpeed,65265,8,2,1,X,Gateway' in text
    database, _ = parse_dbf(text)
    assert database.get_message_by_name('VehicleSpeed').frame_id == 0x18FEF1FE


@pytest.mark.parametrize(
    'body, message',
    [
        ('[START_MSG] M,1,8\n[END_MSG]\n', 'at least 6 fields'),
        ('[START_MSG] M,x,8,0,1,S\n[END_MSG]\n', 'Invalid id'),
        ('[START_MSG] M,1,8,0,1,Q\n[END_MSG]\n', 'S or X'),
        ('[START_MSG] M,1,8,1,1,S\n[START_SIGNALS] A,8,1,0,Z,255,0,1,0,1,,\n[END_MSG]\n', 'Unknown signal type'),
        ('[START_MSG] M,1,8,1,1,S\n[START_SIGNALS] A,16,1,0,U,255,0,0,0,1,,\n[END_MSG]\n', 'does not fit'),
        ('[START_MSG] M,1,8,1,1,S\n[START_SIGNALS] A,8,1,0,U,255,0,1,0,1,,\n', r'no \[END_MSG\]'),
        ('[START_MSG] M,1,8,1,1,S\n[VALUE_DESCRIPTION] "x",1\n[END_MSG]\n', 'before any signal'),
        ('[PROTOCOL] LIN\n', 'Unsupported protocol'),
    ],
)
def test_errors_report_line_numbers(body, message):
    with pytest.raises(DbfError, match=message) as error:
        parse_dbf('[DATABASE_VERSION] 1.3\n' + body)
    assert error.value.line is not None


def test_unsupported_formats(tmp_path):
    with pytest.raises(DatabaseFormatError):
        load_database(tmp_path / 'file.txt')
    with pytest.raises(DatabaseFormatError):
        convert_database(tmp_path / 'a.dbc', tmp_path / 'b.arxml')


def test_command_line(complex_db, tmp_path, capsys):
    source = _write(complex_db, tmp_path)
    assert convert_main([str(source), str(tmp_path / 'out.dbf')]) == 0
    assert 'Converted source.dbc to out.dbf: 5 messages, 14 signals' in capsys.readouterr().out
    assert convert_main([str(tmp_path / 'missing.dbc'), str(tmp_path / 'out.dbf')]) == 1
    assert convert_main([str(source), str(tmp_path / 'out.txt')]) == 1
    assert 'error:' in capsys.readouterr().err
