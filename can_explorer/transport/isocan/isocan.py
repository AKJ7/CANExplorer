from PyQt6.QtCore import pyqtSignal

import can
import logging
import asyncio
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget
from can_explorer.util.worker import Worker

logger = logging.getLogger(__name__)


class IsoCanProtocol(asyncio.Protocol, QWidget):
    __slot__ = (
        "_transport",
        "_on_con_lost",
        "_data_received_queue",
        "_error_queue",
        "on_data_received",
    )
    on_data_received = pyqtSignal(can.Message)

    def __init__(self, on_con_lost) -> None:
        super().__init__()
        self._transport = None
        self._on_con_lost = on_con_lost
        self._data_received_queue = asyncio.Queue()
        self._error_queue = asyncio.Queue()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        logger.debug(f"Connection made")
        return super().connection_made(transport)

    def data_received(self, data: bytes) -> None:
        logger.info(f"Received: {data}")
        self._data_received_queue.put_nowait(data)
        self.on_data_received.emit(data)

    def connection_lost(self, exc: Exception | None) -> None:
        self._on_con_lost.set_result(True)
        return super().connection_lost(exc)


class IsoCanTransport(Worker, asyncio.Transport):
    __slot__ = ("_bus", "_protocol", "_loop", "_parsing_task")

    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol, bus: can.BusABC) -> None:
        self._bus = bus
        self._protocol = protocol
        self._loop = loop
        self._reader = can.AsyncBufferedReader()
        self._notifier = can.Notifier(self._bus, [self._reader])
        super().__init__(self._loop)

    def is_reading(self) -> bool:
        return self._bus.state == can.BusState.ACTIVE

    def pause_reading(self) -> None:
        # TODO: Fix to newer interface
        if self._parsing_task is not None:
            self._parsing_task.cancel()
            self._parsing_task = None
        else:
            logger.warning(f"Parsing task not present!")

    def resume_reading(self) -> None:
        self._start_message_polling()

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol

    def close(self) -> None:
        self.cancel()
        self._reader.stop()
        self._notifier.stop()

    def write(self, data: bytearray, arbitration_id: int) -> None:
        message = can.Message(data=data, arbitration_id=arbitration_id)
        self._bus.send(message)

    async def _run(self) -> None:
        async for message in self._reader:
            self._protocol.data_received(message)