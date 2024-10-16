import logging
from os import PathLike
from pathlib import Path

logger = logging.getLogger(__name__)


def get_res_path(path: PathLike | str) -> Path:
    return Path('.').absolute() / 'gui' / 'qt' / path
