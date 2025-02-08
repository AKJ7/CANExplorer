from schema import Schema
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


is_byte = Schema(lambda x: 0x00 <= x <= 0xFF)


@dataclass(frozen=True, slots=True)
class Bitfield:
    bits_count: int

    def validate(self, value: int) -> bool:
        return Schema(lambda x: 0x00 <= x <= ((2**self.bits_count) - 1)).validate(value)
