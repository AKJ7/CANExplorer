"""IEEE 1722 AVTP Control Format (ACF) decoding, for CAN (and CAN FD) tunnelled over automotive Ethernet.

ACF messages ride in one of two AVTP control subtypes: TSCF (Time-Synchronous Control Format, subtype 0x05) and
NTSCF (Non-Time-Synchronous Control Format, subtype 0x82). Each carries one or more ACF messages, each with a 7-bit
type and a 9-bit length in quadlets (4-byte units). This module walks those messages and expands the two CAN
formats — ACF_CAN (type 0x01, a 16-byte header with a 64-bit message timestamp) and ACF_CAN_BRIEF (type 0x02, an
8-byte header without one) — into `AcfCanFrame`s, each convertible to a `can.Message` so tunnelled CAN can be fed
to the ordinary CAN views.

The byte layout follows the IEEE 1722-2016 ACF CAN PDU as implemented by the open COVESA Open1722 project
(include/avtp/acf/Can.h, CanBrief.h, AcfCommon.h, Tscf.h, Ntscf.h, CommonHeader.h); the subtype values (TSCF 0x05,
NTSCF 0x82) and the CAN/CAN-BRIEF type codes (0x01/0x02) are cross-checked against AUTOSAR's IEEE1722 specification.
IEEE 1722 itself is paywalled, so only what those open sources pin down is expanded: the version-0 TSCF/NTSCF
headers and the CAN/CAN-BRIEF messages. Control frames of another version or ACF type are recognised and labelled
but not expanded, rather than guessing a layout.

The same module also decodes the version-0 AVTP stream formats whose headers Open1722 defines — AAF (audio),
CVF (compressed video) and CRF (clock reference) — into their header fields (see `parse_aaf`/`parse_cvf`/
`parse_crf` and the `describe_stream` summary). Later header versions and the media payload itself are left
untouched, keeping to the same honesty rule.
"""

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

import can

# AVTP control subtypes that carry ACF messages (the AVTP subtype is the first byte of the AVTPDU).
SUBTYPE_TSCF = 0x05
SUBTYPE_NTSCF = 0x82

# ACF message types (IEEE 1722-2016 / AUTOSAR; Open1722 AcfCommon.h).
ACF_FLEXRAY = 0x00
ACF_CAN = 0x01
ACF_CAN_BRIEF = 0x02
ACF_LIN = 0x03

# Every standardised ACF message type, so a carried message is at least named even when it is not expanded.
ACF_TYPE_NAMES = {
    0x00: 'FlexRay',
    0x01: 'CAN',
    0x02: 'CAN brief',
    0x03: 'LIN',
    0x04: 'MOST',
    0x05: 'GPC',
    0x06: 'Serial',
    0x07: 'Parallel',
    0x08: 'Sensor',
    0x09: 'Sensor brief',
    0x0A: 'AECP',
    0x0B: 'Ancillary',
    0x0C: 'GISF',
    0x0D: 'Byte bus',
    0x0E: 'Byte bus brief',
    0x0F: 'I2C',
    0x10: 'I2C brief',
    0x11: 'CAN XL',
    0x12: 'CAN XL brief',
    0x21: 'CAN v2',
    0x22: 'CAN brief v2',
    0x23: 'LIN v2',
    0x76: 'Checksum',
    0x77: 'CRC',
}

# Version-0 carrier header lengths, in bytes, from the subtype byte to the first ACF message.
_TSCF_HEADER_LEN_V0 = 24
_NTSCF_HEADER_LEN_V0 = 12

_CAN_HEADER_LEN = 16  # ACF_CAN: flags/bus-id quadlet, 64-bit timestamp, identifier quadlet
_CAN_BRIEF_HEADER_LEN = 8  # ACF_CAN_BRIEF: flags/bus-id quadlet, identifier quadlet (no timestamp)
_LIN_HEADER_LEN = 12  # ACF_LIN: flags/bus-id/identifier quadlet, 64-bit timestamp (IEEE 1722-2016 §9.4.5)
_FLEXRAY_HEADER_LEN = 16  # ACF_FlexRay: flags quadlet, 64-bit timestamp, frame-id/cycle quadlet (§9.4.2)

_CAN_ID_MASK = 0x1FFFFFFF  # 29-bit identifier field
_STANDARD_ID_MASK = 0x7FF  # 11-bit identifier


def acf_type_name(acf_type: int) -> str:
    return ACF_TYPE_NAMES.get(acf_type, f'type 0x{acf_type:02X}')


