# TODO: Add script to fetch version, build_info and build_date at app creation

import sys
from PyQt6.QtCore import QObject, pyqtProperty, QT_VERSION_STR, PYQT_VERSION, PYQT_VERSION_STR
from can_explorer.sys.log_handler import get_logger
from can_explorer import settings
from can_explorer.__version__ import __version__
from datetime import datetime
import json

logger = get_logger(__name__)


class ApplicationMetadata(QObject):

    CONFIG = {'final': True, 'constant': True}

    def __init__(self, parent: QObject):
        super().__init__(parent=parent)

    @pyqtProperty(str, final=True, constant=True)
    def name(self) -> str:
        return settings.app_name

    @pyqtProperty(str, final=True, constant=True)
    def version(self) -> str:
        return str(__version__)

    @pyqtProperty(str, final=True, constant=True)
    def build_info(self):
        return ''

    @pyqtProperty(str, final=True, constant=True)
    def build_date(self) -> str:
        return datetime.today().strftime('%Y-%m-%d %H:%M:%S')

    @pyqtProperty(str, **CONFIG)
    def runtime_version(self) -> str:
        return sys.version

    @pyqtProperty(str, **CONFIG)
    def qt_version(self) -> str:
        return QT_VERSION_STR

    @pyqtProperty(str, **CONFIG)
    def pyqt_version(self) -> str:
        return PYQT_VERSION_STR

    @pyqtProperty(str, **CONFIG)
    def creation_year(self) -> str:
        return str(settings.creation_year)

    @pyqtProperty(str, **CONFIG)
    def actual_year(self) -> str:
        return datetime.today().strftime('%Y')

    @pyqtProperty(str, **CONFIG)
    def author(self) -> str:
        return settings.author

    @pyqtProperty(str, **CONFIG)
    def license_type(self) -> str:
        return settings.license_type

    @pyqtProperty(str, **CONFIG)
    def build_info(self) -> str:
        return settings.platform

    @pyqtProperty(str, **CONFIG)
    def app_info(self) -> str:
        info = {
            'Build': f'{self.build_info}, built on {self.build_date}',
            'Runtime version': f'{self.runtime_version}',
            'QT': f'v{self.qt_version}',
            'PYQT': f'v{self.pyqt_version}',
            'Powered by': f'{self.license_type}',
            'Copyright': f'@ {self.creation_year}-{self.actual_year} {self.author}',
        }
        return '\n'.join([f'{key}: {value}' for key, value in info.items()])
