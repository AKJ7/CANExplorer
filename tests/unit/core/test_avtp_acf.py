"""Tests for IEEE 1722 ACF-CAN decoding (can_explorer.core.avtp_acf).

The frame builders below assemble bytes straight from the documented IEEE 1722-2016 ACF bit layout (independently
of the parser), so decoding them back is a genuine round-trip check of the field offsets.
"""

import struct

from can_explorer.core import avtp_acf


def _acf_can(
    can_id,
    data,
    *,
    brief=False,
    extended=False,
    remote=False,
    fd=False,
    brs=False,
    esi=False,
    bus_id=0,
    mtv=False,
    timestamp=0,
):
    """Build one ACF_CAN / ACF_CAN_BRIEF message from the spec layout."""
    header_len = 8 if brief else 16
    pad = (4 - (len(data) % 4)) % 4
    message_len = header_len + len(data) + pad
    length_quadlets = message_len // 4
    acf_type = avtp_acf.ACF_CAN_BRIEF if brief else avtp_acf.ACF_CAN
    out = bytearray(message_len)
    out[0] = (acf_type << 1) | (length_quadlets >> 8)
    out[1] = length_quadlets & 0xFF
    out[2] = (pad << 6) | (mtv << 5) | (remote << 4) | (extended << 3) | (brs << 2) | (fd << 1) | esi
    out[3] = bus_id & 0x1F
    if brief:
        struct.pack_into('>I', out, 4, can_id & 0x1FFFFFFF)
    else:
        struct.pack_into('>Q', out, 4, timestamp)
        struct.pack_into('>I', out, 12, can_id & 0x1FFFFFFF)
    out[header_len : header_len + len(data)] = data
    return bytes(out)


def _acf_lin(lin_id, data, *, bus_id=0, mtv=False, timestamp=0):
    """Build an ACF_LIN message from the IEEE 1722-2016 §9.4.5 layout."""
    pad = (4 - ((12 + len(data)) % 4)) % 4
    out = bytearray(12 + len(data) + pad)
    length_quadlets = len(out) // 4
    out[0] = (avtp_acf.ACF_LIN << 1) | (length_quadlets >> 8)
    out[1] = length_quadlets & 0xFF
    out[2] = (pad << 6) | (mtv << 5) | (bus_id & 0x1F)
    out[3] = lin_id & 0xFF
    if mtv:
        struct.pack_into('>Q', out, 4, timestamp)
    out[12 : 12 + len(data)] = data
    return bytes(out)


def _acf_flexray(frame_id, cycle, data, *, channel=1, bus_id=0):
    """Build an ACF_FlexRay message from the IEEE 1722-2016 §9.4.2 layout."""
    pad = (4 - ((16 + len(data)) % 4)) % 4
    out = bytearray(16 + len(data) + pad)
    length_quadlets = len(out) // 4
    out[0] = (avtp_acf.ACF_FLEXRAY << 1) | (length_quadlets >> 8)
    out[1] = length_quadlets & 0xFF
    out[2] = (pad << 6) | (bus_id & 0x1F)
    out[3] = (channel << 4) | 0x01  # chan, and nfi=1 (not a null frame)
    struct.pack_into('>I', out, 12, ((frame_id & 0x7FF) << 21) | (cycle & 0x3F))
    out[16 : 16 + len(data)] = data
    return bytes(out)


def _acf_other(acf_type, quadlets=2):
    out = bytearray(quadlets * 4)
    out[0] = (acf_type << 1) | (quadlets >> 8)
    out[1] = quadlets & 0xFF
    return bytes(out)


def _tscf(*messages, sequence=0):
    body = b''.join(messages)
    header = bytearray(24)
    header[0] = avtp_acf.SUBTYPE_TSCF
    header[2] = sequence & 0xFF
    struct.pack_into('>H', header, 20, len(body))  # stream_data_length
    return bytes(header) + body


