from os import PathLike
from pathlib import Path
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)


def get_res_path(path: PathLike | str) -> Path:
    path = Path(__file__).resolve().parent.parent / "gui" / "qt" / path
    return path
