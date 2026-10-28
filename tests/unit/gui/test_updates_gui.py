import asyncio
import functools
import hashlib
import json
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PyQt6.QtCore import QSettings

from can_explorer.core import updates
from can_explorer.gui.updates import UpdateController


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def server(tmp_path, monkeypatch):
    files = tmp_path / 'server'
    files.mkdir()
    appimage = files / 'CANExplorer-1.1.0-linux-x86_64.AppImage'
    appimage.write_bytes(b'new' * 5000)
    (files / 'SHA256SUMS.txt').write_text(f'{hashlib.sha256(appimage.read_bytes()).hexdigest()}  {appimage.name}\n')
    http = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(files)))
    base = f'http://127.0.0.1:{http.server_address[1]}'
    release = {
        'tag_name': 'v1.1.0',
        'name': 'CANExplorer 1.1.0',
        'body': '## Added\n\n- Something new',
        'html_url': 'https://github.com/AKJ7/CANExplorer/releases/tag/v1.1.0',
        'prerelease': False,
        'published_at': '2026-11-02T10:00:00Z',
        'assets': [
            {'name': name, 'browser_download_url': f'{base}/{name}'} for name in (appimage.name, 'SHA256SUMS.txt')
        ],
    }
    (files / 'releases.json').write_text(json.dumps([release]))
    monkeypatch.setenv('CANEXPLORER_UPDATE_URL', f'{base}/releases.json')
    threading.Thread(target=http.serve_forever, daemon=True).start()
    yield files
    http.shutdown()


@pytest.fixture
def settings(tmp_path):
    settings = QSettings(str(tmp_path / f'settings-{uuid.uuid4().hex}.ini'), QSettings.Format.IniFormat)
    yield settings


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out'
        await asyncio.sleep(0.02)


async def test_check_skip_and_install(qapp, server, settings, tmp_path, monkeypatch):
    current = tmp_path / 'CANExplorer.AppImage'
    current.write_bytes(b'old')
    monkeypatch.setenv('APPIMAGE', str(current))
    monkeypatch.setattr(updates, 'machine', lambda: 'x86_64')
    # An AppImage installation, also when the tests run on Windows or macOS (AppImages only exist on Linux)
    monkeypatch.setattr(updates, 'installation_kind', lambda *args, **kwargs: updates.APPIMAGE)
    controller = UpdateController(settings, current='1.0.0')
    assert controller.installation == 'AppImage' and controller.check_at_start

    announced = []
    controller.update_available.connect(announced.append)
    await controller._check(automatic=True)
    assert (controller.state, controller.version, controller.published) == ('available', '1.1.0', '2026-11-02')
    assert announced == ['1.1.0'] and controller.can_install and controller.install_hint == ''
    assert controller.notes.startswith('## Added')

    controller.skip()  # Automatic checks do not mention 1.1.0 again, manual checks do
    await controller._check(automatic=True)
    assert controller.state == 'idle' and announced == ['1.1.0']
    controller.check()
    await wait_for(lambda: controller.state == 'available')

    started, quit_calls = [], []
    monkeypatch.setattr(updates, 'start_detached', started.append)
    monkeypatch.setattr('can_explorer.gui.updates.QCoreApplication.quit', lambda: quit_calls.append(True))
    monkeypatch.setattr(controller, '_download_directory', lambda: tmp_path / 'downloads')
    controller.install()
    await wait_for(lambda: quit_calls)
    assert current.read_bytes() == b'new' * 5000 and started == [[str(current)]]
    assert controller.state == 'installing' and controller.progress == 1.0

    controller = UpdateController(settings, current='1.1.0')
    await controller._check(automatic=False)
    assert controller.state == 'current' and controller.version == ''


def test_check_at_startup_runs_on_every_start(qapp, settings, monkeypatch):
    from can_explorer.gui import updates as updates_gui

    scheduled = []
    monkeypatch.setattr(updates_gui.QTimer, 'singleShot', lambda ms, callback: scheduled.append((ms, callback)))
    controller = UpdateController(settings, current='1.0.0')

    controller.check_at_startup()
    controller.check_at_startup()  # A later restart: still scheduled, with no once-a-day throttle suppressing it
    assert len(scheduled) == 2 and scheduled[0][0] == updates_gui.STARTUP_DELAY_MS

    controller.set_check_at_start(False)
    controller.check_at_startup()  # Turned off in the preferences: no check is scheduled
    assert len(scheduled) == 2


async def test_errors_and_settings(qapp, settings, monkeypatch):
    monkeypatch.setenv('CANEXPLORER_UPDATE_URL', 'http://127.0.0.1:1/releases.json')  # Nothing listens there
    controller = UpdateController(settings, current='1.0.0')
    await controller._check(automatic=False)
    assert controller.state == 'error' and controller.error.startswith('Could not check for updates')
    await controller._check(automatic=True)  # Automatic checks fail silently
    assert controller.state == 'idle'
    controller.set_check_at_start(False)
    controller.set_include_prereleases(True)
    controller = UpdateController(settings, current='1.0.0')
    assert not controller.check_at_start and controller.include_prereleases
