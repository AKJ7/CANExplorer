import can
import pytest
import asyncio
import logging

from can_explorer.transport.isocan.isocan import IsoCanProtocol
from can_explorer.transport.isotp.addressing import AddressInfo, TargetAddressingType
from can_explorer.transport.j1939.j1939 import J1939Transport
from can_explorer.transport.can_connection import create_can_connection, CanType
from PyQt6.QtWidgets import QApplication

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_works(qapp: QApplication, can_bus: can.BusABC):
    loop = asyncio.get_running_loop()
    addressing = AddressInfo(0x00, 0x00, 0x00, TargetAddressingType.PHYSICAL)
    protocol, transport = create_can_connection(loop, CanType.J1939, lambda : IsoCanProtocol(), can_bus=can_bus, addressing=addressing)
    await transport.connect()
    logger.warning(f'Transport: {transport}')
    protocol.on_data_received.connect(lambda data, addr: logger.info(f'Received: {data=}, {addr=}'))
    await transport.close()