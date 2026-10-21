import math

import can
import pytest

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.reverse.bitstats import BitStatistics
from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.synthetic import Behaviour, generate, score_fields
from can_explorer.core.signals import SignalTracker


def frame(time, data, can_id=0x100, extended=False):
    return can.Message(timestamp=time, arbitration_id=can_id, is_extended_id=extended, data=bytes(data))


@pytest.fixture(scope='module')
def complex_db(data_dir):
    return open_signal_database(data_dir / 'complex.dbc').database


# Bit fields


def test_fields_decode_like_cantools(complex_db):
    """Every field of the synthetic traffic (Intel and Motorola, signed, multiplexed, CAN FD) gives the raw value
    cantools decodes, in every frame."""
    traffic = generate(complex_db, duration=2.0, seed=3)
    assert len(traffic.fields) == 12  # The two floating point signals have no BitField
    for name, field in traffic.fields.items():
        message = complex_db.get_message_by_name(name.split('.')[0])
        signal = name.split('.')[1]
        checked = 0
        for msg in traffic.messages:
            if msg.arbitration_id != message.frame_id:
                continue
            decoded = message.decode(msg.data, scaling=False, decode_choices=False, allow_truncated=True)
            if signal in decoded:
                assert field.raw(msg.data) == decoded[signal], (name, msg)
                checked += 1
        assert checked > 0, name


def test_field_keys_bits_and_spanning():
    intel = BitField(0x1A0, False, 16, 8)
    assert intel.key == '1A0[16|8@1+]' and intel.bits == list(range(23, 15, -1)) and intel.min_length == 3
    motorola = BitField(0x18FEF1FE, True, 39, 10, 'big_endian', signed=True)
    assert motorola.key == '18FEF1FE[39|10@0-]'
    assert motorola.bits == [39, 38, 37, 36, 35, 34, 33, 32, 47, 46] and motorola.min_length == 6
    # Two clicks in a bit grid, in either order
    assert BitField.spanning(1, False, 23, 16) == BitField(1, False, 16, 8)
    assert BitField.spanning(1, False, 46, 39, 'big_endian') == BitField(1, False, 39, 10, 'big_endian')
    assert BitField.spanning(1, False, 39, 46, 'big_endian') == BitField(1, False, 39, 10, 'big_endian')
    assert BitField.from_dict(motorola.to_dict()) == motorola


def test_field_values():
    data = bytes([0x34, 0x12, 0xFF, 0x80])
    assert BitField(0, False, 0, 16).raw(data) == 0x1234
    assert BitField(0, False, 7, 16, 'big_endian').raw(data) == 0x3412
    assert BitField(0, False, 16, 8, signed=True).raw(data) == -1
    assert BitField(0, False, 24, 8, scale=0.5, offset=-10).value(data) == 54.0
    assert BitField(0, False, 24, 16).raw(data) is None  # Beyond the payload
    for options in ({'length': 0}, {'length': 65}, {'start': -1}, {'byte_order': 'middle'}, {'scale': 0}):
        with pytest.raises(ValueError):
            BitField(**{'frame_id': 0, 'extended': False, 'start': 0, 'length': 8, **options})


# Bit statistics


