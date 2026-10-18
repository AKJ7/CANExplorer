import can
import pytest

from can_explorer.core.frames import clone_message, format_hex, frame_type, parse_can_id, parse_hex_bytes


@pytest.mark.parametrize(
    'text, expected',
    [
        ('', b''),
        ('01 02 ff', b'\x01\x02\xff'),
        ('0102FF', b'\x01\x02\xff'),
        ('0x0102', b'\x01\x02'),
        ('1, 2, 3', b'\x01\x02\x03'),
        ('A', b'\x0a'),
    ],
)
def test_parse_hex_bytes(text, expected):
    assert parse_hex_bytes(text) == expected


@pytest.mark.parametrize('text', ['xyz', '123', '100 02', 'GG'])
def test_parse_hex_bytes_rejects_invalid(text):
    with pytest.raises(ValueError):
        parse_hex_bytes(text)


def test_parse_can_id_limits():
    assert parse_can_id('7FF', extended=False) == 0x7FF
    assert parse_can_id('1FFFFFFF', extended=True) == 0x1FFFFFFF
    with pytest.raises(ValueError):
        parse_can_id('800', extended=False)
    with pytest.raises(ValueError):
        parse_can_id('nope', extended=True)


def test_format_hex_truncates():
    assert format_hex(bytes([1, 2, 3])) == '01 02 03'
    assert format_hex(bytes(range(10)), limit=2) == '00 01 …'


def test_frame_type_and_clone():
    msg = can.Message(arbitration_id=0x1234, is_extended_id=True, is_fd=True, data=b'\x01')
    assert frame_type(msg) == 'Ext FD'
    copy = clone_message(msg)
    assert copy is not msg and copy.equals(msg, timestamp_delta=None)
