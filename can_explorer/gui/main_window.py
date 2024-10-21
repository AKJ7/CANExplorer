import asyncio
from PyQt6.uic import loadUi
from PyQt6.QtCore import QSize, Qt, pyqtSlot, QFile, QStringEncoder, QThreadPool
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QTabWidget
from can_explorer.gui.new_connection_dialog import NewConnectionDialog
from can_explorer.util.canutils import CanConfiguration
from can_explorer.util.gui import get_res_path
from can_explorer.gui.about_dialog import AboutDialog
import signal
import logging
from can_explorer.gui.can_raw_viewer import RawCanViewerModel, RawCanViewerView
from can_explorer.transport.can_connection import create_can_connection
from can_explorer.util import canutils

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, app: QApplication):
        super().__init__()
        loadUi(get_res_path('main_window.ui'), self)
        self._app = app
        self._pool = QThreadPool.globalInstance()
        self._configure()
        self._connect_signal_slots()

    def _configure(self):
        sheet_path = get_res_path('stylesheet.qss')
        with open(sheet_path, mode='r') as sheet_file:
            sheet_content = sheet_file.read()
        assert sheet_content is not None, 'Sheet content empty!'
        self._app.setStyleSheet(str(sheet_content))
        max_thread_count = self._pool.maxThreadCount()
        logger.info(f'Using global threadpool instance with max: {max_thread_count=}')

    def _connect_signal_slots(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.actionAbout.triggered.connect(self._show_about_dialog)
        self.actionNew_Connection.triggered.connect(self._show_new_connection_dialog)
        self.connect_button.released.connect(self._connect_to_bus)

    def _show_about_dialog(self) -> int:
        dialog = AboutDialog(self, self._app)
        status = dialog.exec()
        return status

    def _show_new_connection_dialog(self) -> int:
        dialog = NewConnectionDialog(self, self._app)
        dialog.on_connection_added.connect(self._add_new_can_connection)
        status = dialog.exec()
        return status

    @pyqtSlot(CanConfiguration)
    def _add_new_can_connection(self, data: CanConfiguration):
        logger.info(f'Adding new connection: {data=}')
        can_raw_viewer = RawCanViewerView(data)
        self.tab_widget.addTab(can_raw_viewer, data.connection_name)

    def _connect_to_bus(self):
        try:
            widget = self.tab_widget.currentWidget()
            logger.info(f'Connecting to selected bus: {widget}')
            if isinstance(widget, RawCanViewerView):
                channel = widget.configuration_data.channel
                interface = canutils.get_interface_name(widget.configuration_data.interface)
                is_fd = widget.configuration_data.fd
                widget.start_listening(self._pool)
                # protocol, transport = create_can_connection(asyncio.get_running_loop(), protocol_factory=None, url=None, channel=channel, interface=interface, fd=is_fd)
                # protocol.on_data_received.connect(widget.add_can_raw_message)
                # logger.info(f'Connection to protocol: {protocol} successful')
            else:
                logger.warning(f'Connecting to an unexpected widget. Skipping ...')
        except Exception as e:
            logger.error(f'Could not connect to bus: {e}')