def test_counter_bits_and_constant_bytes():
    statistics = BitStatistics()
    statistics.update_all(frame(i * 0.01, [i % 256, 0xA5, i % 2]) for i in range(1024))
    item = statistics.get((0x100, False))
    assert item.count == 1024 and item.length == 3
    bits = {info.bit: info for info in item.bits()}
    # A counter: bit n changes every 2**n frames
    for bit in range(8):
        assert item.flips[bit] == 1024 // 2**bit - 1, bit
        assert math.isclose(bits[bit].flips, (1024 // 2**bit - 1) / 1023) and math.isclose(bits[bit].ones, 0.5)
    assert all(bits[bit].constant for bit in range(8, 16))
    assert [bits[bit].value for bit in range(8, 16)] == [1, 0, 1, 0, 0, 1, 0, 1]  # 0xA5
    counter, constant, toggle = item.bytes()
    assert (counter.distinct, counter.minimum, counter.maximum, round(counter.entropy, 6)) == (256, 0, 255, 8.0)
    assert (constant.distinct, constant.entropy, constant.value) == (1, 0.0, 0xA5)
    assert toggle.distinct == 2 and math.isclose(toggle.entropy, 1.0) and bits[16].flips == 1.0
    assert sum(counter.histogram) == 1024 and bits[0].changed_at == 10.23


def test_notch_finds_the_bits_of_an_action(complex_db):
    """Traffic with a step in EngineData.Flags at 3 s: after a notch at 2 s only its bits stand out.

    The other signals use all their bits before the notch (noise) or none (constant); a slow signal reaching new
    bits after the notch would stand out as well."""
    behaviours = {
        'EngineData.Flags': Behaviour('step', at=3.0),
        'EngineData.EngineSpeed': Behaviour('noise'),
        'EngineData.Torque': Behaviour('constant'),
        'EngineData.Temperature': Behaviour('noise'),
        'EngineData.Status': Behaviour('noise'),
        'Multiplexed.ValueA': Behaviour('noise'),
        'Multiplexed.ValueB': Behaviour('noise'),
    }
    traffic = generate(complex_db, duration=5.0, behaviours=behaviours, seed=1, messages=['EngineData', 'Multiplexed'])
    statistics = BitStatistics()
    before = [msg for msg in traffic.messages if msg.timestamp < 2.0]
    statistics.update_all(before)
    statistics.notch()
    assert statistics.notched and statistics.changed_since_notch() == {}
    statistics.update_all(msg for msg in traffic.messages if msg.timestamp >= 2.0)
    flags = traffic.fields['EngineData.Flags']
    changed = statistics.changed_since_notch()
    assert list(changed) == [(0x100, False)]
    assert {bit for bit in range(64) if changed[(0x100, False)] >> bit & 1} == set(flags.bits)
    item = statistics.get((0x100, False))
    assert {info.bit for info in item.bits() if info.masked} & set(flags.bits) == set()
    # An identifier appearing after the notch is new, and all its changes count
    statistics.on_message(frame(6.0, [1], can_id=0x7FF))
    statistics.on_message(frame(6.1, [2], can_id=0x7FF))
    assert statistics.is_new((0x7FF, False)) and not statistics.is_new((0x100, False))
    assert changed.keys() | {(0x7FF, False)} == statistics.changed_since_notch().keys()
    statistics.clear_notch()
    assert not statistics.notched and statistics.changed_since_notch() == {}
    statistics.reset()
    assert statistics.ids == {}


def test_longer_frames_and_ignored_frames():
    statistics = BitStatistics()
    statistics.on_message(frame(0, [1]))
    statistics.on_message(frame(1, [1, 2, 3]))
    statistics.on_message(can.Message(timestamp=2, arbitration_id=0x100, is_remote_frame=True, dlc=8))
    statistics.on_message(can.Message(timestamp=3, is_error_frame=True))
    item = statistics.get((0x100, False))
    assert item.count == 2 and item.length == 3 and len(statistics.ids) == 1
    assert [info.value for info in item.bytes()] == [1, 2, 3]


# Plotting fields


def test_signal_tracker_fields_without_database():
    tracker = SignalTracker()
    field = BitField(0x100, False, 0, 8, scale=0.5)
    key = tracker.track_field(field, 'V')
    tracker.backfill(key, [frame(0.0, [10]), frame(0.1, [20], can_id=0x200)])
    tracker.update(frame(0.2, [30]), None)
    tracker.update(frame(0.3, [40], extended=True), None)  # Another identifier
    assert tracker.series[key].values == [5.0, 15.0] and tracker.series[key].unit == 'V'
    assert tracker.drain_changed()
    # The same bits with another factor replace the field
    tracker.track_field(BitField(0x100, False, 0, 8, scale=2))
    assert tracker.tracked == [key] and tracker.fields[key].scale == 2
    tracker.track('EngineData', 'EngineSpeed')
    tracker.untrack_all(fields=False)
    assert tracker.tracked == [key]
    tracker.untrack_key(key)
    assert tracker.tracked == [] and tracker.fields == {}
    tracker.update(frame(0.4, [1]), None)
    assert not tracker.drain_changed()


# Synthetic traffic


def test_synthetic_traffic_is_reproducible(complex_db, tmp_path):
    first = generate(complex_db, duration=1.0, seed=7)
    second = generate(complex_db, duration=1.0, seed=7)
    assert [(msg.timestamp, bytes(msg.data)) for msg in first.messages] == [
        (msg.timestamp, bytes(msg.data)) for msg in second.messages
    ]
    times = [msg.timestamp for msg in first.messages]
    assert times == sorted(times) and 0 <= times[0] and times[-1] <= 1.0
    fd = [msg for msg in first.messages if msg.arbitration_id == 0x400]
    assert fd and all(msg.is_fd and len(msg.data) == 64 for msg in fd)
    path = tmp_path / 'synthetic.asc'
    first.write(path)
    assert len(list(can.LogReader(str(path)))) == len(first.messages)


def test_synthetic_behaviours(complex_db):
    behaviours = {
        'EngineData.EngineSpeed': Behaviour('counter'),
        'EngineData.Torque': Behaviour('constant'),
        'EngineData.Status': Behaviour('ramp', period=1.0, amplitude=0.95),
    }
    traffic = generate(complex_db, duration=2.0, behaviours=behaviours, messages=['EngineData', 'Multiplexed'])
    speed = [raw for _, raw in traffic.raw['EngineData.EngineSpeed']]
    assert speed == list(range(len(speed)))
    assert len({raw for _, raw in traffic.raw['EngineData.Torque']}) == 1
    status = [raw for _, raw in traffic.raw['EngineData.Status']]
    assert min(status) < 50 and max(status) > 450  # 0-511 within a period
    mux = [raw for _, raw in traffic.raw['Multiplexed.Mux']]
    assert set(mux) == {0, 1} and len(traffic.raw['Multiplexed.ValueA']) + len(
        traffic.raw['Multiplexed.ValueB']
    ) == len(mux)
    with pytest.raises(ValueError):
        Behaviour('wobble')


def test_score_fields():
    truth = [BitField(1, False, 0, 8), BitField(1, False, 8, 4, signed=True), BitField(2, True, 7, 16, 'big_endian')]
    found = [BitField(1, False, 0, 8), BitField(1, False, 8, 4), BitField(1, False, 12, 4)]
    score = score_fields(truth, found)
    assert len(score.matched) == 2 and score.missed == [truth[2]] and score.spurious == [found[2]]
    assert score.wrong_sign == [found[1]]
    assert math.isclose(score.precision, 2 / 3) and math.isclose(score.recall, 2 / 3)
    assert score_fields([], []).precision == 1.0
