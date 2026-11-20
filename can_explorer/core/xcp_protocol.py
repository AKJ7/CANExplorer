"""XCP (ASAM MCD-1 XCP) packet identifiers, command codes, error codes, bit masks and the BUILD_CHECKSUM algorithms,
shared by the master (`core/xcp.py`), the simulated slave (`core/xcp_slave.py`) and the passive decoder.

The layouts follow XCP 1.x as implemented by the public open-source XCP stacks (Vector's XCPlite, pyxcp, openblt).
"""

import zlib
from typing import Dict

# Command codes (master -> slave)
CONNECT = 0xFF
DISCONNECT = 0xFE
GET_STATUS = 0xFD
SYNCH = 0xFC
GET_COMM_MODE_INFO = 0xFB
GET_ID = 0xFA
SET_REQUEST = 0xF9
GET_SEED = 0xF8
UNLOCK = 0xF7
SET_MTA = 0xF6
UPLOAD = 0xF5
SHORT_UPLOAD = 0xF4
BUILD_CHECKSUM = 0xF3
TRANSPORT_LAYER_CMD = 0xF2
USER_CMD = 0xF1
DOWNLOAD = 0xF0
DOWNLOAD_NEXT = 0xEF
DOWNLOAD_MAX = 0xEE
SHORT_DOWNLOAD = 0xED
MODIFY_BITS = 0xEC
SET_CAL_PAGE = 0xEB
GET_CAL_PAGE = 0xEA
GET_PAG_PROCESSOR_INFO = 0xE9
GET_SEGMENT_INFO = 0xE8
GET_PAGE_INFO = 0xE7
SET_SEGMENT_MODE = 0xE6
GET_SEGMENT_MODE = 0xE5
COPY_CAL_PAGE = 0xE4
CLEAR_DAQ_LIST = 0xE3
SET_DAQ_PTR = 0xE2
WRITE_DAQ = 0xE1
SET_DAQ_LIST_MODE = 0xE0
GET_DAQ_LIST_MODE = 0xDF
START_STOP_DAQ_LIST = 0xDE
START_STOP_SYNCH = 0xDD
GET_DAQ_CLOCK = 0xDC
READ_DAQ = 0xDB
GET_DAQ_PROCESSOR_INFO = 0xDA
GET_DAQ_RESOLUTION_INFO = 0xD9
GET_DAQ_LIST_INFO = 0xD8
GET_DAQ_EVENT_INFO = 0xD7
FREE_DAQ = 0xD6
ALLOC_DAQ = 0xD5
ALLOC_ODT = 0xD4
ALLOC_ODT_ENTRY = 0xD3

COMMAND_NAMES: Dict[int, str] = {
    value: name
    for name, value in dict(globals()).items()
    if name.isupper() and isinstance(value, int) and 0xC0 <= value <= 0xFF and not name.startswith('_')
}

# Packet identifiers (slave -> master); 0x00-0xFB are DAQ data (an ODT number)
RES = 0xFF  # Positive response
ERR = 0xFE  # Error
EV = 0xFD  # Event
SERV = 0xFC  # Service request

ERR_CMD_SYNCH = 0x00
ERR_CMD_BUSY = 0x10
ERR_DAQ_ACTIVE = 0x11
ERR_PGM_ACTIVE = 0x12
ERR_CMD_UNKNOWN = 0x20
ERR_CMD_SYNTAX = 0x21
ERR_OUT_OF_RANGE = 0x22
ERR_WRITE_PROTECTED = 0x23
ERR_ACCESS_DENIED = 0x24
ERR_ACCESS_LOCKED = 0x25
ERR_PAGE_NOT_VALID = 0x26
ERR_MODE_NOT_VALID = 0x27
ERR_SEGMENT_NOT_VALID = 0x28
ERR_SEQUENCE = 0x29
ERR_DAQ_CONFIG = 0x2A
ERR_MEMORY_OVERFLOW = 0x30
ERR_GENERIC = 0x31
ERR_VERIFY = 0x32
ERR_RESOURCE_TEMPORARY_NOT_ACCESSIBLE = 0x33

