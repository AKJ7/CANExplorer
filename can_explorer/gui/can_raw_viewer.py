import logging
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QModelIndex, QThreadPool
from can.message import Message
from typing import Dict
from PyQt6.QtWidgets import QHeaderView

from can_explorer.gui.can_worker import CanWorker
from can_explorer.util.canutils import CanConfiguration

logger = logging.getLogger(__name__)


class RawCanViewerModel(QtCore.QAbstractTableModel):
    HEADER_ROWS = ('Time', 'Tx/RX', 'Message Type', 'Arbitration ID', 'DLC', 'Data Bytes')

    def __init__(self):
        super(RawCanViewerModel, self).__init__()
        self.configure()

    def configure(self):
        pass
        # self.setHeaderData(0, Qt.Orientation.Horizontal, ['timestamp', 'DLC'])

    def headerData(self, section, orientation, role, *args, **kwargs):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADER_ROWS[section]
        return super().headerData(section, orientation, role)

    def rowCount(self, parent) -> int:
        return len(self.HEADER_ROWS)

    def columnCount(self, parent):
        return len(self.HEADER_ROWS)

    def data(self, index: QModelIndex, role):
        return range(len(self.HEADER_ROWS))


class RawCanViewerView(QtWidgets.QTableView):
    data_received_signal = pyqtSignal(Message)

    def __init__(self, configuration: CanConfiguration):
        super().__init__()
        self._configuration = configuration
        self._can_handler = CanWorker(self._configuration)
        self._model = self._configure()
        self._connect_signals()

    @property
    def configuration_data(self) -> CanConfiguration:
        return self._configuration

    def start_listening(self, threadpool: QThreadPool):
        threadpool.start(self._can_handler)

    def _configure(self):
        model = RawCanViewerModel()
        self.setModel(model)
        self.horizontalHeader().setStretchLastSection(True)
        self.resizeColumnsToContents()
        return model

    def _connect_signals(self):
        pass

    @pyqtSlot(Message)
    def add_can_raw_message(self, message: Message):
        logger.info(f'Received {message=}')
        # self._model.insertRow()