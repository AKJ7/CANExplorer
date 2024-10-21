import can
import logging
import enum
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class SupportedProtocols(enum.IntEnum):
    IsoCAN = enum.auto()
    fdCAN = enum.auto()
    isoTp = enum.auto()
    j1939 = enum.auto()
    canopen = enum.auto()

    @property
    def supported_bitrates(self) -> List[int]:
        rates = None
        match self.value:
            case self.IsoCAN:
                rates = [10_000, 20_000, 50_000, 100_000, 125_000, 250_000, 500_000, 800_000, 1_000_000]
        return rates


def get_supported_interfaces() -> List[Tuple[str]]:
    supported_interfaces = [
        (interface, can.interfaces.BACKENDS[interface][1]) for interface in list(can.interfaces.VALID_INTERFACES)
    ]
    return supported_interfaces


def get_available_channels(interfaces: List[str]) -> List[Dict]:
    configs = can.interface.detect_available_configs(interfaces)
    logger.info(f'{configs=}')
    return configs


def load_config():
    return {}


def get_interface_name(target_class_name: str) -> Optional[str]:
    for interface_name, (module_name, class_name) in can.interfaces.BACKENDS.items():
        if class_name == target_class_name:
            return interface_name
    return None


@dataclass(frozen=True)
class CanConfiguration:
    interface: str
    connection_name: str
    bitrate: int
    channel: str
    protocol: str
    fd: bool
