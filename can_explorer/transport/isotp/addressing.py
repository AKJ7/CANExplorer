import logging
import asyncio
import enum

from can_explorer.util.validator import is_byte

logger = logging.getLogger(__name__)


@enum.unique
class TargetAddressingType(enum.IntEnum):
    PHYSICAL = enum.auto()  # 1 to 1 communication
    FUNCTIONAL = enum.auto()  # 1 to n communication


@enum.unique
class AddressingType(enum.IntEnum):
    NORMAL = enum.auto()
    MIXED_EXTENDED = enum.auto()


@enum.unique
class MessageType(enum.IntEnum):
    """
    The parameter Mtype shall be used to identify the type
    and range of address information parameters included
    in a service call. The intention is that users of
    this part of the ISO 15665 can extend the range
    of values by specifying other types and combinations
    of address information parameters to be used with the
    network layer specified as part of the ISO 15765.
    - If Mtype = diagnotics, the Address information
    N_AI shall consist of the parameters N_SA, N_TA and
    N_TAtype
    - If Mtype = remote diagnotics, then the address
    information N_AI shall consist of the parameters
    N_SA, N_TA, N_TAtype and N_AE
    """

    DIAGNOSTIC = enum.auto()
    REMOTE_DIAGNOSTIC = enum.auto()


class AddressInfo:
    def __init__(
        self,
        source_address: int | None,
        target_address: int | None,
        address_extension: int | None,
        target_address_type: TargetAddressingType,
        btr: bool = False,
        max_payload_length: int = 8,
        is_fd: bool = True,
        is_extended: bool = False,
        addressing_type: AddressingType = AddressingType.NORMAL,
    ):
        """
        :param source_address:
        :param target_address:
        :param address_extension:
        :param target_address_type:
        :param btr: BRS bit which is part of a CAN FD frame
        and used to determine if the data phase is to be
        transmitted at a different bit rate than the arbi-
        tration phase. The bitrate of the data phase is
        defined to be equal or higher than the arbitration
        bitrate. Bitrate switching does not influence the
        transport protocol itself. See ISO-15765-2-2016
        Page 6
        :param max_payload_length: The maximum allowed
        payload length (CAN_DL, 8...64 bytes) ISO-15765-2-2016
        Page 6
        """
        self.source_address = source_address
        self.target_address = target_address
        self.address_extension = address_extension
        self.target_address_type = target_address_type
        self.btr = btr
        self.maximum_payload_length = max_payload_length
        self.is_fd = is_fd
        self.is_extended = is_extended
        self.addressing_type = addressing_type
        is_byte.validate(source_address)
        is_byte.validate(target_address)
        is_byte.validate(address_extension)

    @property
    def is_normal_addressing(self) -> bool:
        return self.addressing_type == AddressingType.NORMAL

    @property
    def arbitration_id(self) -> int:
        return self.target_address << 4 | self.source_address
