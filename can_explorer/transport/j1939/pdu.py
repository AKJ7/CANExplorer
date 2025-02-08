import logging
import can
import enum
from typing import Optional
from dataclasses import dataclass
from can_explorer.util.validator import Bitfield


logger = logging.getLogger(__name__)


@enum.unique
class MessageType(enum.Enum):
    COMMAND = enum.auto()
    REQUEST = enum.auto()
    BROADCAST = enum.auto()
    ACKNOWLEDGMENT = enum.auto()
    GROUP_FUNCTION = enum.auto()


@dataclass(slots=True)
class PDU:
    priority: int
    extended_data_page: int
    data_page: int
    pdu_format: int
    pdu_specific_field: int
    source_address: int
