import math
import random

import numpy as np

from can_explorer.gui.signal_plot import _min_max_columns


def _reference(times, values, start, x_scale):
    """The former pure-Python min/max-per-column reduction, for comparison."""
    column, column_min, column_max = None, 0.0, 0.0
    out = []
    for timestamp, value in zip(times, values, strict=True):
        if not math.isfinite(value):
            continue
        pixel = int((timestamp - start) * x_scale)
        if pixel != column:
            if column is not None:
                out.append((column, column_min, column_max))
            column, column_min, column_max = pixel, value, value
        else:
            column_min, column_max = min(column_min, value), max(column_max, value)
    if column is not None:
        out.append((column, column_min, column_max))
    return out


def test_min_max_columns_matches_reference():
    rng = random.Random(5)
    for _ in range(500):
        count = rng.randint(0, 400)
        start = rng.uniform(-5, 5)
        # Sorted times, some before `start`; a few non-finite values
        times = sorted(rng.uniform(-10, 10) for _ in range(count))
        values = []
        for _ in range(count):
            r = rng.random()
            values.append(math.nan if r < 0.05 else math.inf if r < 0.07 else rng.uniform(-1000, 1000))
        x_scale = rng.choice([0.5, 5.0, 50.0, 300.0])

        columns, mins, maxs = _min_max_columns(np.asarray(times), np.asarray(values), start, x_scale)
        expected = _reference(times, values, start, x_scale)
        assert [int(c) for c in columns] == [c for c, _, _ in expected]
        assert mins.tolist() == [lo for _, lo, _ in expected]
        assert maxs.tolist() == [hi for _, _, hi in expected]


def test_min_max_columns_empty_and_all_nan():
    empty = np.empty(0)
    columns, mins, maxs = _min_max_columns(empty, empty, 0.0, 1.0)
    assert columns.size == 0 and mins.size == 0 and maxs.size == 0

    columns, mins, maxs = _min_max_columns(np.array([1.0, 2.0]), np.array([math.nan, math.inf]), 0.0, 1.0)
    assert columns.size == 0
