import can
import logging
import enum
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass

from attr.setters import frozen

logger = logging.getLogger(__name__)


class SupportedProtocols(enum.IntEnum):
    IsoCAN = enum.auto()
    fdCAN = enum.auto()
    isoTp = enum.auto()
    j1939 = enum.auto()
    canopen = enum.auto()

    def get_supported_baudrates(self) -> List[int]:
        rates = None
        match self.value:
            case self.IsoCAN:
                rates = [10000, 20000, 50000, 100000, 125000, 250000, 500000,
                         800000, 1000000]
        return rates


def get_supported_interfaces() -> List[Tuple[str]]:
    supported_interfaces = [(interface, can.interfaces.BACKENDS[interface][1]) for interface in list(can.interfaces.VALID_INTERFACES)]
    return supported_interfaces

def get_available_channels(interfaces: List[str]) -> List[Dict]:
    configs =  can.interface.detect_available_configs(interfaces)
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

