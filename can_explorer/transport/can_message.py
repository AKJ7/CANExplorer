import logging
import asyncio
from dataclasses import dataclass
from can_explorer.transport.isotp.addressing import AddressInfo
import can

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CanMessage:
    arbitration_id: int
    dlc: int
    data: bytearray
    is_extended_id: bool
    is_fd: bool
    DLC_MAP = list(range(0, 9))

    @classmethod
    def from_can(cls, msg: can.Message) -> 'CanMessage':
        address_info = AddressInfo()
        return cls(
            arbitration_id=msg.arbitration_id,
            dlc=msg.dlc,
            data=msg.data,
            is_extended_id=msg.is_fd,
            is_fd=msg.is_fd,
        )

    @classmethod
    def export(cls) -> can.Message:
        return can.Message()

    def decode_dlc(self, dlc: int) -> int:
        dlc_map = self.DLC_MAP
        if self.is_fd:
            dlc_map = dlc_map + [12, 16, 20, 24, 32, 48, 64]
        assert dlc < len(dlc_map), f'Given {dlc=} out of range: {dlc_map=}'
        return dlc_map[dlc]
