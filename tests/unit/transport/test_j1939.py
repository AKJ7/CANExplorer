import pytest
import asyncio
import logging
from can_explorer.transport.j1939.j1939 import J1939Transport
from can_explorer.transport.can_connection import create_can_connection, CanType
from can_explorer.transport.base_protocol import BaseCanProtocol

logger = logging.getLogger(__name__)


@pytest.fixture
def j1939_transport(event_loop, can_bus):
    protocol, transport = create_can_connection(
        event_loop, CanType.J1939, lambda: BaseCanProtocol(), can_bus, addressing=None
    )
    return transport


@pytest.mark.asyncio
async def test_works(j1939_transport: asyncio.Transport):
    transport = j1939_transport
    transport.write(bytes([1, 2, 3, 4, 5, 6]))
