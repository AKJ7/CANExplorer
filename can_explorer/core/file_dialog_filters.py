"""Name filters of file dialogs ("CAN log files (*.asc *.blf)"): their extensions and the default suffix they imply."""

import posixpath
import re
from typing import List

_PATTERNS = re.compile(r'\(([^()]*)\)\s*$')


def filter_extensions(name_filter: str) -> List[str]:
    """Extensions of a name filter without the leading "*.": "Logs (*.asc *.asc.gz *)" -> ['asc', 'asc.gz']."""
    match = _PATTERNS.search(name_filter)
    if match is None:
        return []
    return [pattern[2:] for pattern in match.group(1).split() if pattern.startswith('*.') and '*' not in pattern[2:]]


def default_suffix(name_filter: str, fallback: str = '') -> str:
    """The suffix a save dialog appends for this filter: its first extension, else the fallback."""
    extensions = filter_extensions(name_filter)
    return extensions[0] if extensions else fallback.lstrip('.')


def with_default_suffix(path: str, suffix: str) -> str:
    """Append ".suffix" when the file name has no suffix of its own (as Qt's QFileDialog.defaultSuffix does)."""
    name = posixpath.basename(path.replace('\\', '/'))
    if not suffix or not name or '.' in name.lstrip('.'):
        return path
    return f'{path}.{suffix.lstrip(".")}'
