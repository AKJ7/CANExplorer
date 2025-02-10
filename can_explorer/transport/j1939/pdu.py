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


@enum.unique
class PDUType(enum.Enum):
    PDU1 = enum.auto()
    PDU2 = enum.auto()


@dataclass(frozen=True, slots=True)
class PDU:
    priority: int
    extended_data_page: int
    data_page: int
    pdu_format: int
    pdu_specific_field: int
    source_address: int
    data: bytearray
    arbitration_type: ArbitrationType
    length_type: LengthType

    @classmethod
    def from_can(cls, msg: can.Message, addressing: AddressInfo) -> 'PDU':
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
        priority = 0
        source_address = 0
        extended_data_page = 0
        data_page = 0
        page_format = 0
        pdu_specific_field = 0
        data = None
        arbitration_type = ArbitrationType.from_can(msg)
        length_type = LengthType.from_can(msg)
        if msg.is_extended_id:
            priority = (msg.arbitration_id >> 19) & 0b111
            extended_data_page = (msg.arbitration_id >> 18) & 0b1
            data_page = (msg.arbitration_id >> 17) & 0b1
            page_format = (msg.arbitration_id >> 16) & 0xFF
            pdu_specific_field = (msg.arbitration_id >> 8) & 0xFF
            source_address = msg.arbitration_id & 0xFF
        else:
            priority = (msg.arbitration_id >> 8) & 0b111
            source_address = msg.arbitration_id & 0xFF
        return PDU(
            priority=priority,
            extended_data_page=extended_data_page,
            pdu_format=page_format,
            pdu_specific_field=pdu_specific_field,
            data_page=data_page,
            source_address=source_address,
            arbitration_type=arbitration_type,
            length_type=length_type,
            data=data,
        )

    @property
    def pdu_type(self) -> PDUType:
        return PDUType.PDU1 if 0 <= self.pdu_format <= 239 else PDUType.PDU2

    def is_type2(self) -> bool:
        return self.pdu_type == PDUType.PDU2

    @property
    def destination_address(self) -> Optional[int]:
        return self.pdu_specific_field if self.is_type2() else None

    def as_can(self, force_normal_addressing=False) -> can.Message:
        raise NotImplemented()
