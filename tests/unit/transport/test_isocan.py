import can
import pytest
import asyncio
import logging
from contextlib import suppress
from can_explorer.transport.isocan.isocan import IsoCanProtocol, IsoCanTransport
from can_explorer.transport.can_connection import create_can_connection, CanType
from PyQt6.QtWidgets import QApplication

logger = logging.getLogger(__name__)


async def send_message(bus: can.BusABC, data: bytes, data_queue: asyncio.Queue):
    with suppress(asyncio.CancelledError):
        counter = 0
        while True:
            counter += 1
            message = can.Message(arbitration_id=counter, data=data)
            await data_queue.put(message)
            bus.send(message)
            await asyncio.sleep(1)


def verify_message(queue: asyncio.Queue, data: bytes, addr: int):
    message: can.Message = queue.get_nowait()
    assert message.data == data
    arbitration_id = addr
    assert message.arbitration_id == arbitration_id


@pytest.mark.asyncio
async def test_creatable(qapp: QApplication, can_bus: can.BusABC):
    loop = asyncio.get_running_loop()
    protocol, transport = create_can_connection(loop, CanType.ISOCAN, lambda : IsoCanProtocol(), can_bus=can_bus)
    assert isinstance(protocol, IsoCanProtocol)
    assert isinstance(transport, IsoCanTransport)
    await asyncio.sleep(1)
    await transport.close()


@pytest.mark.asyncio
async def test_receives(qapp: QApplication, can_bus: can.BusABC):
    loop = asyncio.get_running_loop()
    data_queue = asyncio.Queue()
    send_task = loop.create_task(send_message(can_bus, data=bytes(00), data_queue=data_queue))
    protocol, transport = create_can_connection(loop, CanType.ISOCAN, lambda : IsoCanProtocol(), can_bus=can_bus)
    protocol.on_data_received.connect(lambda data, addr: verify_message(data_queue, data, addr))
    transport.connect()
    await asyncio.sleep(1.5)
    await transport.close()
    send_task.cancel()
    await send_task