def _ntscf(*messages, sequence=0):
    body = b''.join(messages)
    header = bytearray(12)
    header[0] = avtp_acf.SUBTYPE_NTSCF
    header[1] = (len(body) >> 8) & 0x07  # ntscf_data_length high bits (version 0 in bits 9-11)
    header[2] = len(body) & 0xFF
    header[3] = sequence & 0xFF
    return bytes(header) + body


def test_classic_can_over_tscf():
    frame_bytes = _tscf(_acf_can(0x123, bytes(range(8)), bus_id=3))
    (frame,) = avtp_acf.decode_can_frames(frame_bytes)
    assert frame.can_id == 0x123
    assert frame.data == bytes(range(8))
    assert not frame.extended and not frame.fd and not frame.remote
    assert frame.bus_id == 3 and not frame.brief and frame.timestamp is None


def test_extended_can_fd_over_tscf_with_timestamp():
    payload = bytes(range(64))  # Full CAN FD payload
    frame_bytes = _tscf(
        _acf_can(0x1ABCDEF, payload, extended=True, fd=True, brs=True, esi=True, mtv=True, timestamp=42)
    )
    (frame,) = avtp_acf.decode_can_frames(frame_bytes)
    assert frame.can_id == 0x1ABCDEF and frame.extended
    assert frame.fd and frame.bitrate_switch and frame.error_state
    assert frame.data == payload and frame.timestamp == 42


def test_can_brief_over_ntscf_has_no_timestamp():
    frame_bytes = _ntscf(_acf_can(0x7DF, b'\x01\x02\x03', brief=True, bus_id=1))
    (frame,) = avtp_acf.decode_can_frames(frame_bytes)
    assert frame.can_id == 0x7DF and frame.brief and frame.timestamp is None
    assert frame.data == b'\x01\x02\x03' and frame.bus_id == 1  # 3 bytes + 1 pad byte stripped


def test_remote_frame_flag():
    (frame,) = avtp_acf.decode_can_frames(_tscf(_acf_can(0x100, b'', remote=True)))
    assert frame.remote and frame.data == b''


def test_standard_id_is_masked_when_not_extended():
    # An 11-bit frame must not read extra identifier bits even if the field holds more.
    (frame,) = avtp_acf.decode_can_frames(_tscf(_acf_can(0x7FF, b'\xaa', extended=False)))
    assert frame.can_id == 0x7FF and not frame.extended


def test_multiple_acf_messages_in_one_frame():
    frames = avtp_acf.decode_can_frames(
        _tscf(
            _acf_can(0x100, b'\x11\x22', bus_id=0),
            _acf_can(0x200, bytes(range(8)), bus_id=1),
            _acf_can(0x1FFFFFFF, b'\xde\xad', extended=True, bus_id=2),
        )
    )
    assert [f.can_id for f in frames] == [0x100, 0x200, 0x1FFFFFFF]
    assert [f.bus_id for f in frames] == [0, 1, 2]
    assert frames[1].data == bytes(range(8))


def test_padding_is_stripped_for_various_lengths():
    for length in range(0, 12):
        data = bytes(range(length))
        (frame,) = avtp_acf.decode_can_frames(_tscf(_acf_can(0x321, data)))
        assert frame.data == data, length


def test_non_control_subtype_yields_nothing():
    assert avtp_acf.decode_can_frames(b'\x02' + bytes(30)) == []  # 0x02 = AAF (a stream subtype)
    assert avtp_acf.decode_can_frames(b'') == []


def test_unknown_version_is_not_expanded():
    frame_bytes = bytearray(_tscf(_acf_can(0x123, b'\x01')))
    frame_bytes[1] = (frame_bytes[1] & ~0x70) | (1 << 4)  # version = 1
    assert avtp_acf.decode_can_frames(bytes(frame_bytes)) == []


