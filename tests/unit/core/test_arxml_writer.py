"""ARXML export: what is written, read back with cantools (the reader CANExplorer uses), and what is reported.

Set CANEXPLORER_AUTOSAR_XSD to the official AUTOSAR_00049.xsd (free from autosar.org, R20-11 AUTOSAR_MMOD_XMLSchema)
to also validate every exported document against the schema.
"""

import logging
import math
import os
import random
from pathlib import Path

import cantools
import pytest

from can_explorer.core.arxml_writer import MARKER, as_arxml_string, short_name, written_by_canexplorer
from can_explorer.core.database_editor import DatabaseEditor
from can_explorer.core.database_io import (
    DatabaseFormatError,
    can_write_back,
    convert_database,
    load_database,
    output_suffix,
    save_database,
)
from can_explorer.core.e2e_check import e2e_config_of
from can_explorer.core.secoc_check import secoc_config_of

RICH_DBC = """VERSION ""

NS_ :

BS_:

BU_: Gateway Body Dash

BO_ 256 Engine: 8 Gateway
 SG_ Speed : 0|16@1+ (0.25,0) [0|16383.75] "rpm" Dash,Body
 SG_ Temp : 23|8@0- (1,-40) [-40|215] "degC" Dash
 SG_ Mode : 24|3@1+ (1,0) [0|7] "" Dash

BO_ 512 Muxed: 8 Body
 SG_ Selector M : 0|8@1+ (1,0) [0|255] "" Dash
 SG_ Always : 56|8@1+ (1,0) [0|255] "" Dash
 SG_ ValueA m1 : 8|16@1+ (0.1,0) [0|6553.5] "V" Dash
 SG_ ValueB m2 : 8|16@0- (1,0) [-32768|32767] "" Dash
 SG_ Shared m1 : 24|8@1+ (1,0) [0|255] "" Dash

BO_ 2566844926 Ratio: 64 Dash
 SG_ Gain : 0|32@1- (1,0) [0|0] "" Gateway
 SG_ Wide : 39|64@0- (1,0) [0|0] "" Gateway

BO_ 768 Nested: 8 Gateway
 SG_ Top M : 0|4@1+ (1,0) [0|15] "" Dash
 SG_ Sub m1M : 8|4@1+ (1,0) [0|15] "" Dash
 SG_ Deep m3 : 16|8@1+ (1,0) [0|255] "" Dash
 SG_ Flat m2 : 16|8@1+ (1,0) [0|255] "" Dash

BO_ 769 Odd: 2 Gateway
 SG_ _lead : 0|8@1+ (1,0) [0|255] "%" Dash

CM_ BU_ Gateway "Central gateway";
CM_ BO_ 256 "Engine status";
CM_ SG_ 256 Speed "Crankshaft speed";
BA_DEF_ BO_ "GenMsgCycleTime" INT 0 65535;
BA_DEF_ SG_ "GenSigStartValue" INT 0 65535;
BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN","ExtendedCAN","reserved","J1939PG","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","StandardCAN_FD","ExtendedCAN_FD";
BA_DEF_DEF_ "GenMsgCycleTime" 0;
BA_DEF_DEF_ "GenSigStartValue" 0;
BA_DEF_DEF_ "VFrameFormat" "StandardCAN";
BA_ "GenMsgCycleTime" BO_ 256 10;
BA_ "GenMsgCycleTime" BO_ 512 1001;
BA_ "VFrameFormat" BO_ 2566844926 15;
BA_ "GenSigStartValue" SG_ 256 Mode 2;
BA_ "GenSigStartValue" SG_ 512 Selector 2;
VAL_ 256 Mode 0 "Off" 1 "Idle" 2 "Run" ;
VAL_ 512 Selector 1 "VoltagePage" 2 "RawPage" ;
SIG_VALTYPE_ 2566844926 Gain : 1;
SIG_VALTYPE_ 2566844926 Wide : 2;
SG_MUL_VAL_ 768 Sub Top 1-1;
SG_MUL_VAL_ 768 Deep Sub 3-3;
SG_MUL_VAL_ 768 Flat Top 2-2;
"""


