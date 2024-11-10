from schema import Schema
import logging

logger = logging.getLogger(__name__)


is_byte = Schema(lambda x: 0x00 <= x <= 0xFF)
