import enum
import urllib
import urllib.parse
from functools import partial
import can_explorer.transport as tp
from can_explorer.transport.isocan.isocan import *
from can_explorer.transport.isotp.old_transport import *
from can_explorer.transport.base_protocol import BaseCanProtocol
from can_explorer.transport.j1939.j1939 import J1939Transport
from can_explorer.transport.canopen.canopen import CanOpenTransport

logger = logging.getLogger(__name__)


@enum.unique
class CanType(enum.Enum):
    ISOCAN = enum.auto()
    ISOTP = enum.auto()
    J1939 = enum.auto()
    CANOPEN = enum.auto()

    def transport(self):
        match self.value:
            case self.ISOCAN.value:
                return IsoCanTransport
            case self.ISOTP.value:
                return IsoTpTransport
            case self.J1939.value:
                return J1939Transport
            case self.CANOPEN.value:
                return CanOpenTransport
        raise ValueError(f"Unknown transport request for: {self}")


def connection_for_can(
    loop: asyncio.AbstractEventLoop,
    can_type: CanType,
    protocol_factory,
    can_bus: can.BusABC,
    *args,
    **kwargs,
):
    original_protocol = protocol_factory()
    transport = can_type.transport()(
        loop=loop, protocol=original_protocol, bus=can_bus, *args, **kwargs
    )
    protocol = transport.get_protocol()
    return protocol, transport


def create_can_connection(
    loop: asyncio.AbstractEventLoop,
    can_type: CanType,
    protocol_factory,
    can_bus: can.BusABC,
    url: Optional[str] = None,
    *args,
    **kwargs,
):
    logger.info(f"Creating can connection with: {args}, {kwargs}")
    parsed_url = urllib.parse.urlparse(url=url)
    if parsed_url.scheme == "socket":
        transport, protocol = loop.run_until_complete(
            loop.create_connection(
                protocol_factory, parsed_url.hostname, parsed_url.port
            )
        )
    else:
        transport, protocol = connection_for_can(
            loop, can_type, protocol_factory, can_bus, *args, **kwargs
        )
    return transport, protocol
