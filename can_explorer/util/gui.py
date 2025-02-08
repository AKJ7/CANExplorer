import logging
from os import PathLike
from pathlib import Path

logger = logging.getLogger(__name__)


def get_res_path(path: PathLike | str) -> str:
    path = Path(".").absolute() / "can_explorer" / "gui" / "qt" / path
    logger.info(f"Requested path: {path=}")
    return path