def test_non_can_acf_messages_are_ignored():
    other = bytearray(8)  # An ACF message of some other type (0x7F), 2 quadlets
    other[0] = (0x7F << 1) | 0
    other[1] = 2
    frames = avtp_acf.decode_can_frames(_tscf(bytes(other), _acf_can(0x55, b'\x01\x02')))
    assert [f.can_id for f in frames] == [0x55]


def test_to_message_maps_fields_to_python_can():
    (frame,) = avtp_acf.decode_can_frames(
        _tscf(_acf_can(0x1ABCDEF, bytes(range(8)), extended=True, fd=True, brs=True, bus_id=4))
    )
    message = frame.to_message(timestamp=1.5)
    assert message.arbitration_id == 0x1ABCDEF and message.is_extended_id
    assert message.is_fd and message.bitrate_switch
    assert message.channel == 4 and message.timestamp == 1.5
    assert bytes(message.data) == bytes(range(8)) and message.dlc == 8


def test_truncated_region_stops_cleanly():
    frame_bytes = _tscf(_acf_can(0x123, bytes(range(8))))[:-4]  # Drop the last quadlet
    # The declared stream_data_length still covers the message, but the buffer is short: decode must not raise.
    assert avtp_acf.decode_can_frames(frame_bytes) == []


def test_lin_over_ntscf():
    (frame,) = avtp_acf.decode_acf(_ntscf(_acf_lin(0x3C, b'\x11\x22\x33', bus_id=2, mtv=True, timestamp=99))).lin
    assert frame.lin_id == 0x3C and frame.frame_id == 0x3C & 0x3F
    assert frame.data == b'\x11\x22\x33' and frame.bus_id == 2 and frame.timestamp == 99


def test_flexray_over_tscf():
    (frame,) = avtp_acf.decode_acf(_tscf(_acf_flexray(0x100, 5, bytes(range(16)), channel=2, bus_id=1))).flexray
    assert frame.frame_id == 0x100 and frame.cycle == 5 and frame.channel == 2
    assert frame.bus_id == 1 and frame.data == bytes(range(16)) and not frame.null_frame


def _aaf(format_code, nsr, channels, bit_depth, data=b'', *, seq=0):
    """Build a version-0 AAF AVTPDU from the Open1722 PCM layout."""
    pdu = bytearray(24 + len(data))
    pdu[0] = avtp_acf.SUBTYPE_AAF
    pdu[2] = seq & 0xFF
    pdu[16] = format_code
    pdu[17] = ((nsr & 0x0F) << 4) | ((channels >> 8) & 0x03)  # nsr high nibble, channels high 2 bits
    pdu[18] = channels & 0xFF
    pdu[19] = bit_depth
    struct.pack_into('>H', pdu, 20, len(data))  # stream_data_length
    pdu[24:] = data
    return bytes(pdu)


def _cvf(format_code, subtype, data=b'', *, marker=False, evt=0):
    """Build a version-0 CVF AVTPDU from the Open1722 layout."""
    pdu = bytearray(24 + len(data))
    pdu[0] = avtp_acf.SUBTYPE_CVF
    pdu[16] = format_code
    pdu[17] = subtype
    struct.pack_into('>H', pdu, 20, len(data))
    pdu[22] = (0x10 if marker else 0) | (evt & 0x0F)  # M bit, evt
    pdu[24:] = data
    return bytes(pdu)


def _crf(crf_type, pull, base_frequency, interval, timestamps=()):
    """Build a version-0 CRF AVTPDU from the Open1722 layout."""
    body = b''.join(struct.pack('>Q', value) for value in timestamps)
    pdu = bytearray(20 + len(body))
    pdu[0] = avtp_acf.SUBTYPE_CRF
    pdu[3] = crf_type
    struct.pack_into('>I', pdu, 12, ((pull & 0x07) << 29) | (base_frequency & 0x1FFFFFFF))
    struct.pack_into('>H', pdu, 16, len(body))  # crf_data_length
    struct.pack_into('>H', pdu, 18, interval)
    pdu[20:] = body
    return bytes(pdu)


