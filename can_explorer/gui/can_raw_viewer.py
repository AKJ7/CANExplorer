import logging

import can
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QModelIndex, QThreadPool
from can.message import Message
from typing import Dict, List
from PyQt6.QtWidgets import QHeaderView
import asyncio

from can_explorer.gui.can_worker import CanWorker
from can_explorer.util.canutils import CanConfiguration

logger = logging.getLogger(__name__)


class RawCanViewerModel(QtCore.QAbstractTableModel):
    HEADER_ROWS = ('Time [s]', 'Tx/RX', 'Message Type', 'Arbitration ID [hex]', 'DLC [hex]', 'Data Bytes [hex]')

    def __init__(self):
        super(RawCanViewerModel, self).__init__()
        self._data: List[can.Message] = []
        self.configure()

    def configure(self):
        pass

    def headerData(self, section, orientation, role, *args, **kwargs):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADER_ROWS[section]
        return super().headerData(section, orientation, role)

    def rowCount(self, parent) -> int:
        return len(self._data)

    def columnCount(self, parent) -> int:
        return len(self.HEADER_ROWS)

    @staticmethod
    def format_data(value):
        match value:
            case float():
                return f'{value: 8.5f}'
            case int():
                return hex(value)
            case bytearray():
                return ' '.join([f"{x:02X}" for x in value])
        return value

    def data(self, index: QModelIndex, role):
        row = index.row()
        col = index.column()
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            data = self._data[row]
            row_data = (
                data.timestamp,
                'Rx' if data.is_rx else 'Tx',
                'F' if data.is_fd else 'S',
                data.arbitration_id,
                data.dlc,
                data.data,
            )
            return self.format_data(row_data[col])
        elif role == QtCore.Qt.ItemDataRole.TextAlignmentRole:
            aligment = QtCore.Qt.AlignmentFlag
            row_pos = (
                aligment.AlignRight,
                aligment.AlignCenter,
                aligment.AlignCenter,
                aligment.AlignRight,
                aligment.AlignRight,
                aligment.AlignLeft,
            )
            return row_pos[col] | aligment.AlignVCenter

    def flags(self, index: QModelIndex):
        return QtCore.Qt.ItemFlag.ItemIsSelectable

    def insert(self, data: can.Message):
        logger.info(f'Added {data=} to container')
        self._data.append(data)
        # self.dataChanged.emit()
        # self.modelReset.emit()
        self.layoutChanged.emit()


class RawCanViewerView(QtWidgets.QTableView):
    data_received_signal = pyqtSignal(Message)

    def __init__(self, configuration: CanConfiguration):
        super().__init__()
        self._configuration = configuration
        self._model = self._configure()
        self._can_handler = CanWorker(self._configuration, lambda x: self._model.insert(x))
        self._connect_signals()

    @property
    def configuration_data(self) -> CanConfiguration:
        return self._configuration

    def start_listening(self, threadpool: QThreadPool):
        threadpool.start(self._can_handler)
        # self._can_handler.protocol.on_data_received.connect(lambda x: logger.info(f'Received CAN message: {x}'))
        logger.info('Signal connected')

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
