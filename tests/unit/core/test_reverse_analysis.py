import asyncio
import math
import random

import can
import pytest

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.reverse.analysis import analyse, analyse_all, classify, collect
from can_explorer.core.reverse.checksums import crc8, find_checksums, sae_j1850
from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.markers import Marker, find_marker_changes
from can_explorer.core.reverse.obd import ObdPoller, is_obd_identifier, request, response
from can_explorer.core.reverse.reference import (
    Reference,
    candidates,
    correlate,
    read_reference_csv,
    reference_from_signal,
)
from can_explorer.core.reverse.synthetic import Behaviour, discoverable, generate, score_fields

FULL = Behaviour('sine', period=20, amplitude=0.95)


@pytest.fixture(scope='module')
def reverse_db(data_dir):
    return open_signal_database(data_dir / 'reverse.dbc').database


def behaviours():
    """Every signal of reverse.dbc exercising all its bits within a minute."""
    result = {
        'Powertrain.Checksum': Behaviour('checksum'),
        'Powertrain.Counter': Behaviour('counter'),
        'Powertrain.Gear': Behaviour('toggle'),
        'Powertrain.EngineSpeed': FULL,
        'Powertrain.Torque': FULL,
        'Powertrain.Throttle': Behaviour('ramp', period=15, amplitude=0.95),
        'Body.VehicleSpeed': FULL,
        'Body.SteeringAngle': Behaviour('sine', period=30, amplitude=0.95),  # Crossing 0, so its sign shows
        'Battery.Temperature': Behaviour('sine', period=40, amplitude=0.95),
    }
    for cell in 'AB':
        for group in range(3):
            result[f'Battery.Cell{cell}{group}'] = Behaviour('sine', period=10 + 5 * group, amplitude=0.95)
    return result


def frame(time, data, can_id=0x100, extended=False):
    return can.Message(timestamp=time, arbitration_id=can_id, is_extended_id=extended, data=bytes(data))


# Checksums


def test_crc8_check_values():
    check = b'123456789'
    assert sae_j1850(check) == 0x4B  # CRC-8/SAE-J1850
    assert crc8(check, 0x2F, 0xFF, 0xFF) == 0xDF  # CRC-8/AUTOSAR
    assert crc8(check, 0x07) == 0xF4  # CRC-8/SMBUS
    assert crc8(check, 0x31, reflected=True) == 0xA1  # CRC-8/MAXIM-DOW
    assert crc8(check, 0x9B, 0xFF) == 0xDA  # CRC-8/CDMA2000


def test_find_checksums():
    rng = random.Random(1)
    payloads = []
    for counter in range(200):
        # Like AUTOSAR E2E profile 1: a CRC over a data ID and the other bytes, in byte 0
        data = [0, counter % 16, rng.randrange(256), rng.randrange(256), 0x55]
        data[0] = sae_j1850(bytes([0x34, 0x12]) + bytes(data[1:]))
        payloads.append(bytes(data))
    (match,) = find_checksums(payloads)
    assert (match.byte, match.algorithm, match.share) == (0, 'CRC-8 SAE J1850 (AUTOSAR)', 1.0)
    assert match.constant is not None and match.description.startswith('CRC-8 SAE J1850 (AUTOSAR), constant 0x')
    # A sum (plus a constant, e.g. of the identifier) in the last byte
    payloads = [bytes([counter % 16, rng.randrange(256), rng.randrange(256)]) for counter in range(200)]
    payloads = [payload + bytes([(sum(payload) + 7) & 0xFF]) for payload in payloads]
    (match,) = find_checksums(payloads)
    assert (match.byte, match.algorithm, match.constant) == (3, 'Sum of the bytes', 7)

    # Profile 2 like: the data ID depends on the counter in the low nibble of byte 1
    data_ids = [rng.randrange(256) for _ in range(16)]
    payloads = []
    for counter in range(320):
        data = [0, counter % 16, rng.randrange(256), rng.randrange(256)]
        data[0] = sae_j1850(bytes([data_ids[counter % 16]]) + bytes(data[1:]))
        payloads.append(bytes(data))
    match = find_checksums(payloads)[0]
    assert (match.byte, match.constant, match.counter_byte) == (0, None, 1)
    assert 'per counter value' in match.description

    # Random bytes, constant data and a byte copying another one are no checksums
    assert find_checksums([bytes(rng.randrange(256) for _ in range(8)) for _ in range(200)]) == []
    assert find_checksums([bytes([7, 7, 7])] * 100) == []
    copies = []
    for _ in range(100):
        value = rng.randrange(256)
        copies.append(bytes([value, value, 0]))
    assert find_checksums(copies) == []
    assert find_checksums([bytes([1, 2])] * 5) == []  # Too few frames


# Classification and analysis


