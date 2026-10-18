"""Loading, saving and converting signal databases by file extension (DBC, DBF, KCD, SYM, ARXML)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cantools
from cantools.database.can import Database

from can_explorer.core import dbf

READ_SUFFIXES = ('.dbc', '.dbf', '.kcd', '.sym', '.arxml')
WRITE_SUFFIXES = ('.dbc', '.dbf', '.kcd', '.sym')


class DatabaseFormatError(ValueError):
    pass


def _suffix(path: Path, supported: Tuple[str, ...], action: str) -> str:
    suffix = path.suffix.lower()
    if suffix not in supported:
        raise DatabaseFormatError(f'Cannot {action} {suffix or "files without extension"}; use {", ".join(supported)}')
    return suffix


def load_database(path: str | Path) -> Tuple[Database, List[str]]:
    """Load any supported database. Returns the database and conversion warnings."""
    path = Path(path)
    if _suffix(path, READ_SUFFIXES, 'read') == '.dbf':
        return dbf.load_dbf(path)
    return cantools.database.load_file(str(path), strict=False), []


def save_database(database: Database, path: str | Path, protocol: Optional[str] = None) -> List[str]:
    """Save to the format given by the extension. Returns warnings about content the format cannot hold."""
    path = Path(path)
    suffix = _suffix(path, WRITE_SUFFIXES, 'write')
    if suffix == '.dbf':
        return dbf.save_dbf(database, path, protocol)
    text = {'.dbc': database.as_dbc_string, '.kcd': database.as_kcd_string, '.sym': database.as_sym_string}[suffix]()
    path.write_text(text, encoding='cp1252' if suffix == '.dbc' else 'utf-8', errors='replace')
    return []


@dataclass
class ConversionReport:
    source: Path
    destination: Path
    messages: int
    signals: int
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f'Converted {self.source.name} to {self.destination.name}: '
            f'{self.messages} messages, {self.signals} signals'
        )
        if self.warnings:
            text += f', {len(self.warnings)} warnings:\n' + '\n'.join(f'- {warning}' for warning in self.warnings)
        return text


def convert_database(source: str | Path, destination: str | Path, protocol: Optional[str] = None) -> ConversionReport:
    source, destination = Path(source), Path(destination)
    _suffix(destination, WRITE_SUFFIXES, 'write')
    if source.resolve() == destination.resolve():
        raise DatabaseFormatError('Source and destination are the same file')
    database, warnings = load_database(source)
    warnings += save_database(database, destination, protocol)
    signals = sum(len(message.signals) for message in database.messages)
    return ConversionReport(source, destination, len(database.messages), signals, warnings)
