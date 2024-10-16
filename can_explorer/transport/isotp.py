import logging
import asyncio
from dataclasses import dataclass
from typing import Optional
import time
import can

logger = logging.getLogger(__name__)

@dataclass(slots=True)
class Timer:
    start: Optional[int] = 0
    timeout: Optional[int] = 0
    timeout_event = asyncio.Future()

    @staticmethod
    def now() -> int:
        return time.perf_counter_ns()
    
    def run(self, start: Optional[int]):
        self.start = start or self.now()

    async def process(self):
        async with asyncio.Timeout(self.timeout / 1_000_000) as tm:
            await asyncio.sleep(self.timeout / 1E6)

    def stop(self):
        self.start = None


class IsoTpCanProtocol(asyncio.Protocol):

    __slot__ = ('_transport', '_on_con_lost', '_data_received_queue', '_error_queue')
    
    def __init__(self, on_con_lost) -> None:
        self._transport = None
        self._on_con_lost = on_con_lost
        self._data_received_queue = asyncio.Queue()
        self._error_queue = asyncio.Queue()
        super().__init__()
    
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        logger.info('Connection made')
        return super().connection_made(transport)

    def data_received(self, data: bytes) -> None:
        logger.info(f'Received: {data}')
        return super().data_received(data)    

    def connection_lost(self, exc: Exception | None) -> None:
        self._on_con_lost.set_result(True)
        return super().connection_lost(exc)


class IsoTpTransport(asyncio.Transport):
    __slot__ = ('_bus', '_protocol', '_loop', '_parsing_task')
    
    def __init__(self, bus: can.BusABC) -> None: 
        self._bus = bus
        self._protocol = IsoTpCanProtocol(lambda x: logger.info(x))
        self._loop = asyncio.get_running_loop()
        self._start_message_polling()
        super().__init__()
        
    def is_reading(self) -> bool:
        return self._bus.state == can.BusState.ACTIVE

    # def write(self, data: bytes | bytearray | memoryview[int], arbitration_id: int) -> None:
    def write(self, data: bytes | bytearray, arbitration_id: int) -> None:
        message = can.Message(data=data, arbitration_id=arbitration_id)
        self._bus.send(message)

    def pause_reading(self) -> None:
        if self._parsing_task is not None:
            self._parsing_task.cancel()
            self._parsing_task = None
        else:
            logger.warning(f'Parsing task not present!')

    def close(self) -> None:
        self._bus.shutdown()
        return super().close()

    async def _parse_can_frames(self) -> None:
        logger.info(f'Waiting for CAN messages')
        while True:
            message = self._bus.recv()
            self._protocol.data_received(message)
        
    def _start_message_polling(self):
        self._parsing_task = asyncio.create_task(self._parse_can_frames())