def test_aaf_header_fields():
    aaf = avtp_acf.parse_aaf(_aaf(0x04, 0x05, 2, 16, data=bytes(8)))  # int16, 48 kHz, 2 channels
    assert aaf is not None
    assert aaf.format_name == 'int16' and aaf.sample_rate == 48000
    assert aaf.channels == 2 and aaf.bit_depth == 16 and aaf.stream_data_length == 8
    assert avtp_acf.describe_stream(_aaf(0x04, 0x05, 2, 16)) == 'int16 48kHz 2ch 16bit'


def test_aaf_wide_channel_count_spans_two_bytes():
    aaf = avtp_acf.parse_aaf(_aaf(0x02, 0x07, 300, 32))  # int32, 96 kHz, 300 channels (10-bit field)
    assert aaf.channels == 300 and aaf.sample_rate == 96000 and aaf.format_name == 'int32'


def test_aaf_user_sample_rate_has_no_hz():
    aaf = avtp_acf.parse_aaf(_aaf(0x01, 0x00, 1, 24))  # float32, user-specified nsr
    assert aaf.sample_rate is None
    assert avtp_acf.describe_stream(_aaf(0x01, 0x00, 1, 24)) == 'float32 1ch 24bit'


def test_cvf_h264_with_marker():
    cvf = avtp_acf.parse_cvf(_cvf(avtp_acf.CVF_FORMAT_RFC, 0x01, data=b'\xde\xad', marker=True, evt=3))
    assert cvf.format_name == 'RFC' and cvf.subtype_name == 'H.264'
    assert cvf.marker and cvf.evt == 3 and cvf.stream_data_length == 2
    assert avtp_acf.describe_stream(_cvf(avtp_acf.CVF_FORMAT_RFC, 0x01, marker=True)) == 'RFC/H.264 M'


def test_crf_audio_reference_timestamps():
    crf = avtp_acf.parse_crf(_crf(0x01, 0x00, 48000, 160, timestamps=(1, 2, 3)))
    assert crf.type_name == 'audio sample' and crf.base_frequency == 48000
    assert crf.timestamp_interval == 160 and crf.timestamps == (1, 2, 3)
    assert avtp_acf.describe_stream(_crf(0x01, 0x00, 48000, 160)) == 'audio sample, base 48000 Hz, interval 160'


def test_stream_parsers_reject_wrong_subtype_and_version():
    assert avtp_acf.parse_aaf(_cvf(avtp_acf.CVF_FORMAT_RFC, 0x00)) is None  # CVF is not AAF
    assert avtp_acf.describe_stream(b'\x00' + bytes(30)) is None  # 61883/IIDC: named but not expanded
    bad_version = bytearray(_aaf(0x04, 0x05, 2, 16))
    bad_version[1] = (bad_version[1] & ~0x70) | (1 << 4)  # version = 1
    assert avtp_acf.parse_aaf(bytes(bad_version)) is None


def test_mixed_and_other_acf_types():
    contents = avtp_acf.decode_acf(
        _tscf(
            _acf_can(0x123, b'\x01\x02'),
            _acf_lin(0x10, b'\xaa'),
            _acf_flexray(0x55, 1, b'\xbb\xcc'),
            _acf_other(0x04),  # MOST — recognised but not expanded
        )
    )
    assert [f.can_id for f in contents.can] == [0x123]
    assert [f.frame_id for f in contents.lin] == [0x10 & 0x3F]
    assert [f.frame_id for f in contents.flexray] == [0x55]
    assert contents.other_types == [0x04]
    assert avtp_acf.acf_type_name(0x04) == 'MOST'
    # decode_can_frames still returns only the CAN frames.
    assert [
        f.can_id for f in avtp_acf.decode_can_frames(_tscf(_acf_can(0x123, b'\x01\x02'), _acf_lin(0x10, b'\xaa')))
    ] == [0x123]
