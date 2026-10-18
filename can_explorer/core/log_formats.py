"""Log file formats. Recording, export, replay and replaying on a bus all use this one list, so every format that
can be recorded can be replayed and vice versa.

The formats are python-can's, chosen by the file extension. MF4 needs the optional asammdf package
(`uv sync --extra mf4`); the text formats can also be gzip compressed (e.g. `trace.asc.gz`).
"""

import importlib.util
from pathlib import Path
from typing import Dict, List, Tuple

FORMAT_NAMES: Dict[str, str] = {
    '.asc': 'Vector ASCII',
    '.blf': 'Vector binary logging format',
    '.csv': 'Comma separated values',
    '.log': 'can-utils candump log',
    '.trc': 'PEAK trace',
    '.db': 'SQLite',
    '.mf4': 'ASAM MDF 4',
}
# python-can compresses the text formats only (not BLF, SQLite or MF4, which compresses itself)
COMPRESSIBLE: Tuple[str, ...] = ('.asc', '.csv', '.log', '.trc')
GZIP = '.gz'


def mf4_available() -> bool:
    """Whether asammdf is installed (checked without importing it: it pulls in numpy and pandas)."""
    return importlib.util.find_spec('asammdf') is not None


def _suffixes() -> Tuple[str, ...]:
    plain = tuple(suffix for suffix in FORMAT_NAMES if suffix != '.mf4' or mf4_available())
    return plain + tuple(suffix + GZIP for suffix in COMPRESSIBLE)


LOG_FILE_SUFFIXES: Tuple[str, ...] = _suffixes()


def log_suffix(path: str | Path) -> str:
    """The log format suffix of a file name, including '.gz' for compressed files: 'trace.asc.gz' -> '.asc.gz'."""
    name = Path(path).name.lower()
    for suffix in sorted(LOG_FILE_SUFFIXES, key=len, reverse=True):
        if name.endswith(suffix):
            return suffix
    suffixes = Path(name).suffixes
    return ''.join(suffixes[-2:]) if suffixes[-1:] == [GZIP] else Path(name).suffix


def is_log_file(path: str | Path) -> bool:
    return log_suffix(path) in LOG_FILE_SUFFIXES


def unsupported_message(path: str | Path) -> str:
    suffix = log_suffix(path) or Path(path).name
    if suffix == '.mf4':
        return 'MF4 log files need the asammdf package: install CANExplorer with the "mf4" extra'
    return f'Unsupported log format {suffix!r}, use one of {", ".join(LOG_FILE_SUFFIXES)}'


def format_name(suffix: str) -> str:
    if suffix.endswith(GZIP):
        return f'{FORMAT_NAMES[suffix[: -len(GZIP)]]}, gzip compressed'
    return FORMAT_NAMES[suffix]


def file_filters() -> List[str]:
    """Name filters for file dialogs: all log files first, then one per format."""
    patterns = ' '.join(f'*{suffix}' for suffix in LOG_FILE_SUFFIXES)
    return [f'CAN log files ({patterns})', *(f'{format_name(suffix)} (*{suffix})' for suffix in LOG_FILE_SUFFIXES)]
