import datetime

from can_explorer.core.timestamps import (
    DEFAULT_TIME_FORMAT,
    TIME_FORMATS,
    format_time,
    normalize_time_format,
    time_header,
)


def test_relative_counts_from_the_session_start():
    assert format_time('relative', 12.5, start=10.0) == '2.500000'
    assert format_time('relative', 12.5) == '0.000000'  # No session start yet


def test_delta_counts_from_the_previous_frame():
    assert format_time('delta', 12.5, start=10.0, previous=12.25) == '0.250000'
    assert format_time('delta', 12.5, start=10.0) == '0.000000'  # The first row


def test_absolute_is_the_local_time_of_day():
    timestamp = datetime.datetime(2026, 11, 13, 14, 3, 22, 123456).timestamp()
    assert format_time('absolute', timestamp, start=0.0) == '14:03:22.123456'


def test_absolute_out_of_range_falls_back_to_seconds():
    assert format_time('absolute', 1e20) == '100000000000000000000.000000'


def test_unix_is_the_raw_timestamp():
    assert format_time('unix', 1763040000.5, start=1763040000.0) == '1763040000.500000'


def test_unknown_formats_fall_back_to_the_default():
    assert DEFAULT_TIME_FORMAT == 'relative' and set(TIME_FORMATS) == {'relative', 'delta', 'absolute', 'unix'}
    assert normalize_time_format('bogus') == 'relative' and normalize_time_format(None) == 'relative'
    assert format_time('bogus', 12.0, start=10.0) == '2.000000'
    assert time_header('delta') == 'Δt [s]' and time_header('bogus') == 'Time [s]'
