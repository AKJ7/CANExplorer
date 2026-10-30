import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QSettings, QStandardPaths, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

MAX_RECENT = 10
# Must match the palettes of Theme.qml
THEMES = (
    'Dark',
    'Purple',
    'Nord',
    'Ocean',
    'Forest',
    'Light',
    'Dracula',
    'Solarized Dark',
    'High Contrast',
    'Cyberpunk',
    'Rosé',
)
DEFAULT_THEME = 'Dark'
DEFAULT_MAX_TRACE_ROWS = 100_000
DEFAULT_REFRESH_INTERVAL_MS = 33
TRACE_ROWS_RANGE = (1_000, 2_000_000)
REFRESH_INTERVAL_RANGE = (16, 1_000)
# Frames of the trace kept in total, the older ones on disk (0: only the frames in memory)
DEFAULT_TRACE_HISTORY = 5_000_000
TRACE_HISTORY_RANGE = (0, 50_000_000)
# Width of the sidebar in pixels; the default must match Theme.sidebarWidth
DEFAULT_SIDEBAR_WIDTH = 232
SIDEBAR_WIDTH_RANGE = (180, 420)


class AppState(QObject):
    """Persistent user state: recent connections and log files, preferences and window geometry."""

    recent_connections_changed = pyqtSignal()
    recent_log_files_changed = pyqtSignal()
    recent_workspaces_changed = pyqtSignal()
    preferences_changed = pyqtSignal()
    language_changed = pyqtSignal()

    def __init__(self, settings: Optional[QSettings] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = settings if settings is not None else QSettings('AKJ7', 'CANExplorer')

    def _load_list(self, key: str) -> List[Dict[str, Any]]:
        raw = self._settings.value(key, '[]')
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(f'Ignoring invalid {key!r} in settings')
            return []
        return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []

    def _store_list(self, key: str, entries: List[Dict[str, Any]]) -> None:
        self._settings.setValue(key, json.dumps(entries))
        self._settings.sync()

    @property
    def settings(self) -> QSettings:
        """The settings storage, for controllers keeping their own settings (e.g. updates)."""
        return self._settings

    @staticmethod
    def _push(entries: List[Dict[str, Any]], entry: Dict[str, Any], identity: str) -> List[Dict[str, Any]]:
        entries = [existing for existing in entries if existing.get(identity) != entry[identity]]
        return [entry, *entries][:MAX_RECENT]

    # Recent connections

    @pyqtProperty('QVariantList', notify=recent_connections_changed)
    def recent_connections(self) -> List[Dict[str, Any]]:
        return self._load_list('recent/connections')

    def add_recent_connection(self, connection: Dict[str, Any]) -> None:
        entry = dict(connection)
        entry['label'] = f'{entry["name"]} ({entry["interface"]}:{entry["channel"]})'
        entry['identity'] = f'{entry["name"]}|{entry["interface"]}|{entry["channel"]}'
        self._store_list('recent/connections', self._push(self.recent_connections, entry, 'identity'))
        self.recent_connections_changed.emit()

    # Recent log files

    @pyqtProperty('QVariantList', notify=recent_log_files_changed)
    def recent_log_files(self) -> List[Dict[str, Any]]:
        return self._load_list('recent/log_files')

    def add_recent_log_file(self, path: str, protocol: str, speed: float) -> None:
        entry = {'path': path, 'protocol': protocol, 'speed': speed}
        self._store_list('recent/log_files', self._push(self.recent_log_files, entry, 'path'))
        self.recent_log_files_changed.emit()

    # Recent workspaces

    @pyqtProperty('QVariantList', notify=recent_workspaces_changed)
    def recent_workspaces(self) -> List[Dict[str, Any]]:
        return self._load_list('recent/workspaces')

    def add_recent_workspace(self, path: str) -> None:
        entry = {'path': path, 'name': Path(path).stem}
        self._store_list('recent/workspaces', self._push(self.recent_workspaces, entry, 'path'))
        self.recent_workspaces_changed.emit()

    @pyqtSlot()
    def clear_recent(self) -> None:
        self._store_list('recent/connections', [])
        self._store_list('recent/log_files', [])
        self._store_list('recent/workspaces', [])
        self.recent_workspaces_changed.emit()
        self.recent_connections_changed.emit()
        self.recent_log_files_changed.emit()

    # Preferences

    def _int(self, key: str, default: int, bounds: tuple[int, int]) -> int:
        try:
            value = int(self._settings.value(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, bounds[0]), bounds[1])

    @pyqtProperty(int, notify=preferences_changed)
    def max_trace_rows(self) -> int:
        return self._int('preferences/max_trace_rows', DEFAULT_MAX_TRACE_ROWS, TRACE_ROWS_RANGE)

    @pyqtProperty(int, notify=preferences_changed)
    def refresh_interval_ms(self) -> int:
        return self._int('preferences/refresh_interval_ms', DEFAULT_REFRESH_INTERVAL_MS, REFRESH_INTERVAL_RANGE)

    @pyqtProperty(int, constant=True)
    def min_trace_rows(self) -> int:
        return TRACE_ROWS_RANGE[0]

    @pyqtProperty(int, constant=True)
    def max_trace_rows_limit(self) -> int:
        return TRACE_ROWS_RANGE[1]

    @pyqtProperty(int, notify=preferences_changed)
    def trace_history(self) -> int:
        return self._int('preferences/trace_history', DEFAULT_TRACE_HISTORY, TRACE_HISTORY_RANGE)

    @pyqtProperty(int, constant=True)
    def max_trace_history(self) -> int:
        return TRACE_HISTORY_RANGE[1]

    @staticmethod
    def default_trace_directory() -> str:
        """In the user's cache directory: the system's temporary directory is often in memory."""
        cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        return str(Path(cache or tempfile.gettempdir()) / 'trace')

    @pyqtProperty(str, notify=preferences_changed)
    def trace_directory(self) -> str:
        return str(self._settings.value('preferences/trace_directory', '') or self.default_trace_directory())

    @pyqtSlot(int, str)
    def set_trace_history(self, frames: int, directory: str) -> None:
        frames = min(max(int(frames), TRACE_HISTORY_RANGE[0]), TRACE_HISTORY_RANGE[1])
        self._settings.setValue('preferences/trace_history', frames)
        directory = directory.strip()
        self._settings.setValue(
            'preferences/trace_directory', '' if directory == self.default_trace_directory() else directory
        )
        self._settings.sync()
        self.preferences_changed.emit()

    @pyqtSlot(int, int)
    def set_preferences(self, max_trace_rows: int, refresh_interval_ms: int) -> None:
        rows = min(max(max_trace_rows, TRACE_ROWS_RANGE[0]), TRACE_ROWS_RANGE[1])
        interval = min(max(refresh_interval_ms, REFRESH_INTERVAL_RANGE[0]), REFRESH_INTERVAL_RANGE[1])
        self._settings.setValue('preferences/max_trace_rows', rows)
        self._settings.setValue('preferences/refresh_interval_ms', interval)
        self._settings.sync()
        self.preferences_changed.emit()

    # Appearance

    @pyqtProperty(str, notify=preferences_changed)
    def theme(self) -> str:
        value = self._settings.value('appearance/theme', DEFAULT_THEME)
        return value if value in THEMES else DEFAULT_THEME

    @pyqtProperty('QStringList', constant=True)
    def themes(self) -> List[str]:
        return list(THEMES)

    @pyqtSlot(str)
    def set_theme(self, theme: str) -> None:
        if theme in THEMES and theme != self.theme:
            self._settings.setValue('appearance/theme', theme)
            self._settings.sync()
            self.preferences_changed.emit()

    # Language

    @pyqtProperty(str, notify=language_changed)
    def language(self) -> str:
        """Language code of the interface; '' follows the system language."""
        return str(self._settings.value('appearance/language', '') or '')

    @pyqtProperty('QVariantList', constant=True)
    def languages(self) -> List[Dict[str, str]]:
        from can_explorer.gui.translations import available_languages

        return [{'code': code, 'name': name} for code, name in available_languages()]

    @pyqtSlot(str)
    def set_language(self, code: str) -> None:
        if code != self.language:
            self._settings.setValue('appearance/language', code)
            self._settings.sync()
            self.language_changed.emit()

    # Layout

    @pyqtProperty(int, constant=True)
    def min_sidebar_width(self) -> int:
        return SIDEBAR_WIDTH_RANGE[0]

    @pyqtProperty(int, constant=True)
    def max_sidebar_width(self) -> int:
        return SIDEBAR_WIDTH_RANGE[1]

    @pyqtSlot(result=int)
    def sidebar_width(self) -> int:
        """Stored width of the sidebar (read when the window opens)."""
        return self._int('window/sidebar_width', DEFAULT_SIDEBAR_WIDTH, SIDEBAR_WIDTH_RANGE)

    @pyqtSlot(int)
    def set_sidebar_width(self, width: int) -> None:
        low, high = SIDEBAR_WIDTH_RANGE
        self._settings.setValue('window/sidebar_width', max(low, min(high, int(width))))
        self._settings.sync()

    # MQTT

    def mqtt_options(self) -> Dict[str, Any]:
        """The MQTT options last used (broker, user, topic prefix, what to publish …; never the password)."""
        try:
            options = json.loads(self._settings.value('mqtt/options', '{}'))
        except (TypeError, ValueError):
            return {}
        return options if isinstance(options, dict) else {}

    def set_mqtt_options(self, options: Dict[str, Any]) -> None:
        self._settings.setValue('mqtt/options', json.dumps({k: v for k, v in options.items() if k != 'password'}))
        self._settings.sync()

    # Window geometry

    @pyqtSlot(result='QVariantList')
    def window_geometry(self) -> List[int]:
        geometry = self._settings.value('window/geometry')
        try:
            values = [int(value) for value in json.loads(geometry)]
        except (TypeError, ValueError):
            return []
        return values if len(values) == 4 and values[2] > 100 and values[3] > 100 else []

    @pyqtSlot(int, int, int, int)
    def save_window_geometry(self, x: int, y: int, width: int, height: int) -> None:
        self._settings.setValue('window/geometry', json.dumps([x, y, width, height]))
        self._settings.sync()
