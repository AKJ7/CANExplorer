import urllib
import urllib.parse
from functools import partial
from can_explorer.transport.isocan import *
from can_explorer.transport.isotp.old_transport import *
from can_explorer.transport.base_protocol import BaseCanProtocol

logger = logging.getLogger(__name__)


def connection_for_can(loop: asyncio.AbstractEventLoop, protocol_factory: BaseCanProtocol, can_bus: can.BusABC):
    # protocol = protocol_factory()
    transport = IsoCanTransport(bus=can_bus)
    protocol = transport.get_protocol()
    return protocol, transport


def create_can_connection(
    loop: asyncio.AbstractEventLoop, protocol_factory: BaseCanProtocol, url: Optional[str], *args, **kwargs
):
    logger.info(f'Creating can connection with: {args}, {kwargs}')
    parsed_url = urllib.parse.urlparse(url=url)
    bus_instance = partial(can.Bus, *args, **kwargs)
    if parsed_url.scheme == 'socket':
        transport, protocol = loop.run_until_complete(
            loop.create_connection(protocol_factory, parsed_url.hostname, parsed_url.port)
        )
    else:
        transport, protocol = connection_for_can(loop, protocol_factory, bus_instance())
    return transport, protocol