ERROR_CODES = {
    ERR_CMD_SYNCH: 'command synchronisation',
    ERR_CMD_BUSY: 'command busy',
    ERR_DAQ_ACTIVE: 'DAQ active',
    ERR_PGM_ACTIVE: 'programming active',
    ERR_CMD_UNKNOWN: 'command unknown',
    ERR_CMD_SYNTAX: 'command syntax',
    ERR_OUT_OF_RANGE: 'address out of range',
    ERR_WRITE_PROTECTED: 'write protected',
    ERR_ACCESS_DENIED: 'access denied',
    ERR_ACCESS_LOCKED: 'access locked (seed & key required)',
    ERR_PAGE_NOT_VALID: 'page not valid',
    ERR_MODE_NOT_VALID: 'page mode not valid',
    ERR_SEGMENT_NOT_VALID: 'segment not valid',
    ERR_SEQUENCE: 'sequence error',
    ERR_DAQ_CONFIG: 'DAQ configuration not valid',
    ERR_MEMORY_OVERFLOW: 'memory overflow',
    ERR_GENERIC: 'generic error',
    ERR_VERIFY: 'verify failed',
    ERR_RESOURCE_TEMPORARY_NOT_ACCESSIBLE: 'resource temporarily not accessible',
}

EVENT_CODES = {
    0x00: 'resume mode',
    0x01: 'clear DAQ',
    0x02: 'store DAQ',
    0x03: 'store calibration',
    0x05: 'command pending',
    0x06: 'DAQ overload',
    0x07: 'session terminated',
    0x08: 'time synchronisation',
    0x09: 'STIM timeout',
    0x0A: 'sleep',
    0x0B: 'wake up',
    0xFE: 'user event',
    0xFF: 'transport layer event',
}

# Resources (CONNECT RESOURCE byte, GET_STATUS protection status, GET_SEED resource)
RESOURCE_CALPAG = 0x01
RESOURCE_DAQ = 0x04
RESOURCE_STIM = 0x08
RESOURCE_PGM = 0x10
RESOURCES = {RESOURCE_CALPAG: 'CAL/PAG', RESOURCE_DAQ: 'DAQ', RESOURCE_STIM: 'STIM', RESOURCE_PGM: 'PGM'}


def resource_names(mask: int) -> str:
    return ', '.join(name for bit, name in RESOURCES.items() if mask & bit) or 'none'


# COMM_MODE_BASIC bits
COMM_BYTE_ORDER_MOTOROLA = 0x01
COMM_ADDRESS_GRANULARITY = 0x06  # 0 = byte, 1 = word, 2 = dword
COMM_SLAVE_BLOCK_MODE = 0x40
COMM_OPTIONAL = 0x80
# COMM_MODE_OPTIONAL bits (GET_COMM_MODE_INFO)
COMM_MASTER_BLOCK_MODE = 0x01
COMM_INTERLEAVED_MODE = 0x02

# GET_ID identification types
ID_ASCII = 0
ID_ASAM_MC2_FILE = 1
ID_ASAM_MC2_PATH = 2
ID_ASAM_MC2_URL = 3
ID_ASAM_MC2_UPLOAD = 4

# SET_CAL_PAGE / GET_CAL_PAGE modes
PAGE_ECU = 0x01
PAGE_XCP = 0x02
PAGE_ALL = 0x80

# DAQ: GET_DAQ_PROCESSOR_INFO DAQ_PROPERTIES bits
DAQ_DYNAMIC = 0x01
DAQ_PRESCALER_SUPPORTED = 0x02
DAQ_RESUME_SUPPORTED = 0x04
DAQ_BIT_STIM_SUPPORTED = 0x08
DAQ_TIMESTAMP_SUPPORTED = 0x10
DAQ_PID_OFF_SUPPORTED = 0x20
# DAQ_KEY_BYTE identification field type (bits 6-7)
ID_FIELD_ABSOLUTE = 0  # PID = absolute ODT number (1 byte)
ID_FIELD_RELATIVE_BYTE = 1  # Relative ODT number, absolute DAQ list number (byte)
ID_FIELD_RELATIVE_WORD = 2  # Relative ODT number, absolute DAQ list number (word)
ID_FIELD_RELATIVE_WORD_ALIGNED = 3  # Relative ODT number, fill byte, absolute DAQ list number (word)
# SET_DAQ_LIST_MODE mode bits
DAQ_MODE_ALTERNATING = 0x01
DAQ_MODE_DIRECTION_STIM = 0x02
DAQ_MODE_TIMESTAMP = 0x10
DAQ_MODE_PID_OFF = 0x20
# START_STOP_DAQ_LIST / START_STOP_SYNCH modes
DAQ_STOP, DAQ_START, DAQ_SELECT = 0, 1, 2
SYNCH_STOP_ALL, SYNCH_START_SELECTED, SYNCH_STOP_SELECTED = 0, 1, 2
# GET_DAQ_RESOLUTION_INFO TIMESTAMP_MODE: bits 0-2 the size, bit 3 fixed, bits 4-7 the unit
TIMESTAMP_FIXED = 0x08
TIMESTAMP_UNITS = {
    0: 1e-9,
    1: 1e-8,
    2: 1e-7,
    3: 1e-6,
    4: 1e-5,
    5: 1e-4,
    6: 1e-3,
    7: 1e-2,
    8: 1e-1,
    9: 1.0,
    10: 1e-12,
    11: 1e-11,
    12: 1e-10,
}

