import asyncio
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QCoreApplication, QObject, QStandardPaths, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices

from can_explorer.__version__ import __version__
from can_explorer.core import updates
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL = 24 * 3600  # Automatic checks at most once a day
STARTUP_DELAY_MS = 5_000  # Let the window open first
RELEASES_PAGE = f'https://github.com/{updates.REPOSITORY}/releases'


def _size(value: int) -> str:
    return f'{value / 1e6:.1f} MB'


class UpdateController(QObject):
    """Checks GitHub for new releases, and downloads and installs them."""

    changed = pyqtSignal()
    progress_changed = pyqtSignal()
    # A newer version was found by an automatic check (manual checks show their result in the dialog)
    update_available = pyqtSignal(str)

    def __init__(self, settings, parent: Optional[QObject] = None, current: str = str(__version__)):
        super().__init__(parent)
        self._settings = settings  # QSettings
        self._current = current
        self._kind = updates.installation_kind()
        self._release: Optional[updates.Release] = None
        self._state = 'idle'  # idle, checking, available, current, downloading, installing, error
        self._error = ''
        self._done = self._total = 0
        self._cancel = False
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(100)
        self._progress_timer.timeout.connect(self.progress_changed)

    # Settings

    def _value(self, key: str, default):
        value = self._settings.value(f'updates/{key}', default)
        if isinstance(default, bool):
            return value in (True, 'true', '1', 1)
        return type(default)(value)

    def _set(self, key: str, value) -> None:
        self._settings.setValue(f'updates/{key}', value)
        self._settings.sync()
        self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def check_at_start(self) -> bool:
        return self._value('check_at_start', True)

    @pyqtSlot(bool)
    def set_check_at_start(self, enabled: bool):
        self._set('check_at_start', enabled)

    @pyqtProperty(bool, notify=changed)
    def include_prereleases(self) -> bool:
        return self._value('include_prereleases', False)

    @pyqtSlot(bool)
    def set_include_prereleases(self, enabled: bool):
        self._set('include_prereleases', enabled)

    # State

    @pyqtProperty(str, notify=changed)
    def state(self) -> str:
        return self._state

    @pyqtProperty(str, constant=True)
    def current_version(self) -> str:
        return self._current

    @pyqtProperty(str, notify=changed)
    def version(self) -> str:
        return self._release.version if self._release else ''

    @pyqtProperty(str, notify=changed)
    def published(self) -> str:
        return self._release.published if self._release else ''

    @pyqtProperty(bool, notify=changed)
    def prerelease(self) -> bool:
        return bool(self._release and self._release.prerelease)

    @pyqtProperty(str, notify=changed)
    def notes(self) -> str:
        return self._release.notes if self._release else ''

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._error

    @pyqtProperty(str, constant=True)
    def installation(self) -> str:
        return updates.INSTALL_DESCRIPTIONS.get(self._kind, self._kind)

    @pyqtProperty(bool, notify=changed)
    def can_install(self) -> bool:
        """The update can be installed from the application (otherwise the release page is offered)."""
        return self._release is not None and updates.asset_name(self._release, self._kind) is not None

    @pyqtProperty(str, notify=changed)
    def install_hint(self) -> str:
        if self._kind == updates.SOURCE:
            return 'Running from source: update with git pull and uv sync.'
        if self._kind == updates.PORTABLE:
            return 'This copy was unpacked from an archive: download the new version from the release page.'
        if self._kind == updates.SNAP:
            return 'Installed as a snap: snapd installs updates automatically (or run: snap refresh canexplorer).'
        if self._kind == updates.DEB:
            return 'The package is installed with administrator rights; you will be asked for your password.'
        if self._release is not None and not self.can_install:
            return 'This release has no installer for this system; see the release page.'
        return ''

    @pyqtProperty(float, notify=progress_changed)
    def progress(self) -> float:
        return self._done / self._total if self._total else 0.0

    @pyqtProperty(str, notify=progress_changed)
    def progress_text(self) -> str:
        if not self._done:
            return ''
        return f'{_size(self._done)} of {_size(self._total)}' if self._total else _size(self._done)

    def _set_state(self, state: str, error: str = '') -> None:
        self._state, self._error = state, error
        self.changed.emit()

    # Checking

    def check_at_startup(self) -> None:
        """Check after a delay when enabled and not checked within the last day."""
        if not self.check_at_start:
            return
        if time.time() - self._value('last_check', 0.0) < CHECK_INTERVAL:
            return
        QTimer.singleShot(STARTUP_DELAY_MS, lambda: asyncio.ensure_future(self._check(automatic=True)))

    @pyqtSlot()
    def check(self):
        """Check now; the result is shown by `state` (also when the version was skipped)."""
        if self._state in ('checking', 'downloading', 'installing'):
            return
        asyncio.ensure_future(self._check(automatic=False))

    async def _check(self, automatic: bool) -> None:
        self._set_state('checking')
        try:
            releases = await asyncio.to_thread(updates.fetch_releases)
        except updates.UpdateError as e:
            logger.info(str(e))
            self._set_state('error' if not automatic else 'idle', str(e))
            return
        self._settings.setValue('updates/last_check', time.time())
        release = updates.newest_release(releases, self._current, self.include_prereleases)
        self._release = release
        if release is None:
            self._set_state('current')
            return
        if automatic and release.version == self._value('skipped', ''):
            self._set_state('idle')
            return
        self._set_state('available')
        logger.info(f'CANExplorer {release.version} is available (this is {self._current})')
        if automatic:
            self.update_available.emit(release.version)

    @pyqtSlot()
    def skip(self):
        """Do not mention this version again in automatic checks."""
        if self._release is not None:
            self._settings.setValue('updates/skipped', self._release.version)
        self._set_state('idle')

    @pyqtSlot()
    def dismiss(self):
        if self._state not in ('checking', 'downloading', 'installing'):
            self._set_state('available' if self._release else 'idle')

    @pyqtSlot()
    def open_release_page(self):
        QDesktopServices.openUrl(QUrl(self._release.url if self._release else RELEASES_PAGE))

    # Installing

    def _download_directory(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        return Path(base) / 'updates'

    @pyqtSlot()
    def install(self):
        if self._release is None or self._state in ('downloading', 'installing') or not self.can_install:
            return
        asyncio.ensure_future(self._install(self._release))

    async def _install(self, release: updates.Release) -> None:
        self._cancel = False
        self._done = self._total = 0
        self._set_state('downloading')
        self._progress_timer.start()

        def progress(done: int, total: int) -> None:  # Called in the download thread
            self._done, self._total = done, total

        try:
            path = await asyncio.to_thread(
                updates.download_update, release, self._kind, self._download_directory(), progress, lambda: self._cancel
            )
        except updates.UpdateError as e:
            self._progress_timer.stop()
            self.progress_changed.emit()
            self._set_state('available' if self._cancel else 'error', '' if self._cancel else str(e))
            return
        self._progress_timer.stop()
        self.progress_changed.emit()
        self._set_state('installing')
        try:
            restart = updates.install(self._kind, path)
        except (updates.UpdateError, OSError) as e:
            self._set_state('error', f'Could not install the update: {e}')
            return
        logger.info(f'Installing CANExplorer {release.version}; quitting')
        if restart:
            updates.start_detached(restart)
        # Quit through the event loop, so connections and settings are closed properly
        asyncio.get_running_loop().call_later(0.3, QCoreApplication.quit)

    @pyqtSlot()
    def cancel(self):
        self._cancel = True