@dataclass(frozen=True)
class AcfCanFrame:
    """One CAN or CAN FD frame carried in an ACF message."""

    can_id: int
    data: bytes
    extended: bool  # EFF: 29-bit identifier
    remote: bool  # RTR: remote request (classic CAN only)
    fd: bool  # FDF: CAN FD frame
    bitrate_switch: bool  # BRS: FD data phase at the higher bit rate
    error_state: bool  # ESI: the transmitter is error-passive
    bus_id: int  # ACF can_bus_id (0-31): which CAN bus the frame came from
    timestamp: Optional[int]  # The 64-bit ACF message timestamp (gPTP ns), or None when not marked valid
    brief: bool  # Carried in ACF_CAN_BRIEF (no per-message timestamp)

    def to_message(self, timestamp: float = 0.0) -> can.Message:
        """A python-can message for the CAN views. `timestamp` is the capture time in seconds (the ACF message
        timestamp is a gPTP value in a different time base, kept separately in `self.timestamp`)."""
        return can.Message(
            timestamp=timestamp,
            arbitration_id=self.can_id,
            is_extended_id=self.extended,
            is_remote_frame=self.remote,
            is_fd=self.fd,
            bitrate_switch=self.bitrate_switch,
            error_state_indicator=self.error_state,
            channel=self.bus_id,
            data=self.data,
            dlc=len(self.data),
        )


def _version(avtpdu: bytes) -> int:
    """The AVTP version (bits 9-11 of the common header), 0 for IEEE 1722-2016."""
    return (avtpdu[1] >> 4) & 0x07


def is_control_subtype(subtype: int) -> bool:
    return subtype in (SUBTYPE_TSCF, SUBTYPE_NTSCF)


def acf_region(avtpdu: bytes) -> Optional[bytes]:
    """The bytes holding the ACF messages inside a TSCF/NTSCF AVTPDU (which starts at the subtype byte), bounded by
    the carrier's data-length field, or None when it is not a version-0 TSCF/NTSCF frame that can be located."""
    if len(avtpdu) < 2:
        return None
    subtype = avtpdu[0]
    if _version(avtpdu) != 0:
        return None  # Only the version-0 headers have a pinned-down layout
    if subtype == SUBTYPE_TSCF:
        header_len = _TSCF_HEADER_LEN_V0
        if len(avtpdu) < header_len:
            return None
        data_length = struct.unpack_from('>H', avtpdu, 20)[0]  # stream_data_length, octets
    elif subtype == SUBTYPE_NTSCF:
        header_len = _NTSCF_HEADER_LEN_V0
        if len(avtpdu) < header_len:
            return None
        data_length = ((avtpdu[1] & 0x07) << 8) | avtpdu[2]  # ntscf_data_length, 11 bits, octets
    else:
        return None
    end = min(len(avtpdu), header_len + data_length)
    return avtpdu[header_len:end]


def iter_acf_messages(region: bytes):
    """Yield (acf_msg_type, message_bytes) for each ACF message in a region, using each message's own length."""
    position = 0
    while position + 2 <= len(region):
        acf_msg_type = region[position] >> 1  # 7 bits
        length_quadlets = ((region[position] & 0x01) << 8) | region[position + 1]  # 9 bits, in quadlets
        message_length = length_quadlets * 4
        if message_length < 4 or position + message_length > len(region):
            break  # Zero length would not advance; an over-long length means a truncated or malformed frame
        yield acf_msg_type, region[position : position + message_length]
        position += message_length


def parse_acf_can(message: bytes, brief: bool) -> Optional[AcfCanFrame]:
    """Expand one ACF_CAN or ACF_CAN_BRIEF message into an AcfCanFrame, or None when it is too short."""
    header_len = _CAN_BRIEF_HEADER_LEN if brief else _CAN_HEADER_LEN
    if len(message) < header_len:
        return None
    flags = message[2]
    pad = (flags >> 6) & 0x03
    mtv = bool((flags >> 5) & 0x01)
    remote = bool((flags >> 4) & 0x01)
    extended = bool((flags >> 3) & 0x01)
    bitrate_switch = bool((flags >> 2) & 0x01)
    fd = bool((flags >> 1) & 0x01)
    error_state = bool(flags & 0x01)
    bus_id = message[3] & 0x1F  # can_bus_id: low 5 bits
    if brief:
        timestamp = None
        identifier = struct.unpack_from('>I', message, 4)[0] & _CAN_ID_MASK
    else:
        timestamp = struct.unpack_from('>Q', message, 4)[0] if mtv else None
        identifier = struct.unpack_from('>I', message, 12)[0] & _CAN_ID_MASK
    can_id = identifier if extended else identifier & _STANDARD_ID_MASK
    # The CAN payload fills the message after the header, less the trailing padding the quadlet alignment added.
    data = message[header_len : len(message) - pad] if pad <= len(message) - header_len else message[header_len:]
    return AcfCanFrame(
        can_id=can_id,
        data=bytes(data),
        extended=extended,
        remote=remote,
        fd=fd,
        bitrate_switch=bitrate_switch,
        error_state=error_state,
        bus_id=bus_id,
        timestamp=timestamp,
        brief=brief,
    )


