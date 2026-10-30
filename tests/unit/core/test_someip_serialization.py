import struct as _struct

import pytest

from can_explorer.core.someip_serialization import (
    ArrayType,
    Scalar,
    SerializationError,
    SomeIpDescription,
    StringType,
    Struct,
    deserialize,
    flatten,
)


def test_scalars_and_byte_order():
    s = Struct(
        'S', [Scalar('a', 'uint16'), Scalar('b', 'sint8'), Scalar('c', 'uint16', 'little'), Scalar('f', 'float32')]
    )
    data = bytes([0x12, 0x34, 0xFF, 0x34, 0x12]) + _struct.pack('>f', 1.5)
    assert deserialize(data, s) == {'a': 0x1234, 'b': -1, 'c': 0x1234, 'f': 1.5}


def test_boolean_and_sint():
    s = Struct('S', [Scalar('on', 'bool'), Scalar('t', 'sint16')])
    assert deserialize(bytes([1, 0xFF, 0xF8]), s) == {'on': True, 't': -8}
    assert deserialize(bytes([0, 0x00, 0x08]), s) == {'on': False, 't': 8}


def test_fixed_and_dynamic_string():
    fixed = Struct('S', [StringType('name', 'utf-8', length_field_bits=0, fixed_length=4)])
    assert deserialize(b'ABC\x00rest', fixed) == {'name': 'ABC'}
    dynamic = Struct('S', [StringType('name', 'utf-8', length_field_bits=32), Scalar('after', 'uint8')])
    payload = (3).to_bytes(4, 'big') + b'VIN' + bytes([0x42])
    assert deserialize(payload, dynamic) == {'name': 'VIN', 'after': 0x42}


def test_fixed_and_dynamic_array():
    fixed = Struct('S', [ArrayType('vals', Scalar('e', 'uint8'), length_field_bits=0, fixed_count=3)])
    assert deserialize(bytes([1, 2, 3, 9]), fixed) == {'vals': [1, 2, 3]}
    dynamic = Struct('S', [ArrayType('vals', Scalar('e', 'uint16'), length_field_bits=32), Scalar('n', 'uint8')])
    payload = (4).to_bytes(4, 'big') + bytes([0x00, 0x01, 0x00, 0x02]) + bytes([0x7F])
    assert deserialize(payload, dynamic) == {'vals': [1, 2], 'n': 0x7F}


def test_nested_struct_and_flatten():
    point = Struct('Point', [Scalar('x', 'sint16'), Scalar('y', 'sint16')])
    outer = Struct('S', [Scalar('id', 'uint8'), ArrayType('points', point, length_field_bits=0, fixed_count=2)])
    data = bytes([0x05]) + bytes([0x00, 0x01, 0x00, 0x02, 0xFF, 0xFF, 0x00, 0x03])
    decoded = deserialize(data, outer)
    assert decoded == {'id': 5, 'points': [{'x': 1, 'y': 2}, {'x': -1, 'y': 3}]}
    assert flatten(decoded) == {'id': 5, 'points[0].x': 1, 'points[0].y': 2, 'points[1].x': -1, 'points[1].y': 3}


def test_struct_length_prefix_skips_unknown_trailing_members():
    # A length-prefixed struct: the sender included 3 bytes, this description knows only one uint8 member
    struct = Struct('S', [Scalar('a', 'uint8')], length_field_bits=32)
    payload = (3).to_bytes(4, 'big') + bytes([0x11, 0x22, 0x33]) + bytes([0x99])
    reader_result = deserialize(payload, struct)
    assert reader_result == {'a': 0x11}  # The extra bytes are skipped


def test_too_short_payload_raises():
    with pytest.raises(SerializationError):
        deserialize(b'\x01', Struct('S', [Scalar('a', 'uint32')]))
    with pytest.raises(SerializationError):
        Scalar('x', 'not_a_type')


def test_description_decode():
    description = SomeIpDescription()
    from can_explorer.core.someip_serialization import MessageType

    description.add(0x1234, 0x8001, MessageType('SpeedEvent', Struct('p', [Scalar('speed', 'uint16')])))
    assert description.decode(0x1234, 0x8001, bytes([0x0F, 0xA0])) == {'speed': 0x0FA0}
    assert description.decode(0x9999, 0x0001, b'\x00') is None  # Unknown
    assert description.decode(0x1234, 0x8001, b'') is None  # Too short
