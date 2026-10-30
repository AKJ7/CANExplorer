import can

from can_explorer.core.reverse.reactions import capture_reaction


def frame(time, data, can_id=0x100, extended=False):
    return can.Message(timestamp=time, arbitration_id=can_id, is_extended_id=extended, data=bytes(data))


def test_a_bit_that_changes_only_during_the_action_is_found():
    # 0x100: byte 0 counts (changes in both windows), byte 1 bit 0 flips on only during the action
    baseline = [frame(i * 0.01, [i % 4, 0x00]) for i in range(40)]
    action = [frame(1.0 + i * 0.01, [i % 4, 0x01 if i >= 10 else 0x00]) for i in range(40)]
    report = capture_reaction(baseline, action)
    assert report.baseline_frames == 40 and report.action_frames == 40
    reaction = next(r for r in report.reactions if r.id_text == '100')
    keys = {item.field.key: item for item in reaction.fields}
    # The counter (byte 0) moved during the baseline too, so it is not reported; bit 8 (byte1 bit0) is
    assert '100[8|1@1+]' in keys
    change = keys['100[8|1@1+]']
    assert (change.before, change.after) == (0, 1) and change.action_flips >= 1
    assert not any(item.field.start < 8 for item in reaction.fields)  # byte 0 not reported


def test_a_new_identifier_appears():
    baseline = [frame(i * 0.01, [0], can_id=0x100) for i in range(30)]
    action = [frame(1.0 + i * 0.01, [0], can_id=0x100) for i in range(30)]
    action += [frame(1.0 + i * 0.05, [1, 2], can_id=0x201) for i in range(5)]  # New ID during the action
    report = capture_reaction(baseline, action)
    assert report.reactions[0].id_text == '201' and report.reactions[0].appeared  # Ranked first
    assert report.reactions[0].baseline_frames == 0


def test_contiguous_bits_are_grouped_and_values_reported():
    # The high nibble (bits 4..7) steps from 3 to 12 during the action, stable before. Only bits that actually
    # change are grouped, so a run needs every bit in it to move (0x30 -> 0xC0 flips all four).
    baseline = [frame(i * 0.01, [0x30]) for i in range(20)]
    action = [frame(1.0 + i * 0.01, [0x30 if i < 10 else 0xC0]) for i in range(20)]
    report = capture_reaction(baseline, action)
    reaction = report.reactions[0]
    assert len(reaction.fields) == 1
    field = reaction.fields[0]
    assert field.field.key == '100[4|4@1+]' and (field.before, field.after) == (3, 12)


def test_nothing_changes():
    baseline = [frame(i * 0.01, [5, 5]) for i in range(20)]
    action = [frame(1.0 + i * 0.01, [5, 5]) for i in range(20)]
    report = capture_reaction(baseline, action)
    assert report.reactions == []


def test_exclude_identifiers():
    baseline = [frame(i * 0.01, [0], can_id=0x7E8) for i in range(10)]
    action = [frame(1.0 + i * 0.01, [1], can_id=0x7E8) for i in range(10)]
    assert capture_reaction(baseline, action).reactions  # Found by default
    assert capture_reaction(baseline, action, exclude={(0x7E8, False)}).reactions == []
