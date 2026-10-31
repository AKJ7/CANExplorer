from can_explorer.core.reverse.raster import (
    CELL_ONE,
    CELL_ONE_CHANGED,
    CELL_ZERO,
    CELL_ZERO_CHANGED,
    build_raster,
)


def test_empty_and_zero_length():
    assert build_raster([], []).columns == 0
    raster = build_raster([0.0, 1.0], [b'', b''])
    assert raster.rows == 0 and raster.columns == 0


def test_constant_bit_is_solid_and_toggling_bit_is_marked_changed():
    # Byte 0: bit 0 (LSB) toggles every frame, bit 7 (MSB) is always 1, the rest are 0
    payloads = [bytes([0x80 | (i & 1)]) for i in range(8)]
    raster = build_raster([i * 0.1 for i in range(8)], payloads)
    assert raster.rows == 8 and raster.columns == 8 and raster.length == 1
    # Row 0 is byte 0 bit 7 (the MSB): always 1, never changing
    assert all(raster.cell(0, c) == CELL_ONE for c in range(8))
    # Row 7 is byte 0 bit 0 (the LSB): toggles, so every column after the first is marked changed
    assert raster.cell(7, 0) in (CELL_ZERO, CELL_ONE)
    assert all(raster.cell(7, c) in (CELL_ZERO_CHANGED, CELL_ONE_CHANGED) for c in range(1, 8))
    # A constant-zero middle bit stays zero and unchanged
    assert all(raster.cell(3, c) == CELL_ZERO for c in range(8))


def test_downsampling_keeps_within_lengths_and_flags_changes_in_a_bucket():
    # 100 frames, byte 0 counts 0..99 -> low bits change within every bucket
    payloads = [bytes([i & 0xFF, 0x00]) for i in range(100)]
    raster = build_raster([i * 0.01 for i in range(100)], payloads, max_columns=10)
    assert raster.columns == 10 and raster.rows == 16 and raster.frames == 100
    # Byte 0 bit 0 (row 7) changes within every bucket of 10 counting frames
    assert all(raster.cell(7, c) in (CELL_ZERO_CHANGED, CELL_ONE_CHANGED) for c in range(10))
    # Byte 1 (rows 8..15) is constant zero
    assert all(raster.cell(r, c) == CELL_ZERO for r in range(8, 16) for c in range(10))


def test_short_frames_are_zero_padded_to_the_longest():
    raster = build_raster([0.0, 0.1], [b'\xff', b'\xff\xaa'])
    assert raster.length == 2 and raster.rows == 16
    # The second byte is absent in frame 0 (padded 0) and 0xAA in frame 1; column 1 represents frame 1.
    # Byte 1 bit 7 of 0xAA is 1, and it changed (0 -> 1) from the padded first frame
    assert raster.cell(8, 1) == CELL_ONE_CHANGED and raster.cell(8, 1) & CELL_ONE == 1
