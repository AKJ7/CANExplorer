from PyQt6.uic import loadUi
from PyQt6.QtWidgets import QDialog, QLabel, QApplication, QPushButton
import logging
from can_explorer.util.gui import get_res_path
from can_explorer import PROJECT_NAME, PROJECT_PLATFORM, PROJECT_BUILD_DATE
from PyQt6.QtCore import QT_VERSION, PYQT_VERSION
from can_explorer.__version__ import __version__
import sys

logger = logging.getLogger(__name__)


class AboutDialog(QDialog):
    def __init__(self, parent=None, app=None):
        super(AboutDialog, self).__init__(parent)
        ui_path = get_res_path('about_dialog.ui')
        loadUi(ui_path, self)
        self._parent = parent
        self._app: QApplication = app
        self._configure()
        self._connect_signals()

    def _configure(self):
        info = AboutDialog._get_program_info()
        self.setWindowTitle(info['about'])
        self.about_program_name.setText(info['version'])
        self.about_build_info.setText(info['build'])
        self.about_runtime_info.setText(info['runtime'])
        self.about_copyright.setText(info['copyright'])

    def _connect_signals(self):
        self.about_copy_to_clipboard.released.connect(self._copy_info_to_clipboard)

    def _copy_info_to_clipboard(self):
        info = AboutDialog._get_program_info()
        build_info = '\n\n'.join([f'{key}: {value}'for key, value in info.items()])
        logger.info(f'Copy to clipboard {build_info=}')
        clipboard = self._app.clipboard()
        clipboard.setText(build_info)

    @staticmethod
    def _get_program_info():
        return {
            'about': f'About {PROJECT_NAME}',
            'version': f'{PROJECT_NAME} ({__version__})',
            'build': f'{PROJECT_PLATFORM}-{__version__}, built on {PROJECT_BUILD_DATE}',
            'runtime': f'Python Runtime version: {sys.version}\nQt Version {QT_VERSION}, PyQt Version: {PYQT_VERSION}',
            'copyright': f'Copyright @2024-2024 {PROJECT_NAME}'
        }

