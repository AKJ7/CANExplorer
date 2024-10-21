import asyncio
from can_explorer.gui.base_worker import Worker
from can_explorer.transport.can_connection import create_can_connection
from can_explorer.transport.isocan import IsoCanProtocol, IsoCanTransport
from can_explorer.util.canutils import CanConfiguration
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class CanWorker(Worker):
    def __init__(self, config: CanConfiguration, on_data_received):
        self._config = config
        self.protocol: Optional[IsoCanProtocol] = None
        self.transport: Optional[IsoCanTransport] = None
        self._on_data_received = on_data_received
        self._progress_callback = None
        self._configure()
        super().__init__(self.start_listening)

    def _configure(self):
        pass

    def start_listening(self, progress_callback):
        try:
            running_loop = None
            self._progress_callback = progress_callback
            self.protocol, self.transport = create_can_connection(
                running_loop,
                protocol_factory=None,
                url=None,
                channel=self._config.channel,
                interface=self._config.interface,
                fd=self._config.fd,
            )
            self.protocol.on_data_received.connect(self._on_data_received)
            self.transport._parse_can_frames()
        except Exception as e:
            logger.error(f'Error while listening to can frame: {e}')
            self._signals.error.emit(e)

    def send(self):
        pass
