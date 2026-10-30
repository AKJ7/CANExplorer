"""SOME/IP payload serialization: a type model and a deserializer that turns a message payload into named values.

The model mirrors the SOME/IP on-wire serialization (AUTOSAR SOME/IP / ASAM): fixed and variable scalars, booleans,
IEEE floats, fixed and length-prefixed strings, fixed and dynamic arrays, and nested structs. A description file
(FIBEX, see someip_fibex.py) is read into this model; the deserializer walks a payload against it.

Byte order is per scalar (big endian by default). Dynamic arrays and strings carry a length field giving the size
of their content in bytes; structs may carry a length field for extensibility. Optional (TLV) members are not
handled yet.
"""

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional, Union

BASE_SIZES = {
    'uint8': 1,
    'uint16': 2,
    'uint32': 4,
    'uint64': 8,
    'sint8': 1,
    'sint16': 2,
    'sint32': 4,
    'sint64': 8,
    'float32': 4,
    'float64': 8,
    'bool': 1,
}


class SerializationError(ValueError):
    pass


@dataclass(frozen=True)
class Scalar:
    name: str
    base: str  # A key of BASE_SIZES
    byte_order: str = 'big'  # 'big' or 'little'

    def __post_init__(self):
        if self.base not in BASE_SIZES:
            raise SerializationError(f'Unknown base type {self.base!r}')


@dataclass(frozen=True)
class StringType:
    name: str
    encoding: str = 'utf-8'  # 'utf-8' or 'utf-16'
    length_field_bits: int = 32  # 0 for a fixed-length string
    fixed_length: int = 0  # Bytes, when length_field_bits == 0


@dataclass(frozen=True)
class ArrayType:
    name: str
    element: 'DataType'
    length_field_bits: int = 32  # 0 for a fixed-length array
    fixed_count: int = 0  # Elements, when length_field_bits == 0


@dataclass(frozen=True)
class Struct:
    name: str
    members: List['DataType'] = dataclass_field(default_factory=list)
    length_field_bits: int = 0  # Optional length prefix for extensibility


DataType = Union[Scalar, StringType, ArrayType, Struct]


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise SerializationError('Payload too short')
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def uint(self, bits: int, order: str = 'big') -> int:
        return int.from_bytes(self.take(bits // 8), order)


def _read_length(reader: _Reader, bits: int) -> int:
    return reader.uint(bits) if bits else 0


def _deserialize(reader: _Reader, data_type: DataType) -> Any:
    if isinstance(data_type, Scalar):
        return _scalar(reader, data_type)
    if isinstance(data_type, StringType):
        return _string(reader, data_type)
    if isinstance(data_type, ArrayType):
        return _array(reader, data_type)
    if isinstance(data_type, Struct):
        return _struct(reader, data_type)
    raise SerializationError(f'Unsupported type {data_type!r}')


def _scalar(reader: _Reader, scalar: Scalar) -> Any:
    size = BASE_SIZES[scalar.base]
    raw = reader.take(size)
    if scalar.base == 'bool':
        return raw[0] != 0
    if scalar.base.startswith('float'):
        import struct as _struct

        return _struct.unpack(('>' if scalar.byte_order == 'big' else '<') + ('f' if size == 4 else 'd'), raw)[0]
    value = int.from_bytes(raw, scalar.byte_order)
    if scalar.base.startswith('sint') and value >> (size * 8 - 1):
        value -= 1 << (size * 8)
    return value


def _string(reader: _Reader, string: StringType) -> str:
    if string.length_field_bits:
        length = _read_length(reader, string.length_field_bits)
    else:
        length = string.fixed_length
    raw = reader.take(length)
    codec = 'utf-16-be' if string.encoding == 'utf-16' else 'utf-8'
    # Strip a byte-order mark and trailing NULs
    text = raw.decode(codec, errors='replace')
    return text.replace('﻿', '').rstrip('\x00')


def _array(reader: _Reader, array: ArrayType) -> List[Any]:
    items = []
    if array.length_field_bits:
        length = _read_length(reader, array.length_field_bits)
        end = reader.pos + length
        if end > len(reader.data):
            raise SerializationError('Array longer than the payload')
        while reader.pos < end:
            items.append(_deserialize(reader, array.element))
        reader.pos = end
    else:
        for _ in range(array.fixed_count):
            items.append(_deserialize(reader, array.element))
    return items


def _struct(reader: _Reader, struct: Struct) -> Dict[str, Any]:
    end = None
    if struct.length_field_bits:
        length = _read_length(reader, struct.length_field_bits)
        end = reader.pos + length
    result: Dict[str, Any] = {}
    for member in struct.members:
        if end is not None and reader.pos >= end:
            break  # A newer sender added members this description does not know
        result[member.name] = _deserialize(reader, member)
    if end is not None:
        reader.pos = end
    return result


def deserialize(payload: bytes, struct: Struct) -> Dict[str, Any]:
    """Decode a payload against a struct (the parameters of a method or event). Raises SerializationError."""
    reader = _Reader(bytes(payload))
    return _struct(reader, struct)


def flatten(values: Dict[str, Any], prefix: str = '') -> Dict[str, Any]:
    """Flatten a decoded tree to 'a.b[0].c' -> value, for a flat signal view."""
    flat: Dict[str, Any] = {}
    for name, value in values.items():
        key = f'{prefix}{name}'
        if isinstance(value, dict):
            flat.update(flatten(value, key + '.'))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    flat.update(flatten(item, f'{key}[{index}].'))
                else:
                    flat[f'{key}[{index}]'] = item
        else:
            flat[key] = value
    return flat


@dataclass
class MessageType:
    """The payload structure of one method's input/output or an event, keyed in a description by (service, id)."""

    name: str
    struct: Struct


class SomeIpDescription:
    """Maps a (service id, method/event id) to the structure of its payload. A method's request and response carry
    different payloads, so they are stored separately; events use the request slot."""

    def __init__(self):
        self.messages: Dict[tuple, MessageType] = {}  # (service_id, method_id, is_response) -> MessageType
        self.service_names: Dict[int, str] = {}

    def add(self, service_id: int, method_id: int, message: MessageType, is_response: bool = False) -> None:
        self.messages[(service_id, method_id, is_response)] = message

    def message(self, service_id: int, method_id: int, is_response: bool = False) -> Optional[MessageType]:
        return self.messages.get((service_id, method_id, is_response)) or self.messages.get(
            (service_id, method_id, False)
        )

    def decode(
        self, service_id: int, method_id: int, payload: bytes, is_response: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Decoded, flattened values, or None when the message is unknown or the payload does not fit."""
        message = self.message(service_id, method_id, is_response)
        if message is None:
            return None
        try:
            return flatten(deserialize(payload, message.struct))
        except SerializationError:
            return None

    def __len__(self) -> int:
        return len(self.messages)
