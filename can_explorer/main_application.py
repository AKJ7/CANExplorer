import logging
import os

from PyQt6.QtQml import (
    QQmlApplicationEngine,
    QQmlError,
)
from PyQt6.QtCore import (
    QObject,
    qInstallMessageHandler,
    QtMsgType,
    QMessageLogContext,
)
from can_explorer.sys.log_handler import get_logger
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication
from typing import Callable, Dict, List
import signal
import sys
from can_explorer.util.gui import get_res_path
from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager
from can_explorer.desktop import APP_ID
from can_explorer.gui.signal_plot import register_qml_types

logger = get_logger(__name__)


APP_ICON = 'CanExplorer/images/logo.png'  # Generated from docs/assets/logo.png by tools/make_icons.py


def create_qml_engine(
    context: Dict[str, QObject], on_warnings: Callable[[List[QQmlError]], None]
) -> QQmlApplicationEngine:
    """Load the main window with the given context properties (e.g. {'metadata': ...})."""
    # Controls drawn with the theme colours on every platform (the native styles cannot be recoloured)
    os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'CanExplorerStyle')  # can_explorer/gui/qt/CanExplorerStyle
    register_qml_types()
    engine = QQmlApplicationEngine()
    engine.setOutputWarningsToStandardError(False)
    engine.warnings.connect(on_warnings)
    engine.addImportPath(str(get_res_path('.')))
    for name, value in context.items():
        engine.rootContext().setContextProperty(name, value)
    engine.load(str(get_res_path('main_window.qml')))
    return engine


class MainApplication(QApplication):
    def __init__(self):
        super().__init__(sys.argv)
        self.setOrganizationName('AKJ7')
        self.setApplicationName('CANExplorer')
        self.setWindowIcon(QIcon(str(get_res_path(APP_ICON))))
        self.setDesktopFileName(APP_ID)  # Lets Linux desktops match the window to the installed desktop entry
        self.metadata = ApplicationMetadata(parent=self)
        self.connection_manager = ConnectionManager(parent=self)
        qInstallMessageHandler(self._message_handler)
        self._qml_engine = create_qml_engine(
            {'metadata': self.metadata, 'connectionManager': self.connection_manager},
            self._on_qml_warnings,
        )
        self._qml_engine.quit.connect(self.quit)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    @staticmethod
    def _on_qml_warnings(warnings: List[QQmlError]) -> None:
        for warning in warnings:
            logger.warning(f'Qml Warning: {warning.toString()}')

    @staticmethod
    def _message_handler(mode: QtMsgType, context: QMessageLogContext, message: str):
        log_level = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.CRITICAL,
            QtMsgType.QtFatalMsg: logging.FATAL,
        }.get(mode, logging.CRITICAL)
        prefix = 'System error' if context.file is None else f'{context.file}({context.line})'
        logger.log(log_level, f'[QML] {prefix} : {message}')
