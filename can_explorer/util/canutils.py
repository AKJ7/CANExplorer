from typing import Dict, List, Optional, Tuple

import can

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

CLASSIC_CAN_BITRATES = (10_000, 20_000, 50_000, 100_000, 125_000, 250_000, 500_000, 800_000, 1_000_000)


def get_supported_interfaces() -> List[Tuple[str, str]]:
    """(interface name, bus class name) for every interface python-can knows."""
    return [(interface, can.interfaces.BACKENDS[interface][1]) for interface in can.interfaces.VALID_INTERFACES]


def get_available_channels(interfaces: List[str]) -> List[Dict]:
    configs = can.interface.detect_available_configs(interfaces)
    logger.info(f'{configs=}')
    return configs


def get_interface_name(target_class_name: str) -> Optional[str]:
    for interface_name, (_module_name, class_name) in can.interfaces.BACKENDS.items():
        if class_name == target_class_name:
            return interface_name
    return None
