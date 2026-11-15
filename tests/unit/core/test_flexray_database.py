"""FlexRay cluster descriptions: PDUs with offsets and update bits, multiplexing, cluster parameters, controllers,
several clusters, and the simulator built on them."""

from pathlib import Path

import pytest

from can_explorer.core.flexray import FlexRayError, FlexRayFrame
from can_explorer.core.flexray_database import (
    ClusterSimulator,
    cluster_names,
    encode_frame,
    load_database,
)

DATA = Path(__file__).resolve().parents[2] / 'data'
ARXML = DATA / 'flexray_pdus.arxml'
FIBEX = DATA / 'flexray_fibex_pdus.xml'


def _definition(database, name):
    return next(d for d in database.definitions if d.name == name)


def test_arxml_frame_with_several_pdus_and_update_bits():
    db = load_database(ARXML)
    combined = _definition(db, 'FrCombined')
    assert [(p.name, p.offset, p.length, p.update_bit) for p in combined.pdus] == [
        ('EnginePdu', 0, 8, 120),  # START-POSITION 0, update bit 15.0
        ('WheelPdu', 8, 8, 121),  # START-POSITION 64: byte 8, update bit 15.1
    ]
    assert combined.signal_names == ['EngineSpeed', 'GearPosition', 'OilPressure', 'WheelSpeedFrontLeft']
    assert combined.message is None  # Not a single PDU at the start

    payload = bytearray(16)
    payload[0:2] = (4000).to_bytes(2, 'little')  # EngineSpeed 1000 rpm (0.25 rpm/bit)
    payload[8:10] = (12345).to_bytes(2, 'little')  # WheelSpeedFrontLeft 123.45 km/h
    payload[15] = 0b01  # Only the engine PDU was updated
    frame = FlexRayFrame(0, 'A', 60, 0, bytes(payload))
    _, values = db.decode(frame)
    assert values['EngineSpeed'] == 1000 and 'WheelSpeedFrontLeft' not in values
    assert db.describe(frame).endswith('WheelPdu not updated')
    payload[15] = 0b11
    _, values = db.decode(FlexRayFrame(0, 'A', 60, 0, bytes(payload)))
    assert values['WheelSpeedFrontLeft'] == pytest.approx(123.45)

    # The frames with one PDU decode as before
    engine = _definition(db, 'FrEngine')
    assert engine.message is engine.pdus[0].message and engine.pdus[0].update_bit is None


def test_arxml_cluster_parameters_and_controllers():
    db = load_database(ARXML)
    p = db.parameters
    assert (p.baudrate, p.cycle, p.macrotick, p.macro_per_cycle) == (10_000_000, 0.005, 1e-6, 5000)
    assert (p.static_slots, p.static_slot_duration, p.payload_length_static) == (50, 50, 4)
    assert (p.minislots, p.minislot_duration, p.network_idle_time) == (492, 5, 40)
    assert p.cycle_time == 0.005 and p.static_slot_time == pytest.approx(50e-6)
    assert p.static_slot_offset(12) == pytest.approx(11 * 50e-6)
    assert p.is_dynamic(50) is False and p.is_dynamic(60) is True and p.static_slot_offset(60) is None
    assert ('Static payload', '8 bytes') in p.rows() and ('Cycle', '5000 µs') in p.rows()
    assert db.controllers == {
        'EngineEcu': {
            'KEY-SLOT-ID': '12',
            'KEY-SLOT-USED-FOR-START-UP': 'true',
            'KEY-SLOT-USED-FOR-SYNC': 'true',
            'MICRO-PER-CYCLE': '200000',
        }
    }
    # A description without them knows nothing
    plain = load_database(DATA / 'flexray.arxml')
    assert not plain.parameters.known and plain.parameters.cycle_time is None and plain.parameters.rows() == []


def test_several_clusters_in_one_file():
    assert cluster_names(ARXML) == ['Chassis', 'Body']
    assert cluster_names(FIBEX) == ['Chassis', 'Comfort']
    db = load_database(ARXML)
    assert db.cluster == 'Chassis' and db.clusters == ['Chassis', 'Body']
    body = load_database(ARXML, 'Body')
    assert [(d.name, d.slot, d.cycles, d.channels) for d in body.definitions] == [
        ('FrStatus', 5, 'from 0, every 2', frozenset({'A'}))
    ]
    comfort = load_database(FIBEX, 'Comfort')
    assert [(d.name, d.slot, d.channels) for d in comfort.definitions] == [('Configuration', 3, frozenset({'B'}))]
    with pytest.raises(FlexRayError, match="no FlexRay cluster 'Powertrain'.*Chassis, Body"):
        load_database(ARXML, 'Powertrain')