def _payload(message: bytes, header_len: int, pad: int) -> bytes:
    """The tunnelled payload after the ACF header, less the trailing padding the quadlet alignment added."""
    if pad <= len(message) - header_len:
        return bytes(message[header_len : len(message) - pad])
    return bytes(message[header_len:])


@dataclass(frozen=True)
class AcfLinFrame:
    """One LIN frame carried in an ACF_LIN message (IEEE 1722-2016 §9.4.5)."""

    lin_id: int  # Full LIN identifier byte (the low 6 bits are the frame id)
    data: bytes
    bus_id: int  # lin_bus_id (0-31)
    timestamp: Optional[int]  # 64-bit ACF message timestamp (gPTP ns), or None

    @property
    def frame_id(self) -> int:
        return self.lin_id & 0x3F


@dataclass(frozen=True)
class AcfFlexRayFrame:
    """One FlexRay frame carried in an ACF_FlexRay message (IEEE 1722-2016 §9.4.2)."""

    frame_id: int  # 11-bit FlexRay slot id
    cycle: int  # 6-bit cycle count
    channel: int  # chan field (bit 0: channel A, bit 1: channel B)
    data: bytes
    bus_id: int  # fr_bus_id (0-31)
    startup: bool  # STR: startup frame
    sync: bool  # SYN: sync frame
    preamble: bool  # PRE: payload preamble indicator
    null_frame: bool  # NFI set to 0 marks a null frame; stored as "is a null frame"
    timestamp: Optional[int]


def parse_acf_lin(message: bytes) -> Optional[AcfLinFrame]:
    if len(message) < _LIN_HEADER_LEN:
        return None
    flags = message[2]
    pad = (flags >> 6) & 0x03
    mtv = bool((flags >> 5) & 0x01)
    bus_id = flags & 0x1F  # lin_bus_id: low 5 bits of the flags byte
    return AcfLinFrame(
        lin_id=message[3],
        data=_payload(message, _LIN_HEADER_LEN, pad),
        bus_id=bus_id,
        timestamp=struct.unpack_from('>Q', message, 4)[0] if mtv else None,
    )


def parse_acf_flexray(message: bytes) -> Optional[AcfFlexRayFrame]:
    if len(message) < _FLEXRAY_HEADER_LEN:
        return None
    flags = message[2]
    pad = (flags >> 6) & 0x03
    mtv = bool((flags >> 5) & 0x01)
    bus_id = flags & 0x1F
    indicators = message[3]  # reserved(2) | chan(2) | str | syn | pre | nfi
    identifier = struct.unpack_from('>I', message, 12)[0]  # fr_frame_id(11) | reserved(15) | cycle(6)
    return AcfFlexRayFrame(
        frame_id=(identifier >> 21) & 0x7FF,
        cycle=identifier & 0x3F,
        channel=(indicators >> 4) & 0x03,
        data=_payload(message, _FLEXRAY_HEADER_LEN, pad),
        bus_id=bus_id,
        startup=bool((indicators >> 3) & 0x01),
        sync=bool((indicators >> 2) & 0x01),
        preamble=bool((indicators >> 1) & 0x01),
        null_frame=not (indicators & 0x01),  # NFI == 0 means a null frame
        timestamp=struct.unpack_from('>Q', message, 4)[0] if mtv else None,
    )


@dataclass
class AcfContents:
    """Everything decoded from the ACF messages of one TSCF/NTSCF AVTPDU."""

    can: List[AcfCanFrame]
    lin: List[AcfLinFrame]
    flexray: List[AcfFlexRayFrame]
    other_types: List[int]  # ACF message types present but not expanded (named via acf_type_name)

    def __bool__(self) -> bool:
        return bool(self.can or self.lin or self.flexray or self.other_types)


