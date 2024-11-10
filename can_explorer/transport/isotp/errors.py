import logging
import enum
from typing import Optional

logger = logging.getLogger(__name__)


@enum.unique
class NResult(enum.IntEnum):
    N_OK = 0x00
    N_TIMEOUT_A = 0x01
    N_TIMEOUT_Bs = 0x02
    N_TIMEOUT_Cr = 0x03
    N_WRONG_SN = 0x04
    N_INVALID_FS = 0x05
    N_UNEXP_PDU = 0x06
    N_WFT_OVRN = 0x07
    N_BUFFER_OVFLW = 0x08
    N_ERROR = 0x09


class IsoTpError(Exception):
    def __init__(self, result: NResult, msg: Optional[str]):
        self._result = result
        self._msg = msg


@enum.unique
class IsoTpWarning(enum.IntEnum):
    pass


class IsoTpException(Exception):
    pass
