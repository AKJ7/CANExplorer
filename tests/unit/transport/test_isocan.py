import pytest
import asyncio
import logging
import can
from mypyc.irbuild.statement import transform_try_stmt

from can_explorer.transport.isocan.isocan import IsoCanProtocol, IsoCanTransport
from can_explorer.transport.can_connection import create_can_connection, CanType
from PyQt6.QtWidgets import QApplication

logger = logging.getLogger(__name__)


async def send_message(bus: can.BusABC):
    counter = 0
    while True:
        message = can.Message(arbitration_id=counter, data=0x00)
        bus.send(message)
        counter += 1
        logger.info(f'Sending data: {counter=}')
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_creatable(qapp: QApplication, can_bus: can.BusABC):
    loop = asyncio.get_running_loop()
    protocol, transport = create_can_connection(loop, CanType.ISOCAN,
                                                lambda : IsoCanProtocol(lambda x: logger.info(f'Received: {x=}')), can_bus=can_bus)
    assert isinstance(protocol, IsoCanProtocol)
    assert isinstance(transport, IsoCanTransport)
    transport.close()


@pytest.mark.asyncio
async def test_receives(can_bus: can.BusABC):
    loop = asyncio.get_running_loop()
    send_task = loop.create_task(send_message(can_bus))
    protocol, transport = create_can_connection(loop, CanType.ISOCAN,
                                                lambda : IsoCanProtocol(lambda x: logger.info(f'Received: {x=}')), can_bus=can_bus)
    await asyncio.sleep(2)
    send_task.cancel()
    transport.close()