@pytest.fixture(scope='module')
def schema():
    """The AUTOSAR R20-11 schema when CANEXPLORER_AUTOSAR_XSD points to it, else None."""
    path = os.environ.get('CANEXPLORER_AUTOSAR_XSD')
    if not path:
        return None
    from lxml import etree

    return etree.XMLSchema(etree.parse(path))


def export(database, schema=None):
    """(ARXML text, warnings, the database cantools reads back from it)."""
    text, warnings = as_arxml_string(database)
    if schema is not None:
        from lxml import etree

        assert schema.validate(etree.fromstring(text.encode())), schema.error_log
    logging.disable(logging.WARNING)  # cantools logs about identifiers reused on several buses
    try:
        reread = cantools.database.load_string(text, database_format='arxml', strict=False)
    finally:
        logging.disable(logging.NOTSET)
    return text, warnings, reread


def same(first, second) -> bool:
    if isinstance(first, float) and isinstance(second, float) and math.isnan(first) and math.isnan(second):
        return True
    return first == second


def assert_decodes_alike(original, reread, frames=200):
    """Random payloads decode to the same raw values (the multiplexer is named after the message when read back)."""
    rng = random.Random(original.frame_id)
    renamed = {
        signal.name: other.name
        for signal in original.signals
        for other in reread.signals
        if signal.is_multiplexer and other.is_multiplexer and signal.start == other.start
    }
    for _ in range(frames):
        data = bytes(rng.randrange(256) for _ in range(original.length))
        try:
            expected = original.decode(data, decode_choices=False)
        except cantools.database.errors.DecodeError:  # E.g. a multiplexer value without signals
            with pytest.raises(cantools.database.errors.DecodeError):
                reread.decode(data, decode_choices=False)
            continue
        actual = reread.decode(data, decode_choices=False)
        expected = {renamed.get(name, name): value for name, value in expected.items()}
        assert expected.keys() == actual.keys(), data.hex()
        assert all(same(expected[name], actual[name]) for name in expected), (data.hex(), expected, actual)


@pytest.fixture
def rich():
    return cantools.database.load_string(RICH_DBC, database_format='dbc', strict=False)


def test_dbc_round_trip(data_dir, schema):
    database, _ = load_database(data_dir / 'example.dbc')
    text, warnings, reread = export(database, schema)
    assert warnings == [] and text.splitlines()[1] == MARKER
    assert [bus.name for bus in reread.buses] == ['CAN']  # A DBC has no bus: one cluster named CAN
    assert [node.name for node in reread.nodes] == ['ECU']
    for message in database.messages:
        copy = reread.get_message_by_frame_id(message.frame_id)
        assert (copy.name, copy.length, copy.is_extended_frame, copy.senders) == (
            message.name,
            message.length,
            message.is_extended_frame,
            message.senders,
        )
        for signal in message.signals:
            other = copy.get_signal_by_name(signal.name)
            for attribute in ('start', 'length', 'byte_order', 'is_signed', 'scale', 'offset', 'unit', 'choices'):
                assert getattr(other, attribute) == getattr(signal, attribute), (signal.name, attribute)
            assert other.minimum == pytest.approx(signal.minimum) and other.maximum == pytest.approx(signal.maximum)
        assert_decodes_alike(message, copy)


