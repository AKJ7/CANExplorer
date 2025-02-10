import enum
import logging
import can

logger = logging.getLogger(__name__)


@enum.unique
class ArbitrationType(enum.Enum):
    NORMAL = enum.auto()
    EXTENDED = enum.auto()

    @classmethod
    def from_can(cls, msg: can.Message) -> 'ArbitrationType':
        return cls.EXTENDED if msg.is_extended_id else cls.NORMAL


@enum.unique
class LengthType(enum.Enum):
    NORMAL = enum.auto()
    FLEXIBLE = enum.auto()

    @classmethod
    def from_can(cls, msg: can.Message) -> 'LengthType':
        return cls.FLEXIBLE if msg.is_fd else cls.NORMAL
