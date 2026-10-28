import math

import can

from can_explorer.core.reverse.analysis import analyse_all, collect
from can_explorer.core.reverse.related import reference_from_field, signal_groups
from can_explorer.core.reverse.fields import BitField


def _frame(arbitration_id, data):
    return can.Message(arbitration_id=arbitration_id, is_extended_id=False, data=bytes(data), timestamp=0.0)


def _le16(value):
    value &= 0xFFFF
    return [value & 0xFF, value >> 8]


def _build():
    """Traffic where one slow signal appears in three messages (one scaled), plus an unrelated signal and noise."""
    messages = []
    period = 400
    for i in range(period):
        t = i * 0.01
        value = int(500 + 450 * math.sin(i * 2 * math.pi / period))  # 50..950, smooth, many distinct values
        other = int(500 + 450 * math.sin(i * 2 * math.pi / period + 1.3))  # A different phase: uncorrelated enough
        counter = i & 0xFF
        # 0x1A0: the signal in bytes 0-1
        messages.append(_make(0x1A0, t, _le16(value) + [counter, 0, 0, 0, 0, 0]))
        # 0x3B2: the same signal in bytes 2-3
        messages.append(_make(0x3B2, t, [0xAA, 0xAA] + _le16(value) + [0, 0, 0, 0]))
        # 0x4C4: twice the signal in bytes 0-1
        messages.append(_make(0x4C4, t, _le16(value * 2) + [0, 0, 0, 0, 0, 0]))
        # 0x200: a different signal (should not join the group)
        messages.append(_make(0x200, t, _le16(other) + [0, 0, 0, 0, 0, 0]))
    return messages


def _make(arbitration_id, timestamp, data):
    msg = can.Message(arbitration_id=arbitration_id, is_extended_id=False, data=bytes(data[:8]))
    msg.timestamp = timestamp
    return msg


def test_groups_the_same_signal_across_messages():
    messages = _build()
    frames = collect(messages)
    analyses = analyse_all(frames)
    groups = signal_groups(frames, analyses, min_correlation=0.98, max_lag=0.2)
    assert groups, 'expected at least one signal group'

    # The group holding 0x1A0's signal should also contain 0x3B2 and 0x4C4, but not 0x200
    def ids(group):
        return {member.field.frame_id for member in group.members}

    main = next(group for group in groups if 0x1A0 in ids(group))
    assert {0x1A0, 0x3B2, 0x4C4} <= ids(main)
    assert 0x200 not in ids(main)

    # Exactly one representative, and every other member correlates strongly with it
    assert sum(1 for m in main.members if m.representative) == 1
    assert all(abs(m.correlation) >= 0.98 for m in main.members if not m.representative)


def test_reference_from_field_samples_the_values():
    messages = [_make(0x100, i * 0.01, _le16(i * 3) + [0, 0, 0, 0, 0, 0]) for i in range(50)]
    frames = collect(messages)
    field = BitField(0x100, False, 0, 16)
    reference = reference_from_field(frames[(0x100, False)], field)
    assert len(reference) == 50
    assert reference.values[0] == 0.0 and reference.values[10] == 30.0
    assert (0x100, False) in reference.exclude  # The field's own identifier is excluded from matching


def test_no_groups_without_shared_signals():
    # Independent noise per identifier: nothing should group
    messages = []
    for i in range(300):
        messages.append(_make(0x111, i * 0.01, _le16((i * 7 + 13) % 1000) + [0, 0, 0, 0, 0, 0]))
        messages.append(_make(0x222, i * 0.01, _le16((i * 101 + 500) % 1000) + [0, 0, 0, 0, 0, 0]))
    frames = collect(messages)
    analyses = analyse_all(frames)
    groups = signal_groups(frames, analyses, min_correlation=0.99)
    assert all(not ({0x111, 0x222} <= {m.field.frame_id for m in g.members}) for g in groups)
