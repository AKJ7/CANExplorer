import functools
import hashlib
import importlib.util
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from can_explorer.core import updates

ROOT = Path(__file__).resolve().parents[3]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def release_server(tmp_path, monkeypatch):
    """A local stand-in for GitHub: the releases list, an AppImage, a disk image and their checksums."""
    files = tmp_path / 'server'
    files.mkdir()
    appimage = files / 'CANExplorer-1.1.0-linux-x86_64.AppImage'
    appimage.write_bytes(b'new version ' * 1000)
    (files / 'CANExplorer-1.1.0-macos-arm64.dmg').write_bytes(b'tampered')  # Not matching its checksum
    sums = f'{hashlib.sha256(appimage.read_bytes()).hexdigest()}  {appimage.name}\n'
    sums += f'{"0" * 64}  CANExplorer-1.1.0-macos-arm64.dmg\n'
    (files / 'SHA256SUMS.txt').write_text(sums)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(files)))
    base = f'http://127.0.0.1:{server.server_address[1]}'

    def release(tag, prerelease=False, draft=False, names=()):
        return {
            'tag_name': tag,
            'name': f'CANExplorer {tag}',
            'body': f'## Changes in {tag}',
            'html_url': f'https://github.com/AKJ7/CANExplorer/releases/tag/{tag}',
            'prerelease': prerelease,
            'draft': draft,
            'published_at': '2026-11-02T10:00:00Z',
            'assets': [{'name': name, 'browser_download_url': f'{base}/{name}'} for name in names],
        }

    names = (appimage.name, 'CANExplorer-1.1.0-macos-arm64.dmg', 'SHA256SUMS.txt')
    data = [
        release('v2.0.0', draft=True),
        release('v1.2.0-rc1', prerelease=True, names=names),
        release('v1.1.0', names=names),
        release('v1.0.0'),
    ]
    (files / 'releases.json').write_text(json.dumps(data))
    monkeypatch.setenv('CANEXPLORER_UPDATE_URL', f'{base}/releases.json')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield files
    server.shutdown()


def test_versions():
    assert updates.is_newer('1.0.1', '1.0.0') and updates.is_newer('1.10.0', '1.9.3')
    assert updates.is_newer('1.0.0', '1.0.0-rc2') and updates.is_newer('1.0.0-rc2', '1.0.0-rc1')
    assert updates.is_newer('v1.0.0', '0.0.1') and not updates.is_newer('1.0.0', '1.0.0')
    assert not updates.is_newer('1.0.0-beta', '1.0.0-rc1') and not updates.is_newer('nonsense', '0.0.1')


def test_releases_and_selection(release_server):
    releases = updates.fetch_releases()
    assert [release.version for release in releases] == ['1.2.0-rc1', '1.1.0', '1.0.0']  # Without the draft
    newest = updates.newest_release(releases, '1.0.0')
    assert (newest.version, newest.published, newest.notes) == ('1.1.0', '2026-11-02', '## Changes in v1.1.0')
    assert updates.newest_release(releases, '1.0.0', include_prereleases=True).version == '1.2.0-rc1'
    assert updates.newest_release(releases, '1.1.0') is None