def decode_acf(avtpdu: bytes) -> Optional[AcfContents]:
    """Decode the ACF messages of a TSCF/NTSCF AVTPDU (starting at the subtype byte): CAN, LIN and FlexRay frames
    are expanded, other message types are recorded by their type code. None when it is not such a control frame."""
    region = acf_region(avtpdu)
    if region is None:
        return None
    contents = AcfContents([], [], [], [])
    for acf_msg_type, message in iter_acf_messages(region):
        if acf_msg_type in (ACF_CAN, ACF_CAN_BRIEF):
            frame = parse_acf_can(message, brief=acf_msg_type == ACF_CAN_BRIEF)
            if frame is not None:
                contents.can.append(frame)
        elif acf_msg_type == ACF_LIN:
            lin = parse_acf_lin(message)
            if lin is not None:
                contents.lin.append(lin)
        elif acf_msg_type == ACF_FLEXRAY:
            flexray = parse_acf_flexray(message)
            if flexray is not None:
                contents.flexray.append(flexray)
        else:
            contents.other_types.append(acf_msg_type)
    return contents


def decode_can_frames(avtpdu: bytes) -> List[AcfCanFrame]:
    """Every CAN/CAN-BRIEF frame tunnelled in a TSCF/NTSCF AVTPDU. Empty when the frame is not a control frame, is
    another version, or carries no CAN ACF messages."""
    contents = decode_acf(avtpdu)
    return contents.can if contents is not None else []


# --- AVTP stream formats (version-0 common stream header; AAF / CVF / CRF, from Open1722) ---

SUBTYPE_61883 = 0x00  # IEC 61883 / IIDC (named but not expanded here)
SUBTYPE_AAF = 0x02  # AVTP Audio Format
SUBTYPE_CVF = 0x03  # Compressed Video Format
SUBTYPE_CRF = 0x04  # Clock Reference Format

_CSH_LEN_V0 = 24  # Common stream header length, version 0 (AAF/CVF share it)
_CRF_HEADER_LEN_V0 = 20  # CRF uses the shorter alternative header

# AAF (Open1722 aaf/Aaf.h, aaf/Pcm.h)
AAF_FORMATS = {0x00: 'user', 0x01: 'float32', 0x02: 'int32', 0x03: 'int24', 0x04: 'int16', 0x05: 'AES3-32'}
AAF_SAMPLE_RATES = {  # nsr code -> Hz (0x00 user-specified, others reserved)
    0x01: 8000,
    0x02: 16000,
    0x03: 32000,
    0x04: 44100,
    0x05: 48000,
    0x06: 88200,
    0x07: 96000,
    0x08: 176400,
    0x09: 192000,
    0x0A: 24000,
}

# CVF (Open1722 cvf/Cvf.h)
CVF_FORMAT_RFC = 0x02
CVF_FORMATS = {CVF_FORMAT_RFC: 'RFC'}
CVF_RFC_SUBTYPES = {0x00: 'MJPEG', 0x01: 'H.264', 0x02: 'JPEG2000', 0x03: 'H.265'}

# CRF (Open1722 Crf.h)
CRF_TYPES = {0x00: 'user', 0x01: 'audio sample', 0x02: 'video frame', 0x03: 'video line', 0x04: 'machine cycle'}
CRF_PULLS = {0x00: '×1', 0x01: '×1/1.001', 0x02: '×1.001', 0x03: '×24/25', 0x04: '×25/24', 0x05: '×1/8'}


@dataclass(frozen=True)
class AafStream:
    """The version-0 AAF (audio) common-stream header fields."""

    format: int
    format_name: str
    nsr: int  # Nominal sample rate code
    sample_rate: Optional[int]  # Hz, or None for user-specified / reserved codes
    channels: int
    bit_depth: int
    stream_data_length: int


@dataclass(frozen=True)
class CvfStream:
    """The version-0 CVF (compressed video) common-stream header fields."""

    format: int
    format_name: str
    format_subtype: int
    subtype_name: str
    marker: bool  # M bit: end of a video frame
    evt: int
    stream_data_length: int


@dataclass(frozen=True)
class CrfStream:
    """The version-0 CRF (clock reference) header fields, with the 64-bit reference timestamps it carries."""

    type: int
    type_name: str
    pull: int
    pull_name: str
    base_frequency: int  # Nominal media clock frequency, Hz
    crf_data_length: int
    timestamp_interval: int  # Events between successive timestamps
    timestamps: Tuple[int, ...]


