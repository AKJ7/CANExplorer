import struct as _struct

import pytest

from can_explorer.core.someip_serialization import (
    ArrayType,
    BitSubfield,
    Bitfield,
    Scalar,
    SerializationError,
    SomeIpDescription,
    StringType,
    Struct,
    UnionType,
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


def _tag(wire_type, data_id):
    return ((wire_type << 12) | data_id).to_bytes(2, 'big')


def test_tlv_optional_members_absent_and_out_of_order():
    s = Struct(
        'S',
        [
            Scalar('speed', 'uint16', data_id=1),
            Scalar('mode', 'uint8', data_id=2),
            StringType('name', 'utf-8', length_field_bits=32, data_id=3),
        ],
    )
    # mode (wire 0) comes before speed (wire 1); name (id 3) is omitted entirely.
    payload = _tag(0, 2) + bytes([0x07]) + _tag(1, 1) + bytes([0x0F, 0xA0])
    assert deserialize(payload, s) == {'mode': 0x07, 'speed': 0x0FA0}


def test_tlv_skips_unknown_member():
    s = Struct('S', [Scalar('speed', 'uint16', data_id=1)])
    unknown = _tag(5, 9) + bytes([3]) + b'\xaa\xbb\xcc'  # wire type 5: a 1-byte length field, id 9 not in the model
    payload = unknown + _tag(1, 1) + bytes([0x12, 0x34])
    assert deserialize(payload, s) == {'speed': 0x1234}


def test_tlv_complex_member_bounded_by_its_length():
    inner = Struct('inner', [Scalar('a', 'uint8'), Scalar('b', 'uint8')], data_id=5)
    s = Struct('S', [inner])
    # wire type 6 (2-byte length) says 3 bytes; the struct reads 2, the extra 0x99 is bounded out.
    payload = _tag(6, 5) + (3).to_bytes(2, 'big') + bytes([0x01, 0x02, 0x99])
    assert deserialize(payload, s) == {'inner': {'a': 1, 'b': 2}}


def test_union_selects_member_and_skips_padding():
    union = UnionType('u', [Scalar('as_int', 'uint32'), Scalar('as_byte', 'uint8')])
    s = Struct('S', [union])
    as_int = (4).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + (0x11223344).to_bytes(4, 'big')
    assert deserialize(as_int, s) == {'u': {'as_int': 0x11223344}}
    as_byte = (4).to_bytes(4, 'big') + (2).to_bytes(4, 'big') + bytes([0x7F, 0, 0, 0])  # 3 padding bytes
    assert deserialize(as_byte, s) == {'u': {'as_byte': 0x7F}}
    null = (0).to_bytes(4, 'big') + (0).to_bytes(4, 'big')  # type 0 = null
    assert deserialize(null, s) == {'u': {}}


def test_bitfield_unpacks_msb_first():
    bitfield = Bitfield(
        'flags',
        'uint16',
        [BitSubfield('version', 4), BitSubfield('count', 10), BitSubfield('ready', 1), BitSubfield('err', 1)],
    )
    s = Struct('S', [bitfield])
    value = (0x3 << 12) | (5 << 2) | (1 << 1) | 0
    decoded = deserialize(value.to_bytes(2, 'big'), s)
    assert decoded == {'flags': {'version': 3, 'count': 5, 'ready': 1, 'err': 0}}
    assert flatten(decoded) == {'flags.version': 3, 'flags.count': 5, 'flags.ready': 1, 'flags.err': 0}


def test_bitfield_signed_subfield():
    bitfield = Bitfield('b', 'uint8', [BitSubfield('hi', 4), BitSubfield('lo', 4, signed=True)])
    assert deserialize(bytes([0x2F]), Struct('S', [bitfield])) == {'b': {'hi': 2, 'lo': -1}}


def test_description_decode():
    description = SomeIpDescription()
    from can_explorer.core.someip_serialization import MessageType

    description.add(0x1234, 0x8001, MessageType('SpeedEvent', Struct('p', [Scalar('speed', 'uint16')])))
    assert description.decode(0x1234, 0x8001, bytes([0x0F, 0xA0])) == {'speed': 0x0FA0}
    assert description.decode(0x9999, 0x0001, b'\x00') is None  # Unknown
    assert description.decode(0x1234, 0x8001, b'') is None  # Too short
