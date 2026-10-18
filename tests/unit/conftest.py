import os
import signal
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from pytestqt.qt_compat import qt_api

# Qt must not need a display in tests or CI
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'


@pytest.fixture(scope='session')
def qapp_cls():
    class QTestApplication(qt_api.QtWidgets.QApplication):
        def __init__(self, *args):
            self._register_signals()
            super().__init__(*args)

        def _register_signals(self):
            signal.signal(signal.SIGINT, signal.SIG_DFL)

    return QTestApplication


@pytest.fixture(scope='session')
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture(scope='session', autouse=True)
def isolated_settings(tmp_path_factory):
    """Keep tests from reading or writing the user's real application settings."""
    path = str(tmp_path_factory.mktemp('settings'))
    for settings_format in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(settings_format, QSettings.Scope.UserScope, path)
    return path
