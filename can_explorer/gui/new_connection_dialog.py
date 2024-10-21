import logging

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QDialog, QComboBox, QLineEdit, QCheckBox
from PyQt6.uic import loadUi
from matplotlib.pyplot import connect

from can_explorer.util.canutils import CanConfiguration
from can_explorer.util.gui import get_res_path
from can_explorer.util import canutils

logger = logging.getLogger(__name__)


class NewConnectionDialog(QDialog):
    on_connection_added = pyqtSignal(CanConfiguration)

    def __init__(self, parent=None, app=None):
        super(NewConnectionDialog, self).__init__(parent)
        self._parent = parent
        self._app = app
        ui_path = get_res_path('new_connection_dialog.ui')
        loadUi(ui_path, self)
        self._configure()
        self._connect_signals()

    def _configure(self):
        self.connection_name_box.setText('Connection 1')
        supported_bitrates = canutils.SupportedProtocols.IsoCAN.supported_bitrates
        self.bitrate_box.addItems(list(map(str, supported_bitrates)))
        supported_interfaces = canutils.get_supported_interfaces()
        self.interface_box.addItems(sorted(list([description for name, description in supported_interfaces])))
        # TODO: Extend of proper interfaces
        # available_channels = get_available_channels([name for name, description in supported_interfaces])
        available_channels = canutils.get_available_channels(['socketcan'])
        self.channel_box.addItems([available_channel['channel'] for available_channel in available_channels])
        self.protocol_box.addItems(['IsoCan'])
        logger.info(f'Loaded config: {canutils.load_config()}')

    def _connect_signals(self):
        pass

    def accept(self):
        QComboBox().currentText()
        can_configuration = CanConfiguration(
            connection_name=self.connection_name_box.text(),
            bitrate=int(self.bitrate_box.currentText(), base=10),
            interface=canutils.get_interface_name(self.interface_box.currentText()),
            channel=self.channel_box.currentText(),
            protocol=self.protocol_box.currentText(),
            fd=self.flexible_data_checkbox.isChecked(),
        )
        self.on_connection_added.emit(can_configuration)
        super().accept()