def test_signal_details_and_frames(rich, schema):
    _, _, reread = export(rich, schema)
    engine = reread.get_message_by_name('Engine')
    assert engine.cycle_time == 10 and engine.comment == 'Engine status' and engine.senders == ['Gateway']
    speed = engine.get_signal_by_name('Speed')
    assert (speed.scale, speed.unit, speed.comment) == (0.25, 'rpm', 'Crankshaft speed')
    temp = engine.get_signal_by_name('Temp')
    assert (temp.byte_order, temp.is_signed, temp.offset, temp.minimum, temp.maximum) == (
        'big_endian',
        True,
        -40,
        -40,
        215,
    )
    mode = engine.get_signal_by_name('Mode')
    assert {value: str(name) for value, name in mode.choices.items()} == {0: 'Off', 1: 'Idle', 2: 'Run'}
    assert mode.raw_initial == 2
    # Receivers belong to the whole PDU in ARXML: an ECU receiving one signal receives them all
    assert sorted(mode.receivers) == ['Body', 'Dash']
    assert reread.get_message_by_name('Muxed').cycle_time == 1001  # Read back exactly (see _seconds)

    ratio = reread.get_message_by_name('Ratio')
    assert ratio.is_fd and ratio.is_extended_frame and ratio.length == 64
    assert [signal.is_float for signal in ratio.signals] == [True, True]
    assert sorted(node.name for node in reread.nodes) == ['Body', 'Dash', 'Gateway']
    assert next(node for node in reread.nodes if node.name == 'Gateway').comment == 'Central gateway'
    for message in rich.messages:
        if message.name not in ('Nested', 'Odd'):  # Changed on purpose, see below
            assert_decodes_alike(message, reread.get_message_by_name(message.name))


def test_multiplexed_message(rich, schema):
    text, _, reread = export(rich, schema)
    assert '<MULTIPLEXED-I-PDU>' in text
    muxed = reread.get_message_by_name('Muxed')
    selector = next(signal for signal in muxed.signals if signal.is_multiplexer)
    assert selector.name == 'Muxed_selector1'  # ARXML stores no name for the selector field
    assert (selector.start, selector.length, selector.raw_initial) == (0, 8, 2)
    assert {value: str(name) for value, name in selector.choices.items()} == {1: 'VoltagePage', 2: 'RawPage'}
    by_name = {signal.name: signal for signal in muxed.signals}
    assert by_name['Always'].multiplexer_ids is None  # The static part
    assert by_name['ValueA'].multiplexer_ids == [1] and by_name['ValueB'].multiplexer_ids == [2]
    assert by_name['ValueA'].scale == 0.1 and by_name['ValueB'].byte_order == 'big_endian'
    assert_decodes_alike(rich.get_message_by_name('Muxed'), muxed)


def test_what_arxml_cannot_hold_is_reported(rich, schema):
    _, warnings, reread = export(rich, schema)
    assert "Message 'Nested': nested multiplexing cannot be expressed in ARXML; written without Deep" in warnings
    assert "Signal 'Odd._lead' written as 'N_lead' (ARXML names allow only letters, digits and _)" in warnings
    nested = reread.get_message_by_name('Nested')
    assert sorted(signal.name for signal in nested.signals) == ['Flat', 'Nested_selector1', 'Sub']
    assert [signal.name for signal in reread.get_message_by_name('Odd').signals] == ['N_lead']


def test_names_are_made_unique_identifiers(rich, schema):
    rich.get_message_by_name('Engine').get_signal_by_name('Mode').name = 'Speed'  # Twice in one message
    rich.get_message_by_name('Odd').name = 'Odd frame'
    _, warnings, reread = export(rich, schema)
    assert "Signal 'Engine.Speed' written as 'Speed_2' (already used)" in warnings
    assert "Message 'Odd frame' written as 'Odd_frame' (ARXML names allow only letters, digits and _)" in warnings
    assert sorted(signal.name for signal in reread.get_message_by_name('Engine').signals) == [
        'Speed',
        'Speed_2',
        'Temp',
    ]
    assert short_name('9lives') == 'N9lives' and short_name('km/h') == 'km_h' and len(short_name('x' * 300)) == 128