def test_classify():
    field = BitField(1, False, 0, 8)
    assert classify([5] * 50, field).kind == 'constant'
    counter = classify([i % 16 for i in range(100)], BitField(1, False, 0, 4))
    assert (counter.kind, counter.details) == ('counter', 'counter, step +1, 0–15')
    assert classify([(15 - i) % 16 for i in range(100)], BitField(1, False, 0, 4)).details.startswith(
        'counter, step -1'
    )
    # Rising by one per frame without running through the range: a value, not a counter
    assert classify(list(range(40, 90)), field).kind == 'physical'
    assert classify([i // 30 % 2 for i in range(100)], BitField(1, False, 0, 1)).kind == 'flag'
    assert classify([(0, 3, 9)[i // 20 % 3] for i in range(100)], field).kind == 'enum'
    smooth = [round(100 * math.sin(i / 20)) & 0xFF for i in range(300)]  # Crossing 0: signed
    signed = classify(smooth, field)
    assert (signed.kind, signed.field.signed, signed.minimum < 0) == ('physical', True, True)
    rng = random.Random(2)
    assert classify([rng.randrange(256) for _ in range(300)], field).kind == 'unknown'


@pytest.mark.parametrize('seed', [0, 1, 2])
def test_analysis_finds_the_signals(reverse_db, seed):
    """Checksum, counter, flags and enumerations, Intel and Motorola values, signed ones, and multiplexed ones."""
    traffic = generate(reverse_db, duration=60, seed=seed, behaviours=behaviours())
    analyses = analyse_all(collect(traffic.messages))
    # Each multiplexer value carries other cell voltages at the same bits: those count once here
    found = list(
        dict.fromkeys(item.field for analysis in analyses for item in analysis.fields if item.kind != 'constant')
    )
    score = score_fields(discoverable(traffic.fields.values(), traffic.messages), found)
    assert (score.missed, score.spurious, score.wrong_sign) == ([], [], [])
    kinds = {(item.field.key, item.kind) for analysis in analyses for item in analysis.fields}
    assert {('123[0|8@1+]', 'checksum'), ('123[8|4@1+]', 'counter'), ('600[0|2@1+]', 'multiplexer')} <= kinds
    assert ('500[23|16@0-]', 'physical') in kinds and ('123[56|1@1+]', 'flag') in kinds
    battery = next(analysis for analysis in analyses if analysis.key == (0x600, False))
    assert battery.multiplexer.field.key == '600[0|2@1+]'
    cells = {(item.field.key, item.mux_value) for item in battery.fields if item.mux_value is not None}
    assert cells == {(key, value) for key in ('600[8|16@1+]', '600[24|16@1+]') for value in (0, 1, 2)}
    temperature = next(item for item in battery.fields if item.field.start == 40)
    assert temperature.mux_value is None and temperature.multiplexer is None  # In every frame


def test_neighbouring_values_are_split():
    """A slow and a fast value next to each other (Motorola order would join them into one smooth field)."""
    rng = random.Random(3)
    frames = []
    for index in range(2000):
        slow = 120 - 100 * math.cos(index / 300) + rng.gauss(0, 0.5)
        fast = 128 + 100 * math.sin(index / 15) + rng.gauss(0, 1)
        frames.append(frame(index * 0.01, [round(slow), round(fast), 0, 0]))
    analysis = analyse(collect(frames)[(0x100, False)])
    fields = {item.field.key: item.kind for item in analysis.fields if item.kind != 'constant'}
    assert fields == {'100[0|8@1+]': 'physical', '100[8|8@1+]': 'physical'}


def test_analysis_notes():
    assert analyse(collect([frame(0, [1])])[(0x100, False)]).note.startswith('Too few frames')
    assert analyse(collect([frame(i, []) for i in range(30)])[(0x100, False)]).note == 'No data bytes'
    # Frames of other lengths are left out
    frames = [frame(i * 0.01, [i % 16, 0]) for i in range(100)] + [frame(2, [1, 2, 3])]
    analysis = analyse(collect(frames)[(0x100, False)])
    assert (analysis.frames, analysis.length) == (100, 2)
    assert collect(frames, max_frames=10)[(0x100, False)].payloads[0] == bytes([91 % 16, 0])
    assert collect(frames, exclude={(0x100, False)}) == {}


# References


def test_reference_from_a_csv_file(reverse_db, tmp_path):
    traffic = generate(reverse_db, duration=60, seed=1, behaviours=behaviours())
    frames = collect(traffic.messages)
    rng = random.Random(0)
    # A GPS logger: once a second, 0.3 s late, with noise, times relative to the first frame
    start = traffic.messages[0].timestamp
    path = tmp_path / 'gps.csv'
    lines = ['time_s,speed [km/h],satellites']
    for time, raw in traffic.raw['Body.VehicleSpeed'][::50]:
        lines.append(f'{time + 0.3 - start:.3f},{raw * 0.01 + rng.gauss(0, 0.2):.2f},{rng.randrange(5, 12)}')
    path.write_text('\n'.join(lines))
    speed, satellites = read_reference_csv(path, time_offset=start)
    assert (speed.name, speed.unit, len(speed), satellites.name) == ('speed', 'km/h', 60, 'satellites')
    analyses = analyse_all(frames)
    best = correlate(speed, frames, candidates(frames, analyses))[0]
    assert best.field.key == '500[7|16@0+]' and best.correlation > 0.99
    assert math.isclose(best.field.scale, 0.01, rel_tol=0.01) and abs(best.field.offset) < 1
    assert best.lag == -0.3
    assert correlate(Reference('empty'), frames, candidates(frames)) == []


def test_reference_from_a_database_signal(reverse_db):
    """The engine speed sent in a second message (e.g. on a gateway) is found from the first one."""
    traffic = generate(reverse_db, duration=20, seed=0, behaviours=behaviours(), messages=['Powertrain'])
    messages = list(traffic.messages)
    for msg in traffic.messages:
        copy = bytes(msg.data[2:4]) + bytes(6)
        messages.append(frame(msg.timestamp + 0.002, copy, can_id=0x321))
    messages.sort(key=lambda msg: msg.timestamp)
    reference = reference_from_signal(reverse_db, messages, 'Powertrain', 'EngineSpeed')
    assert reference.unit == 'rpm' and reference.exclude == {(0x123, False)}
    frames = collect(messages)
    best = correlate(reference, frames, candidates(frames))[0]
    assert (best.field.key, best.field.scale) == ('321[0|16@1+]', 0.25) and best.correlation > 0.999


def test_reference_csv_errors(tmp_path):
    path = tmp_path / 'empty.csv'
    path.write_text('time\n')
    with pytest.raises(ValueError, match='no data rows'):
        read_reference_csv(path)
    path.write_text('time;name\n1;a\n2;b\n')
    with pytest.raises(ValueError, match='no numeric columns'):
        read_reference_csv(path)
    path.write_text('time;value\n' + '\n'.join(f'{1.7e9 + i};{i}' for i in range(20)))
    (reference,) = read_reference_csv(path, time_offset=100)
    assert reference.times[0] == 1.7e9  # Absolute times are kept


# OBD-II


async def test_obd_polling():
    sent = []
    poller = ObdPoller(sent.append, [0x0C, 0x0D], interval=0.01)
    poller.start()
    await asyncio.sleep(0.05)
    poller.stop()
    assert poller.requests >= 4 and not poller.running
    assert [bytes(msg.data[:3]) for msg in sent[:2]] == [b'\x02\x01\x0c', b'\x02\x01\x0d']
    assert sent[0].arbitration_id == 0x7DF
    for index in range(20):
        answer = response(0x0C, bytes([0x1A, 0xF8 + index % 4]))  # (256 * 26 + 248) / 4 = 1726 rpm
        answer.timestamp = index * 0.1
        poller.on_message(answer)
    poller.on_message(response(0x05, bytes([90])))  # Not polled
    poller.on_message(frame(0, [3, 0x7F, 0x01, 0x12], can_id=0x7E8))  # Negative response
    assert poller.answers(0x0C) == 20 and poller.answers(0x0D) == 0
    (reference,) = poller.references()
    assert (reference.name, reference.unit, reference.values[0]) == ('OBD Engine speed', 'rpm', 1726.0)
    assert poller.reference(0x0D).values == [] and poller.reference(0x42) is None

    def failing(msg):
        raise RuntimeError('Session is not open')

    broken = ObdPoller(failing, [0x0D], extended=True)
    broken.start()
    await asyncio.sleep(0.01)
    assert broken.error == 'Session is not open' and not broken.running
    assert request(0x0D, extended=True).arbitration_id == 0x18DB33F1
    with pytest.raises(ValueError):
        ObdPoller(sent.append, [0x99])
    with pytest.raises(ValueError):
        ObdPoller(sent.append, [])


def test_obd_identifiers():
    assert is_obd_identifier((0x7DF, False)) and is_obd_identifier((0x7E8, False))
    assert is_obd_identifier((0x18DAF110, True)) and not is_obd_identifier((0x7E8, True))
    assert not is_obd_identifier((0x123, False))


# Markers


def test_markers_find_the_bits_of_an_action():
    """A door switch (byte 2, bit 3) toggling at the markers, among smooth and noisy traffic."""
    rng = random.Random(4)
    frames, markers = [], []
    door = 0
    toggles = {5.0, 12.0, 20.0, 31.0, 40.0}
    for index in range(5000):
        time = index * 0.01
        if round(time, 2) in toggles:
            door ^= 1
            markers.append(Marker(time + 0.2, 'door'))  # Marked a little late
        frames.append(frame(time, [rng.randrange(256), index // 100 % 256, door << 3, 0]))
        if index % 7 == 0:
            frames.append(frame(time, [rng.randrange(2)], can_id=0x200))  # Toggles constantly
    matches = find_marker_changes(collect(frames), markers)
    best = matches[0]
    assert best.field.key == '100[19|1@1+]' and (best.hits, best.markers) == (5, 5)
    assert best.transitions == ('0 → 1', '1 → 0') and best.background == 0
    assert all(match.field.frame_id != 0x200 for match in matches)  # Changes everywhere, not specific
    assert find_marker_changes(collect(frames), []) == []
