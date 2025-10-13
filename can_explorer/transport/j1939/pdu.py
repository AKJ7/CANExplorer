import logging
import can
import enum
from typing import Optional
from dataclasses import dataclass
from can_explorer.util.validator import Bitfield
from can_explorer.transport.j1939.addressing import AddressInfo
from can_explorer.transport.frame_info import LengthType, ArbitrationType


logger = logging.getLogger(__name__)


@enum.unique
class MessageType(enum.Enum):
    COMMAND = enum.auto()
    REQUEST = enum.auto()
    BROADCAST = enum.auto()
    ACKNOWLEDGMENT = enum.auto()
    GROUP_FUNCTION = enum.auto()


@dataclass(frozen=True, slots=True)
class PDU:
    address_info: AddressInfo
    data: bytearray
    arbitration_type: ArbitrationType
    length_type: LengthType

    @classmethod
    def from_can(cls, msg: can.Message) -> 'PDU':
        """
        Generate a valid j1939 PDU from a CAN Frame

        It is recognized that controllers on the SAE J1939 network may support the CAN standard frame (11-bit identifier)
        message format. Though these are not compatible with the SAE J1939 message structure, to accommodate the
        coexistence of the two formats, a minimum level of definition is given. This minimum definition allows devices that use
        this format to not interfere with other devices. CAN standard frame format messages are defined to be proprietary.
        Referencing Table 1, the 11-bit identifier field is parsed as follows: the three most significant bits are used as priority bits;
        the eight least significant bits identify the source address of the PDU. Priority bits are described in Section 5.2.1. The
        source address is defined in the Source Address Table (see SAE J1939).

        Returns:
            PDU: Generated PDU. Returns None on Error
        """
        arbitration_type = ArbitrationType.from_can(msg)
        length_type = LengthType.from_can(msg)
        address_info = AddressInfo.from_can_id(msg.arbitration_id, msg.is_extended_id)
        data = msg.data
        return PDU(
            address_info=address_info,
            arbitration_type=arbitration_type,
            length_type=length_type,
            data=data,
        )

    def export(self, force_normal_addressing=False) -> can.Message:
        raise NotImplemented()

    def validate(self) -> bool:
        if not self.address_info.validate():
            return False
        return True
