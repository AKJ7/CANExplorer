import sys

import pytest

from can_explorer import desktop


def test_desktop_entry_quotes_the_command():
    entry = desktop.desktop_entry(['/opt/My Apps/CANExplorer', '--log_level', 'DEBUG'])
    assert 'Exec="/opt/My Apps/CANExplorer" --log_level DEBUG\n' in entry
    assert f'Icon={desktop.APP_ID}\n' in entry and entry.startswith('[Desktop Entry]\n')


def test_launch_command_of_the_environment():
    command = desktop.launch_command()
    assert command[0].endswith('canexplorer') or command == [sys.executable, '-m', 'can_explorer']


def test_install_and_uninstall(tmp_path):
    entry = desktop.install(tmp_path, command=['/usr/bin/canexplorer'], update_caches=False)
    assert entry == tmp_path / 'applications' / f'{desktop.APP_ID}.desktop'
    assert 'Exec=/usr/bin/canexplorer\n' in entry.read_text()
    icons = sorted((tmp_path / 'icons' / 'hicolor').glob(f'*/apps/{desktop.APP_ID}.png'))
    assert len(icons) == len(desktop.ICON_SIZES)
    assert icons[0].read_bytes().startswith(b'\x89PNG')
    removed = desktop.uninstall(tmp_path, update_caches=False)
    assert len(removed) == len(desktop.ICON_SIZES) + 1 and not entry.exists()
    assert desktop.uninstall(tmp_path, update_caches=False) == []


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux only')
def test_command_line_option(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    monkeypatch.setattr(desktop, '_update_caches', lambda base: None)
    assert desktop.run('--install-desktop-entry') == 0
    assert (tmp_path / 'applications' / f'{desktop.APP_ID}.desktop').exists()
    assert desktop.run('--uninstall-desktop-entry') == 0
    assert 'Removed' in capsys.readouterr().out
