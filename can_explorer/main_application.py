import logging

from PyQt6.QtQml import (
    QQmlApplicationEngine,
    QQmlError,
    qmlRegisterType,
    qmlRegisterModule,
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonType,
)
from PyQt6.QtCore import (
    pyqtSignal,
    pyqtProperty,
    QCoreApplication,
    QObject,
    qInstallMessageHandler,
    QtMsgType,
    QMessageLogContext,
)
from can_explorer.sys.log_handler import get_logger
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QGuiApplication
from typing import List
import signal
import sys
from can_explorer.util.gui import get_res_path
from can_explorer.gui.application_metadata import ApplicationMetadata

logger = get_logger(__name__)


class MainApplication(QApplication, QGuiApplication):

    plugin_loaded = pyqtSignal()
    application_running = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(sys.argv)
        self.metadata = ApplicationMetadata(parent=self)
        qInstallMessageHandler(self._message_handler)
        self._qml_engine = self._initialize_engine()
        self._configure()

    def _configure(self):
        self._qml_engine.quit.connect(super().quit)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    def _initialize_engine(self) -> QQmlApplicationEngine:
        engine = QQmlApplicationEngine()
        engine.setOutputWarningsToStandardError(False)
        engine.warnings.connect(self._on_qml_warnings)
        qml_dir = get_res_path('.')
        engine.addImportPath(str(qml_dir))
        main_window_path = get_res_path('main_window.qml')
        # qmlRegisterSingletonType(
        #     MainApplication, str(main_window_path), 1, 0, lambda eng, val: self, 'MainApplicationWindow'
        # )
        # qmlRegisterType(MainApplication, str(main_window_path), 1, 0, 'MainApplication')
        engine.rootContext().setContextProperty('metadata', self.metadata)
        engine.load(str(main_window_path))
        return engine

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

    def run(self) -> int:
        exec_result = super().exec()
        return exec_result

    def excepthook(self, hook_type, value, traceback):
        # TODO:
        from can_explorer.crash_handler import CrashHandler
        from can_explorer.main_application import MainApplication

        app_created = MainApplication.created()
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
