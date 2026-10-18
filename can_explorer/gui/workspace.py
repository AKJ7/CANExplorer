"""Workspace files (.cxw): connections with their databases, filters, plots, simulation and test scripts.

Paths are stored relative to the workspace file, so a workspace can be moved or shared together with its files.
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from PyQt6.QtCore import QUrl

if TYPE_CHECKING:
    from can_explorer.gui.connection_manager import CanConnection, ConnectionManager

FORMAT = 'canexplorer-workspace'
VERSION = 1
SUFFIX = '.cxw'


class WorkspaceError(ValueError):
    pass


def _relative(path: str, base: Path) -> str:
    """Relative to the workspace directory when possible (also across directories), else absolute."""
    if not path:
        return ''
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # Another drive on Windows
        return str(path)


def _absolute(path: str, base: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def describe_connection(connection: 'CanConnection', base: Path) -> Optional[Dict[str, Any]]:
    origin = dict(connection.origin)
    if not origin:
        return None
    if origin['kind'] == 'replay':
        origin['path'] = _relative(origin['path'], base)
    entry: Dict[str, Any] = {'name': connection.name, **origin}
    entry['connected'] = bool(connection.connected) if origin['kind'] == 'bus' else True
    entry['database'] = _relative(connection.database_file, base)
    entry['database_bus'] = connection.database_bus
    entry['diagnostic_description'] = _relative(connection.diagnostics.description_file, base)
    entry['filter'] = connection.filter_text
    entry['signals'] = [{'key': key, 'unit': series.unit} for key, series in connection.session.signals.series.items()]
    simulation = connection.simulation
    entry['simulation'] = {
        'scripts': [_relative(str(script.path), base) for script in simulation.runtime.scripts],
        'panels': [_relative(path, base) for path in simulation._panels],
        'running': bool(simulation.running),
    }
    entry['tests'] = [_relative(str(script.path), base) for script in connection.tests.runner.scripts]
    return entry


def save_workspace(manager: 'ConnectionManager', path: Path, current: int = -1) -> None:
    base = path.parent.resolve()
    connections = [describe_connection(connection, base) for connection in manager._connections]
    data = {
        'format': FORMAT,
        'version': VERSION,
        'current': current,
        'connections': [entry for entry in connections if entry is not None],
    }
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def read_workspace(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except OSError as e:
        raise WorkspaceError(f'Cannot read {path.name}: {e}') from e
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WorkspaceError(f'{path.name} is not a CANExplorer workspace') from None
    if not isinstance(data, dict) or data.get('format') != FORMAT:
        raise WorkspaceError(f'{path.name} is not a CANExplorer workspace')
    if int(data.get('version', 0)) > VERSION:
        raise WorkspaceError(f'{path.name} was written by a newer CANExplorer (version {data["version"]})')
    return data


def open_workspace(manager: 'ConnectionManager', path: Path) -> List[str]:
    """Replace the connections with those of the workspace. Returns problems (e.g. missing files); the rest of
    the workspace is opened nevertheless."""
    data = read_workspace(path)
    base = path.parent.resolve()
    problems: List[str] = []
    while manager._connections:
        manager.remove_connection(0)

    for entry in data.get('connections', []):
        name = entry.get('name', '?')
        before = len(manager._connections)
        if entry.get('kind') == 'replay':
            log_path = _absolute(entry.get('path', ''), base)
            if not log_path.is_file():
                problems.append(f'{name}: log file {log_path} not found')
                continue
            manager.open_log_file(
                QUrl.fromLocalFile(str(log_path)), entry.get('protocol', 'Raw'), float(entry.get('speed', 1.0))
            )
        elif entry.get('kind') == 'bus':
            manager.add_connection({**entry.get('settings', {}), 'name': name})
        else:
            problems.append(f'{name}: unknown connection type {entry.get("kind")!r}')
            continue
        if len(manager._connections) == before:
            problems.append(f'{name}: could not be created')
            continue
        connection = manager._connections[-1]
        problems += _restore(connection, entry, base)

    return problems


def _restore(connection: 'CanConnection', entry: Dict[str, Any], base: Path) -> List[str]:
    name = connection.name
    problems: List[str] = []

    def existing(relative: str, what: str) -> Optional[Path]:
        if not relative:
            return None
        resolved = _absolute(relative, base)
        if not resolved.is_file():
            problems.append(f'{name}: {what} {resolved} not found')
            return None
        return resolved

    errors: List[str] = []
    connection.error_occurred.connect(errors.append)
    try:
        for key, what in (('database', 'database'), ('diagnostic_description', 'diagnostic description')):
            path = existing(entry.get(key, ''), what)
            if path is not None:
                connection.load_database(QUrl.fromLocalFile(str(path)))
        if entry.get('database_bus'):
            connection.set_database_bus(entry['database_bus'])
        if entry.get('filter'):
            connection.set_filter(entry['filter'])
        for signal in entry.get('signals', []):
            message, _, signal_name = signal.get('key', '').partition('.')
            if message and signal_name:
                connection.track_signal(message, signal_name, signal.get('unit', ''))

        simulation = entry.get('simulation', {})
        for relative in simulation.get('scripts', []):
            path = existing(relative, 'simulation script')
            if path is not None:
                error = connection.simulation.load_script(QUrl.fromLocalFile(str(path)))
                if error:
                    problems.append(f'{name}: {path.name}: {error}')
        for relative in simulation.get('panels', []):
            path = existing(relative, 'panel')
            if path is not None:
                connection.simulation.add_panel(QUrl.fromLocalFile(str(path)))
        for relative in entry.get('tests', []):
            path = existing(relative, 'test script')
            if path is not None:
                error = connection.tests.load_script(QUrl.fromLocalFile(str(path)))
                if error:
                    problems.append(f'{name}: {path.name}: {error}')

        if entry.get('kind') == 'bus' and entry.get('connected'):
            connection.open()
            if connection.connected and simulation.get('running'):
                error = connection.simulation.start()
                if error:
                    problems.append(f'{name}: simulation: {error}')
    finally:
        connection.error_occurred.disconnect(errors.append)
    problems += [f'{name}: {error}' for error in errors]
    return problems