def test_fibex_pdus_update_bits_multiplexing_and_cycle_counter():
    db = load_database(FIBEX)
    drive = _definition(db, 'Drive')
    assert [(p.name, p.offset, p.length, p.update_bit) for p in drive.pdus] == [
        ('TorquePdu', 0, 4, 24),
        ('BrakePdu', 4, 4, 56),
    ]
    assert db.describe(FlexRayFrame(0, 'A', 10, 3, bytes([0x10, 0x27, 0, 1, 0x20, 0x4E, 0, 0]))) == (
        'Drive: Torque=10000, BrakePdu not updated'
    )
    # The multiplexer: the switch selects the switched PDU; the static part is always there
    assert db.describe(FlexRayFrame(0, 'A', 20, 1, bytes([1, 0x34, 0x12, 7]))) == (
        'Diagnosis: Page=1, Alive=7, Voltage=4660'
    )
    _, values = db.decode(FlexRayFrame(0, 'A', 20, 3, bytes([2, 0x34, 0x12, 7])))
    assert values == {'Page': 2, 'Alive': 7, 'Temperature': 0x1234}
    # CYCLE-COUNTER: in that cycle of the 64 only
    once = _definition(db, 'Configuration')
    assert (once.base_cycle, once.repetition) == (5, 64)
    assert db.definition(FlexRayFrame(0, 'A', 30, 5, bytes(2))) is once
    assert db.definition(FlexRayFrame(0, 'A', 30, 6, bytes(2))) is None
    # Cluster parameters, in microseconds as NI-XNET's FIBEX files give them
    p = db.parameters
    assert (p.cycle, p.macrotick, p.static_slots, p.static_slot_duration, p.baudrate) == (
        pytest.approx(0.005),
        pytest.approx(1e-6),
        60,
        50,
        10_000_000,
    )


def test_encoding_frames_sets_update_bits():
    db = load_database(FIBEX)
    drive = _definition(db, 'Drive')
    assert encode_frame(drive, {'Torque': 5, 'BrakePressure': 9}) == bytes([5, 0, 0, 1, 9, 0, 0, 1])
    assert encode_frame(drive, {'Torque': 5}, update_bits=False) == bytes([5, 0, 0, 0, 0, 0, 0, 0])
    diagnosis = _definition(db, 'Diagnosis')
    assert encode_frame(diagnosis, {'Page': 2, 'Temperature': 300, 'Alive': 4}) == bytes([2, 0x2C, 0x01, 4])
    # Physical values are scaled (EngineSpeed 0.25 rpm/bit)
    arxml = load_database(ARXML)
    payload = encode_frame(_definition(arxml, 'FrEngine'), {'EngineSpeed': 1000})
    assert payload[:2] == (4000).to_bytes(2, 'little')
    _, values = arxml.decode(FlexRayFrame(0, 'A', 12, 0, payload))
    assert values['EngineSpeed'] == 1000


def test_value_texts_decode_as_text_or_number():
    db = load_database(DATA / 'flexray.arxml')
    frame = FlexRayFrame(0, 'A', 12, 0, bytes([0, 0, 2, 0, 0, 0, 0, 0]))
    assert db.decode(frame)[1]['GearPosition'] == 'Second'
    assert db.decode(frame, numeric=True)[1]['GearPosition'] == 2


def test_damaged_descriptions_raise_flexray_errors(tmp_path):
    # Two mappings with the same short name: cantools refuses the file; the error is a FlexRayError, not ValueError
    text = ARXML.read_text(encoding='utf-8').replace(
        '<SHORT-NAME>FrCombined_wheels</SHORT-NAME>', '<SHORT-NAME>FrCombined_engine</SHORT-NAME>'
    )
    path = tmp_path / 'damaged.arxml'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(FlexRayError, match='damaged.arxml'):
        load_database(path)


def test_simulator_follows_the_description_timing():
    db = load_database(ARXML)
    simulator = ClusterSimulator(db, start=0.0)
    assert simulator.cycle_time == 0.005  # The description's
    assert simulator.sync_slots == {12} and simulator.startup_slots == {12}  # The controller's key slot
    frames = simulator.frames_until(0.010)
    engine = [f for f in frames if f.slot == 12 and f.channel == 'A']
    assert [f.timestamp for f in engine] == pytest.approx([11 * 50e-6, 0.005 + 11 * 50e-6])
    assert all(f.sync and f.startup for f in engine)
    combined = next(f for f in frames if f.slot == 60)
    assert combined.payload[15] & 0b11 == 0b11  # The simulated PDUs are updated
    assert db.decode(combined)[1].keys() >= {'EngineSpeed', 'WheelSpeedFrontLeft'}

    # Without controllers: the two lowest slots sent every cycle carry sync frames
    plain = ClusterSimulator(load_database(DATA / 'flexray.arxml'), start=0.0)
    assert plain.sync_slots == {12} and plain.cycle_time == 0.005  # Only slot 12 is sent every cycle


def test_simulator_sends_what_it_is_given():
    db = load_database(ARXML)
    simulator = ClusterSimulator(db, start=0.0)
    simulator.transmit(12, 0, 1, 'A', bytes(range(8)))
    simulator.transmit(70, 1, 2, 'A+B', b'\x01\x02')  # Not in the description
    frames = simulator.frames_until(0.020)  # 4 cycles
    sent = [f for f in frames if f.slot == 12]
    assert [f.channel for f in sent] == ['A'] * 4 and all(f.transmitted and f.payload == bytes(range(8)) for f in sent)
    extra = [(f.cycle, f.channel) for f in frames if f.slot == 70]
    assert extra == [(1, 'A'), (1, 'B'), (3, 'A'), (3, 'B')]
    simulator.stop_transmit(12)
    assert not any(f.transmitted for f in simulator.frames_until(0.025) if f.slot == 12)
    with pytest.raises(FlexRayError, match='even number'):
        simulator.transmit(12, 0, 1, 'A', b'\x01')
    with pytest.raises(FlexRayError, match='schedule'):
        simulator.transmit(12, 4, 3, 'A', b'')
