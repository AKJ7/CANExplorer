"""How the trace shows the time of a frame.

- ``relative``: seconds since the start of the session (the default).
- ``delta``: seconds since the previous frame shown, so with a display filter the time between matching frames.
- ``absolute``: the local wall-clock time of day, ``HH:MM:SS.ffffff``.
- ``unix``: the raw timestamp, seconds since 1970-01-01 UTC.

Absolute and Unix times are only meaningful when the source stamps frames with wall-clock time: a log file
whose timestamps start at zero shows times on 1970-01-01.
"""

import datetime
from typing import Dict, Optional, Tuple

RELATIVE = 'relative'
DELTA = 'delta'
ABSOLUTE = 'absolute'
UNIX = 'unix'

TIME_FORMATS: Tuple[str, ...] = (RELATIVE, DELTA, ABSOLUTE, UNIX)
DEFAULT_TIME_FORMAT = RELATIVE

HEADERS: Dict[str, str] = {
    RELATIVE: 'Time [s]',
    DELTA: 'Δt [s]',
    ABSOLUTE: 'Time',
    UNIX: 'Unix time [s]',
}


def normalize_time_format(name: object) -> str:
    """A known time format name, else the default."""
    return name if isinstance(name, str) and name in TIME_FORMATS else DEFAULT_TIME_FORMAT


def time_header(time_format: str) -> str:
    return HEADERS[normalize_time_format(time_format)]


def format_time(
    time_format: str,
    timestamp: float,
    start: Optional[float] = None,
    previous: Optional[float] = None,
) -> str:
    """Text of a frame's `timestamp` in `time_format`.

    `start` is the start of the session (relative times count from the timestamp itself without one, i.e. 0);
    `previous` is the timestamp of the previous frame shown (a delta is 0 without one).
    """
    match normalize_time_format(time_format):
        case 'delta':
            return f'{timestamp - (timestamp if previous is None else previous):.6f}'
        case 'absolute':
            try:
                return datetime.datetime.fromtimestamp(timestamp).strftime('%H:%M:%S.%f')
            except (OverflowError, OSError, ValueError):
                return f'{timestamp:.6f}'  # Out of the platform's time range
        case 'unix':
            return f'{timestamp:.6f}'
    return f'{timestamp - (timestamp if start is None else start):.6f}'
