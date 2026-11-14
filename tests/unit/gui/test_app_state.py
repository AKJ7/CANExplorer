import pytest
from PyQt6.QtCore import QSettings

from can_explorer.gui.app_state import DEFAULT_MAX_TRACE_ROWS, MAX_RECENT, AppState


@pytest.fixture
def state(qapp, tmp_path):
    return AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))


def connection(name, channel='vcan0'):
    return {'name': name, 'interface': 'VirtualBus', 'channel': channel, 'bitrate': 500000, 'protocol': 'Raw'}


def test_recent_connections_are_unique_and_bounded(state):
    for i in range(MAX_RECENT + 3):
        state.add_recent_connection(connection(f'Bus {i}'))
    state.add_recent_connection(connection('Bus 5'))
    names = [entry['name'] for entry in state.recent_connections]
    assert len(names) == MAX_RECENT and names[0] == 'Bus 5' and names.count('Bus 5') == 1
    assert state.recent_connections[0]['label'] == 'Bus 5 (VirtualBus:vcan0)'


def test_recent_log_files_persist(state, tmp_path):
    state.add_recent_log_file('/logs/a.blf', 'J1939', 0.0)
    reloaded = AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    assert reloaded.recent_log_files == [{'path': '/logs/a.blf', 'protocol': 'J1939', 'speed': 0.0}]
    reloaded.clear_recent()
    assert reloaded.recent_log_files == [] and reloaded.recent_connections == []


def test_preferences_are_clamped(state):
    assert state.max_trace_rows == DEFAULT_MAX_TRACE_ROWS
    state.set_preferences(5, 5000)
    assert state.max_trace_rows == 1_000 and state.refresh_interval_ms == 1_000


def test_trace_time_format_persists(state, tmp_path):
    changes = []
    state.preferences_changed.connect(lambda: changes.append(True))
    assert state.trace_time_format == 'relative'
    state.set_trace_time_format('unix')
    state.set_trace_time_format('unix')  # No change, no signal
    assert changes == [True]
    reloaded = AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    assert reloaded.trace_time_format == 'unix'
    reloaded.set_trace_time_format('bogus')
    assert reloaded.trace_time_format == 'relative'


def test_window_geometry(state):
    assert state.window_geometry() == []
    state.save_window_geometry(10, 20, 800, 600)
    assert state.window_geometry() == [10, 20, 800, 600]
    state.save_window_geometry(10, 20, 5, 5)
    assert state.window_geometry() == []


def test_corrupt_settings_are_ignored(state, tmp_path):
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    settings.setValue('recent/connections', '{not json')
    settings.setValue('preferences/max_trace_rows', 'many')
    assert state.recent_connections == [] and state.max_trace_rows == DEFAULT_MAX_TRACE_ROWS


def test_theme(state, tmp_path):
    assert state.theme == 'Dark' and 'Light' in state.themes
    state.set_theme('Nord')
    state.set_theme('Unknown')
    reloaded = AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    assert reloaded.theme == 'Nord'


def test_system_theme_is_offered_first_and_persists(state, tmp_path):
    assert state.themes[0] == 'System' and state.themes[1:] == sorted(state.themes[1:], key=str.casefold)
    state.set_theme('System')
    reloaded = AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    assert reloaded.theme == 'System'


def test_system_light_and_dark_themes(state, tmp_path):
    assert (state.system_light_theme, state.system_dark_theme) == ('Light', 'Dark')
    state.set_system_themes('Solarized Light', 'Gruvbox Dark')
    state.set_system_themes('System', 'Unknown')  # Neither is a palette: both ignored
    reloaded = AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    assert (reloaded.system_light_theme, reloaded.system_dark_theme) == ('Solarized Light', 'Gruvbox Dark')


def test_spirit_easter_egg(state, tmp_path):
    # The hidden 'Spirit' theme is not offered until it is awakened.
    assert not state.spirit_unlocked and 'Spirit' not in state.themes
    state.awaken_spirit()
    assert state.spirit_unlocked and state.theme == 'Spirit' and 'Spirit' in state.themes
    # It is persisted: a reload keeps it unlocked and active.
    reloaded = AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    assert reloaded.spirit_unlocked and reloaded.theme == 'Spirit'