def test_buses_and_container_frames(data_dir, schema):
    database, _ = load_database(data_dir / 'example.arxml')
    _, warnings, reread = export(database, schema)
    assert warnings == []
    assert [(bus.name, bus.baudrate, bus.fd_baudrate) for bus in reread.buses] == [
        (bus.name, bus.baudrate, bus.fd_baudrate) for bus in database.buses
    ]
    # Identifier 0x100 on both buses stays two messages, each on its own cluster
    assert sorted((m.name, m.bus_name) for m in reread.messages if m.frame_id == 0x100) == [
        ('EngineData', 'Powertrain'),
        ('WheelSpeeds', 'Chassis'),
    ]
    original = database.get_message_by_name('Bundle')
    bundle = reread.get_message_by_name('Bundle')
    assert bundle.is_container and bundle.is_fd and bundle.header_byte_order == original.header_byte_order
    assert [(m.name, m.header_id, m.length) for m in bundle.contained_messages] == [
        (m.name, m.header_id, m.length) for m in original.contained_messages
    ]
    for contained in original.contained_messages:
        assert_decodes_alike(contained, bundle.get_contained_message_by_name(contained.name))
    status = original.get_contained_message_by_name('StatusPdu')
    data = original.encode([('StatusPdu', {signal.name: 1 for signal in status.signals})])
    decoded = [(m.name, values) for m, values in bundle.decode(data, decode_containers=True)]
    assert decoded == [(m.name, values) for m, values in original.decode(data, decode_containers=True)]
    assert decoded == [('StatusPdu', {signal.name: 1 for signal in status.signals})]


@pytest.mark.parametrize('fixture', ['e2e_arxml', 'secoc_arxml', 'secoc_container_arxml'])
def test_e2e_and_secoc_survive(fixture, request, schema):
    """What the E2E and SecOC checks use is written back, so they work on an exported file as on the original."""
    database, _ = load_database(request.getfixturevalue(fixture))
    _, warnings, reread = export(database, schema)
    assert warnings == []

    def configurations(db):
        result = {}
        for message in db.messages:
            for part in message.contained_messages or [message]:
                result[part.name] = (e2e_config_of(part), secoc_config_of(part))
        return result

    expected = configurations(database)
    assert any(e2e or secoc for e2e, secoc in expected.values())
    assert configurations(reread) == expected
    for message in database.messages:
        if not message.is_container:
            assert_decodes_alike(message, reread.get_message_by_name(message.name))


def test_convert_and_save(data_dir, tmp_path):
    report = convert_database(data_dir / 'example.arxml', tmp_path / 'copy.arxml')
    assert report.warnings == [] and report.messages == 3  # Buses kept apart: no merge or container warnings
    assert written_by_canexplorer(tmp_path / 'copy.arxml')
    assert save_database(load_database(data_dir / 'example.kcd')[0], tmp_path / 'kcd.arxml') == []
    assert load_database(tmp_path / 'kcd.arxml')[0].get_message_by_name('EngineData').frame_id == 0x100


def test_an_opened_system_description_is_not_overwritten(data_dir, tmp_path):
    source = tmp_path / 'system.arxml'
    source.write_bytes((data_dir / 'example.arxml').read_bytes())
    assert not can_write_back(source) and output_suffix('.arxml') == '.dbc'
    editor = DatabaseEditor(source)
    assert not editor.writable
    for target in (None, source):
        with pytest.raises(DatabaseFormatError, match='more than the signal database; save the changes under'):
            editor.save(target)
    assert source.read_bytes() == (data_dir / 'example.arxml').read_bytes()

    # A new ARXML is CANExplorer's own: Save writes over it, also after reopening it
    exported = tmp_path / 'exported.arxml'
    editor.save(exported)
    assert editor.writable and editor.path == exported
    editor.save()
    reopened = DatabaseEditor(exported)
    assert reopened.writable and can_write_back(exported)

    # Once another tool rewrote it (without the marker comment), it is protected again
    exported.write_text(exported.read_text(encoding='utf-8').replace(MARKER + '\n', ''), encoding='utf-8')
    assert not DatabaseEditor(exported).writable


def test_marker_needs_the_second_line(tmp_path):
    path = tmp_path / 'other.arxml'
    path.write_text(f'<?xml version="1.0"?>\n<AUTOSAR/>\n{MARKER}\n', encoding='utf-8')
    assert not written_by_canexplorer(path) and not written_by_canexplorer(tmp_path / 'missing.arxml')
    assert not can_write_back(Path(tmp_path / 'missing.arxml'))