def test_installation_kinds_and_assets(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, 'machine', lambda: 'x86_64')
    assert updates.installation_kind({'APPIMAGE': '/x/CANExplorer.AppImage'}, 'linux') == updates.APPIMAGE
    snap = {'SNAP': '/snap/canexplorer/12', 'SNAP_NAME': 'canexplorer'}
    assert updates.installation_kind(snap, 'linux') == updates.SNAP
    assert updates.installation_kind({}, 'linux') == updates.SOURCE  # Not frozen
    monkeypatch.setattr(updates, 'frozen', lambda: True)
    monkeypatch.setattr(updates, 'executable', lambda: Path('/opt/canexplorer/CANExplorer'))
    assert updates.installation_kind({}, 'linux') == updates.DEB
    app = tmp_path / 'CANExplorer.app' / 'Contents' / 'MacOS' / 'CANExplorer'
    monkeypatch.setattr(updates, 'executable', lambda: app)
    assert updates.installation_kind({}, 'darwin') == updates.MACOS_APP
    windows = tmp_path / 'Programs' / 'CANExplorer'
    windows.mkdir(parents=True)
    monkeypatch.setattr(updates, 'executable', lambda: windows / 'CANExplorer.exe')
    assert updates.installation_kind({}, 'win32') == updates.PORTABLE
    (windows / 'unins000.exe').touch()
    assert updates.installation_kind({}, 'win32') == updates.WINDOWS_SETUP
    release = updates.Release(
        '1.1.0',
        'v1.1.0',
        '',
        '',
        '',
        False,
        '',
        {
            'CANExplorer-1.1.0-windows-x86_64-setup.exe': 'u1',
            'CANExplorer-1.1.0-windows-x86_64.zip': 'u2',
            'CANExplorer-1.1.0-linux-x86_64.deb': 'u3',
        },
    )
    assert updates.asset_name(release, updates.WINDOWS_SETUP) == 'CANExplorer-1.1.0-windows-x86_64-setup.exe'
    assert updates.asset_name(release, updates.DEB) == 'CANExplorer-1.1.0-linux-x86_64.deb'
    assert (
        updates.asset_name(release, updates.APPIMAGE) is None and updates.asset_name(release, updates.PORTABLE) is None
    )
    assert updates.asset_name(release, updates.SNAP) is None  # snapd updates snaps


def test_download_verify_and_install_appimage(release_server, tmp_path, monkeypatch):
    monkeypatch.setattr(updates, 'machine', lambda: 'x86_64')
    release = updates.newest_release(updates.fetch_releases(), '1.0.0')
    progress = []
    path = updates.download_update(release, updates.APPIMAGE, tmp_path / 'downloads', lambda *p: progress.append(p))
    assert path.read_bytes() == (release_server / path.name).read_bytes()
    assert progress[-1] == (12000, 12000)

    current = tmp_path / 'apps' / 'CANExplorer.AppImage'
    current.parent.mkdir()
    current.write_bytes(b'old version')
    assert updates.install_appimage(path, {'APPIMAGE': str(current)}) == current
    assert current.read_bytes() == path.read_bytes()
    if os.name != 'nt':  # Windows has no execute permission bits
        assert current.stat().st_mode & 0o111
    assert updates.restart_command(updates.APPIMAGE, {'APPIMAGE': str(current)}) == [str(current)]

    with pytest.raises(updates.UpdateError, match='Cancelled'):
        updates.download_update(release, updates.APPIMAGE, tmp_path / 'other', cancelled=lambda: True)
    assert not list((tmp_path / 'other').glob('*.part'))
    monkeypatch.setattr(updates, 'machine', lambda: 'arm64')
    with pytest.raises(updates.UpdateError, match='damaged'):
        updates.download_update(release, updates.MACOS_APP, tmp_path / 'downloads')
    release.assets.pop('SHA256SUMS.txt')
    with pytest.raises(updates.UpdateError, match='no SHA256SUMS.txt'):
        updates.download_update(release, updates.MACOS_APP, tmp_path / 'downloads')
    with pytest.raises(updates.UpdateError, match='cannot be installed automatically'):
        updates.install(updates.PORTABLE, path)


def test_only_github_without_a_test_server(monkeypatch):
    monkeypatch.delenv('CANEXPLORER_UPDATE_URL', raising=False)
    with pytest.raises(updates.UpdateError, match='Refusing to download from example.org'):
        updates.download('https://example.org/CANExplorer.AppImage', Path('unused'))


def test_windows_setup_restarts_after_updates():
    spec = importlib.util.spec_from_file_location('installer', ROOT / 'packaging' / 'installer.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    script = installer.inno_setup_script(Path('bundle'), Path('out'))
    assert 'Check: IsUpdate' in script and "ExpandConstant('{param:update|0}') = '1'" in script
    assert 'CloseApplications=force' in script and 'skipifsilent' in script