def parse_aaf(avtpdu: bytes) -> Optional[AafStream]:
    """Expand a version-0 AAF AVTPDU (starting at the subtype byte), or None when it is not one / is too short."""
    if len(avtpdu) < _CSH_LEN_V0 or avtpdu[0] != SUBTYPE_AAF or _version(avtpdu) != 0:
        return None
    fmt = avtpdu[16]
    nsr = (avtpdu[17] >> 4) & 0x0F
    channels = ((avtpdu[17] & 0x03) << 8) | avtpdu[18]  # channels_per_frame: 10 bits
    return AafStream(
        format=fmt,
        format_name=AAF_FORMATS.get(fmt, f'0x{fmt:02X}'),
        nsr=nsr,
        sample_rate=AAF_SAMPLE_RATES.get(nsr),
        channels=channels,
        bit_depth=avtpdu[19],
        stream_data_length=struct.unpack_from('>H', avtpdu, 20)[0],
    )


def parse_cvf(avtpdu: bytes) -> Optional[CvfStream]:
    """Expand a version-0 CVF AVTPDU (starting at the subtype byte), or None when it is not one / is too short."""
    if len(avtpdu) < _CSH_LEN_V0 or avtpdu[0] != SUBTYPE_CVF or _version(avtpdu) != 0:
        return None
    fmt = avtpdu[16]
    subtype = avtpdu[17]
    flags = avtpdu[22]
    return CvfStream(
        format=fmt,
        format_name=CVF_FORMATS.get(fmt, f'0x{fmt:02X}'),
        format_subtype=subtype,
        subtype_name=CVF_RFC_SUBTYPES.get(subtype, f'0x{subtype:02X}') if fmt == CVF_FORMAT_RFC else '',
        marker=bool(flags & 0x10),  # M bit (bit 3 from the MSB of byte 22)
        evt=flags & 0x0F,
        stream_data_length=struct.unpack_from('>H', avtpdu, 20)[0],
    )


def parse_crf(avtpdu: bytes) -> Optional[CrfStream]:
    """Expand a version-0 CRF AVTPDU (starting at the subtype byte), or None when it is not one / is too short."""
    if len(avtpdu) < _CRF_HEADER_LEN_V0 or avtpdu[0] != SUBTYPE_CRF or _version(avtpdu) != 0:
        return None
    crf_type = avtpdu[3]
    pull_and_frequency = struct.unpack_from('>I', avtpdu, 12)[0]  # pull(3) | base_frequency(29)
    crf_data_length = struct.unpack_from('>H', avtpdu, 16)[0]
    timestamps = []
    for index in range(crf_data_length // 8):  # Each CRF timestamp is 8 octets
        offset = _CRF_HEADER_LEN_V0 + index * 8
        if offset + 8 > len(avtpdu):
            break
        timestamps.append(struct.unpack_from('>Q', avtpdu, offset)[0])
    pull = (pull_and_frequency >> 29) & 0x07
    return CrfStream(
        type=crf_type,
        type_name=CRF_TYPES.get(crf_type, f'0x{crf_type:02X}'),
        pull=pull,
        pull_name=CRF_PULLS.get(pull, f'pull {pull}'),
        base_frequency=pull_and_frequency & 0x1FFFFFFF,
        crf_data_length=crf_data_length,
        timestamp_interval=struct.unpack_from('>H', avtpdu, 18)[0],
        timestamps=tuple(timestamps),
    )


def _khz(hertz: int) -> str:
    return f'{hertz / 1000:g}kHz'


def describe_stream(avtpdu: bytes) -> Optional[str]:
    """A short detail string for an AAF/CVF/CRF stream frame (e.g. 'int16 48kHz 2ch 16bit'), or None when the
    frame is not a recognised version-0 stream format."""
    if not avtpdu:
        return None
    subtype = avtpdu[0]
    if subtype == SUBTYPE_AAF:
        aaf = parse_aaf(avtpdu)
        if aaf is None:
            return None
        parts = [aaf.format_name]
        if aaf.sample_rate:
            parts.append(_khz(aaf.sample_rate))
        parts.append(f'{aaf.channels}ch')
        parts.append(f'{aaf.bit_depth}bit')
        return ' '.join(parts)
    if subtype == SUBTYPE_CVF:
        cvf = parse_cvf(avtpdu)
        if cvf is None:
            return None
        detail = f'{cvf.format_name}/{cvf.subtype_name}' if cvf.subtype_name else cvf.format_name
        return f'{detail} M' if cvf.marker else detail
    if subtype == SUBTYPE_CRF:
        crf = parse_crf(avtpdu)
        if crf is None:
            return None
        return f'{crf.type_name}, base {crf.base_frequency} Hz, interval {crf.timestamp_interval}'
    return None
