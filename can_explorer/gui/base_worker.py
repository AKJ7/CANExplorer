from PyQt6.QtCore import QRunnable, QObject, pyqtSignal, pyqtSlot
import logging
import traceback

logger = logging.getLogger(__name__)


# Borrowed from https://www.pythonguis.com/tutorials/multithreading-pyqt-applications-qthreadpool/
class WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(Exception)
    result = pyqtSignal(object)
    progress = pyqtSignal(int)


class Worker(QRunnable):
    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self._signals = WorkerSignals()
        self._kwargs['progress_callback'] = self._signals.progress

    @pyqtSlot()
    def run(self):
        # try:
            logger.info(self._func)
            result = self._func(*self._args, **self._kwargs)
        # except Exception as e:
        #     traceback.print_exc()
        #    logger.error(f'An error occurred while running task: {e}')
        #     self._signals.error.emit(e)
        # else:
        #     self._signals.result.emit(result)
        # finally:
        #    self._signals.finished.emit()

