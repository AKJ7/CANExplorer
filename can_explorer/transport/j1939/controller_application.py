import asyncio
import logging
import enum
from can_explorer.transport.j1939.addressing import AddressInfo
from can_explorer.transport.j1939.pdu import PDU

logger = logging.getLogger(__name__)


class ControllerState(enum.Enum):
    READY = enum.auto()
    OPERATIONAL = enum.auto()


class ControllerApp:

    def __init__(self):
        self._state: ControllerState = ControllerState.READY

    @property
    def arbitration_id(self):
        raise NotImplementedError()

    def accepts(self, arbitration_id: int) -> bool:
        return self._state == ControllerState.OPERATIONAL and (
            self.arbitration_id == arbitration_id or self.arbitration_id == AddressInfo.GLOBAL
        )

    def handle_request(self, pdu: PDU) -> bool:
        raise NotImplementedError()
