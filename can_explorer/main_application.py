import logging

from PyQt6.QtQml import (
    QQmlApplicationEngine,
    QQmlError,
)
from PyQt6.QtCore import (
    pyqtSignal,
    QObject,
    qInstallMessageHandler,
    QtMsgType,
    QMessageLogContext,
)
from can_explorer.sys.log_handler import get_logger
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QGuiApplication
from typing import Callable, Dict, List
import signal
import sys
from can_explorer.util.gui import get_res_path
from can_explorer.gui.application_metadata import ApplicationMetadata
from can_explorer.gui.connection_manager import ConnectionManager

logger = get_logger(__name__)


def create_qml_engine(
    context: Dict[str, QObject], on_warnings: Callable[[List[QQmlError]], None]
) -> QQmlApplicationEngine:
    """Load the main window with the given context properties (e.g. {'metadata': ...})."""
    engine = QQmlApplicationEngine()
    engine.setOutputWarningsToStandardError(False)
    engine.warnings.connect(on_warnings)
    engine.addImportPath(str(get_res_path('.')))
    for name, value in context.items():
        engine.rootContext().setContextProperty(name, value)
    engine.load(str(get_res_path('main_window.qml')))
    return engine


class MainApplication(QApplication, QGuiApplication):

    plugin_loaded = pyqtSignal()
    application_running = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(sys.argv)
        self.metadata = ApplicationMetadata(parent=self)
        self.connection_manager = ConnectionManager(parent=self)
        qInstallMessageHandler(self._message_handler)
        self._qml_engine = self._initialize_engine()
        self._configure()

    def _configure(self):
        self._qml_engine.quit.connect(super().quit)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    def _initialize_engine(self) -> QQmlApplicationEngine:
        return create_qml_engine(
            {'metadata': self.metadata, 'connectionManager': self.connection_manager},
            self._on_qml_warnings,
        )

    def _register_components(self):
        # engine =  QQmlEngine()
        # component = QQmlComponent(engine)
        # component.
        # qmlRegisterSingletonType(MainApplication, )
        pass

    @staticmethod
    def _on_qml_warnings(warnings: List[QQmlError]) -> None:
        for warning in warnings:
            logger.warning(f'Qml Warning: {warning.toString()}')

    @staticmethod
    def created():
        # TODO: Implement
        return False

    def excepthook(self, hook_type, value, traceback):
        # TODO: Show a crash dialog when the application is running
        logger.exception(f'An error occurred {traceback}')
        sys.exit(0)

    @staticmethod
    def _message_handler(mode: QtMsgType, context: QMessageLogContext, message: str):
        log_level = {
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtFatalMsg: logging.FATAL,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtSystemMsg: logging.INFO,
            QtMsgType.QtCriticalMsg: logging.CRITICAL,
        }
        if mode not in log_level:
            default = QtMsgType.QtCriticalMsg
            logger.warning(f'Could not find corresponding logging level for {mode=}. Defaulting to: {default=}')
            mode = default
        level = log_level.get(mode)
        prefix = 'System error' if context.file is None else f'{context.file}({context.line})'
        logger.log(level, f'[QML] {prefix} : {message}')