# BUILD_CHECKSUM types
CHECKSUM_ADD_11 = 0x01
CHECKSUM_ADD_12 = 0x02
CHECKSUM_ADD_14 = 0x03
CHECKSUM_ADD_22 = 0x04
CHECKSUM_ADD_24 = 0x05
CHECKSUM_ADD_44 = 0x06
CHECKSUM_CRC_16 = 0x07
CHECKSUM_CRC_16_CITT = 0x08
CHECKSUM_CRC_32 = 0x09
CHECKSUM_USER_DEFINED = 0xFF
CHECKSUM_NAMES = {
    CHECKSUM_ADD_11: 'ADD_11',
    CHECKSUM_ADD_12: 'ADD_12',
    CHECKSUM_ADD_14: 'ADD_14',
    CHECKSUM_ADD_22: 'ADD_22',
    CHECKSUM_ADD_24: 'ADD_24',
    CHECKSUM_ADD_44: 'ADD_44',
    CHECKSUM_CRC_16: 'CRC_16',
    CHECKSUM_CRC_16_CITT: 'CRC_16_CITT',
    CHECKSUM_CRC_32: 'CRC_32',
    CHECKSUM_USER_DEFINED: 'user defined',
}


def _crc16_arc(data: bytes) -> int:
    """CRC-16 (polynomial 0x8005 reflected, initial value 0): check value 0xBB3D for b'123456789'."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _crc16_ccitt(data: bytes) -> int:
    """CRC-16-CCITT (polynomial 0x1021, initial value 0xFFFF): check value 0x29B1 for b'123456789'."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def checksum(kind: int, data: bytes, byte_order: str = 'little') -> int:
    """The BUILD_CHECKSUM value of `data` for checksum type `kind`. Words and dwords of ADD_2x/ADD_44 are read in
    the slave's byte order; a trailing partial word is ignored."""
    data = bytes(data)

    def words(size: int):
        return (int.from_bytes(data[i : i + size], byte_order) for i in range(0, len(data) - size + 1, size))

    if kind == CHECKSUM_ADD_11:
        return sum(data) & 0xFF
    if kind == CHECKSUM_ADD_12:
        return sum(data) & 0xFFFF
    if kind == CHECKSUM_ADD_14:
        return sum(data) & 0xFFFFFFFF
    if kind == CHECKSUM_ADD_22:
        return sum(words(2)) & 0xFFFF
    if kind == CHECKSUM_ADD_24:
        return sum(words(2)) & 0xFFFFFFFF
    if kind == CHECKSUM_ADD_44:
        return sum(words(4)) & 0xFFFFFFFF
    if kind == CHECKSUM_CRC_16:
        return _crc16_arc(data)
    if kind == CHECKSUM_CRC_16_CITT:
        return _crc16_ccitt(data)
    if kind == CHECKSUM_CRC_32:
        return zlib.crc32(data) & 0xFFFFFFFF
    raise ValueError(f'Checksum type 0x{kind:02X} is user defined or unknown: it cannot be computed here')


def granularity_size(code: int) -> int:
    """Bytes of an address granularity / ODT entry granularity code (0/1/2/3 -> 1/2/4/8)."""
    return 1 << code
