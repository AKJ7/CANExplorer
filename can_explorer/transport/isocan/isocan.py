import can
import logging
import asyncio
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget
from can_explorer.util.worker import Worker
from typing import Tuple, Any

logger = logging.getLogger(__name__)


class IsoCanProtocol(asyncio.DatagramProtocol, QWidget):
    __slot__ = (
        "_transport",
        "_on_con_lost",
        "_data_received_queue",
        "_error_queue",
        "on_data_received",
    )
    on_data_received = pyqtSignal(bytearray, int)
    on_connection_lost = pyqtSignal(Exception)
    on_connection_made = pyqtSignal(asyncio.BaseProtocol)
    on_error_received = pyqtSignal(Exception)

    def __init__(self) -> None:
        self._transport: asyncio.Transport | None = None
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport
        self.on_connection_made.emit(transport)
        return super().connection_made(transport)

    def datagram_received(self, data: bytes | bytearray, addr: Tuple[str | Any, int]) -> None:
        logger.info(f"Received: {data=}, {addr=}")
        _, arbitration_id = addr
        self.on_data_received.emit(data, arbitration_id)

    def connection_lost(self, exc: Exception | None) -> None:
        self.on_connection_lost.emit(exc)
        return super().connection_lost(exc)

    def error_received(self, exc: Exception) -> None:
        logger.debug(f'Error received: {exc=}')
        self.on_error_received.emit(exc)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.close()


class IsoCanTransport(Worker, asyncio.DatagramTransport):
    __slot__ = ("_bus", "_protocol", "_loop", "_parsing_task")

    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: asyncio.DatagramProtocol, bus: can.BusABC) -> None:
        self._bus = bus
        self._protocol = protocol
        self._loop = loop
        self._reader = can.AsyncBufferedReader()
        self._notifier = can.Notifier([], [], loop=self._loop)
        # self._bus.set_filters()
        # filter = can.typechecking.CanFilter()
        super().__init__(self._loop)

    def is_reading(self) -> bool:
        return self._reader.buffer.empty()

    def pause_reading(self) -> None:
        # TODO: Fix to newer interface
        if self._parsing_task is not None:
            self._parsing_task.cancel()
            self._parsing_task = None
        else:
            logger.warning("Parsing task not present!")

    def connect(self, start_listening=True):
        logger.debug('Connecting')
        self._notifier.add_bus(self._bus)
        if start_listening:
            self._notifier.add_listener(self._reader)

    def listen(self):
        self._notifier.add_listener(self._reader)

    def disconnect(self):
        self._reader.stop()
        self._notifier.stop()

    def resume_reading(self) -> None:
        # TODO: Fix
        self._start_message_polling()

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol

    async def close(self) -> None:
        await self.cancel()
        self.disconnect()

    def sendto(self, data: bytes | bytearray | memoryview, addr: int | None = None) -> None:
        message = can.Message(data=data, arbitration_id=addr)
        self._bus.send(message)

    def abort(self):
        pass

    async def _run(self) -> None:
        async for message in self._reader:
            data = message.data
            address = message.arbitration_id
            self._protocol.datagram_received(data, ('', address))
