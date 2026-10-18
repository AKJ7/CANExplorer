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


def select_bus(database: Database, bus: str) -> None:
    """Keep only the messages of one bus (ARXML cluster, KCD bus), in place."""
    names = [candidate.name for candidate in database.buses]
    if bus not in names:
        raise DatabaseFormatError(f'Unknown bus {bus!r}; the database defines {", ".join(names) or "no buses"}')
    kept = [message for message in database.messages if message.bus_name == bus]
    database.messages.clear()
    database.messages.extend(kept)
    database.buses[:] = [candidate for candidate in database.buses if candidate.name == bus]
    database.refresh()


def _content_warnings(database: Database, suffix: str) -> List[str]:
    """Content that the output formats cannot represent."""
    warnings = []
    if len(database.buses) > 1:
        seen, clashes = {}, set()
        for message in database.messages:
            key = (message.frame_id, message.is_extended_frame)
            if key in seen and seen[key] != message.bus_name:
                clashes.add(message.frame_id)
            seen.setdefault(key, message.bus_name)
        text = f'{len(database.buses)} buses ({", ".join(bus.name for bus in database.buses)}) merged into one'
        if clashes:
            text += f'; identifiers {", ".join(f"0x{frame_id:X}" for frame_id in sorted(clashes))} clash'
        warnings.append(text + ' (convert one bus at a time to avoid this)')
    if suffix != '.dbf':  # The DBF writer reports this itself
        for message in database.messages:
            if message.is_container:
                warnings.append(f'Container message {message.name!r} written without its contained messages')
    return warnings


def save_database(database: Database, path: str | Path, protocol: Optional[str] = None) -> List[str]:
    """Save to the format given by the extension. Returns warnings about content the format cannot hold."""
    path = Path(path)
    suffix = _suffix(path, WRITE_SUFFIXES, 'write')
    warnings = _content_warnings(database, suffix)
    if suffix == '.dbf':
        return warnings + dbf.save_dbf(database, path, protocol)
    text = {'.dbc': database.as_dbc_string, '.kcd': database.as_kcd_string, '.sym': database.as_sym_string}[suffix]()
    path.write_text(text, encoding='cp1252' if suffix == '.dbc' else 'utf-8', errors='replace')
    return warnings


def count_signals(database: Database) -> int:
    return sum(len(part.signals) for message in database.messages for part in (message.contained_messages or [message]))


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


def convert_database(
    source: str | Path, destination: str | Path, protocol: Optional[str] = None, bus: Optional[str] = None
) -> ConversionReport:
    """Convert between formats. `bus` keeps only the messages of one bus of a multi-bus database."""
    source, destination = Path(source), Path(destination)
    _suffix(destination, WRITE_SUFFIXES, 'write')
    if source.resolve() == destination.resolve():
        raise DatabaseFormatError('Source and destination are the same file')
    database, warnings = load_database(source)
    if bus:
        select_bus(database, bus)
    warnings += save_database(database, destination, protocol)
    return ConversionReport(source, destination, len(database.messages), count_signals(database), warnings)
