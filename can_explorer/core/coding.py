"""Coding records: reading, editing and writing a module's coding (or any configuration record) bit by bit.

A *coding record* is a block of bytes a module keeps at a diagnostic identifier — read with one service and written
with its counterpart:

- `kwp`: KWP2000 local identifier, read with ReadDataByLocalIdentifier (0x21) and written with
  WriteDataByLocalIdentifier (0x3B), e.g. over VW TP 2.0;
- `uds`: UDS data identifier, read with ReadDataByIdentifier (0x22) and written with WriteDataByIdentifier (0x2E).

What CANExplorer provides is the mechanism: decoding the record into named *fields* (a value in some bits of one
byte, with optional names for its values), editing fields or bytes, and a write that reads the current record first
(kept as the backup), writes, reads it back and reports whether the module kept it. **Where** a module keeps its
coding and **what** its bits mean are the manufacturer's and are not built in: they come from a label file you
supply (`core/diagnostic_labels.py`). Without fields, a record is edited as raw bytes.

Writing coding changes the vehicle's configuration, and a module may need a particular session or security access
first: do that with the client before `write_record`, and keep the backup.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from can_explorer.core.frames import format_hex

PROTOCOLS = ('kwp', 'uds')


class CodingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CodingField:
    """A value in `width` bits of byte `byte` from bit `bit` (0 = least significant), with optional value names."""

    name: str
    byte: int
    bit: int = 0
    width: int = 8
    values: Mapping[int, str] = field(default_factory=dict)
    description: str = ''

    def __post_init__(self):
        if self.byte < 0:
            raise CodingError(f'Field {self.name!r}: the byte must be 0 or more')
        if not (0 <= self.bit <= 7 and 1 <= self.width <= 8 and self.bit + self.width <= 8):
            raise CodingError(f'Field {self.name!r}: bits {self.bit}..{self.bit + self.width - 1} do not fit one byte')

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.bit

    @property
    def maximum(self) -> int:
        return (1 << self.width) - 1

    def location(self) -> str:
        if self.width == 8:
            return f'byte {self.byte}'
        if self.width == 1:
            return f'byte {self.byte} bit {self.bit}'
        return f'byte {self.byte} bits {self.bit}-{self.bit + self.width - 1}'

    def decode(self, data: bytes) -> int:
        if self.byte >= len(data):
            raise CodingError(f'Field {self.name!r} is at byte {self.byte}, but the record has {len(data)} bytes')
        return (data[self.byte] & self.mask) >> self.bit

    def encode(self, data: bytes, value: int) -> bytes:
        if not 0 <= value <= self.maximum:
            raise CodingError(f'Field {self.name!r} takes 0-{self.maximum}, got {value}')
        if self.byte >= len(data):
            raise CodingError(f'Field {self.name!r} is at byte {self.byte}, but the record has {len(data)} bytes')
        result = bytearray(data)
        result[self.byte] = (result[self.byte] & ~self.mask & 0xFF) | (value << self.bit)
        return bytes(result)

    def text(self, value: int) -> str:
        name = self.values.get(value)
        return f'{value} ({name})' if name else str(value)


@dataclass(frozen=True, slots=True)
class FieldValue:
    field: CodingField
    value: int

    @property
    def text(self) -> str:
        return self.field.text(self.value)

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {'name': self.field.name, 'location': self.field.location(), 'value': self.value}
        if self.field.values:
            result['meaning'] = self.field.values.get(self.value, '')
            result['options'] = {str(value): name for value, name in sorted(self.field.values.items())}
        if self.field.description:
            result['description'] = self.field.description
        return result


@dataclass(frozen=True, slots=True)
class CodingRecord:
    """Where a record is kept (`protocol` and `identifier`), its expected `length` (if known) and its fields."""

    name: str
    protocol: str
    identifier: int
    fields: Tuple[CodingField, ...] = ()
    length: Optional[int] = None

    def __post_init__(self):
        if self.protocol not in PROTOCOLS:
            raise CodingError(f'Record {self.name!r}: protocol must be one of {", ".join(PROTOCOLS)}')
        limit = 0xFF if self.protocol == 'kwp' else 0xFFFF
        if not 0 <= self.identifier <= limit:
            raise CodingError(f'Record {self.name!r}: identifier 0x{self.identifier:X} is out of range')
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise CodingError(f'Record {self.name!r}: field names must be unique')

    def location(self) -> str:
        if self.protocol == 'kwp':
            return f'KWP2000 local identifier 0x{self.identifier:02X}'
        return f'UDS data identifier 0x{self.identifier:04X}'

    def field(self, name: str) -> CodingField:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise CodingError(f'Record {self.name!r} has no field {name!r}')

    def decode(self, data: bytes) -> List[FieldValue]:
        """The fields that the data covers (a field beyond a short record is left out)."""
        return [FieldValue(f, f.decode(data)) for f in self.fields if f.byte < len(data)]

    def apply(self, data: bytes, changes: Mapping[str, int]) -> bytes:
        """The data with fields set: {field name: value}."""
        for name, value in changes.items():
            data = self.field(name).encode(data, int(value))
        return data

    def check_length(self, data: bytes) -> None:
        if self.length is not None and len(data) != self.length:
            raise CodingError(f'{self.name} is {self.length} bytes, got {len(data)}')

    async def read(self, client) -> bytes:
        """Read the record with a `KwpClient` (protocol kwp) or a `UdsClient` (protocol uds)."""
        if self.protocol == 'kwp':
            return bytes(await client.read_local_identifier(self.identifier))
        return bytes(await client.read_data_by_identifier(self.identifier))

    async def write(self, client, data: bytes) -> None:
        if self.protocol == 'kwp':
            await client.write_local_identifier(self.identifier, bytes(data))
        else:
            await client.write_data_by_identifier(self.identifier, bytes(data))


@dataclass(frozen=True, slots=True)
class ByteChange:
    index: int
    before: int
    after: int


def byte_changes(before: bytes, after: bytes) -> List[ByteChange]:
    if len(before) != len(after):
        raise CodingError(f'Cannot compare records of {len(before)} and {len(after)} bytes')
    return [ByteChange(i, a, b) for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]


def field_changes(record: CodingRecord, before: bytes, after: bytes) -> List[Tuple[CodingField, int, int]]:
    """(field, value before, value after) for each field the edit changes."""
    old = {value.field.name: value.value for value in record.decode(before)}
    return [
        (value.field, old[value.field.name], value.value)
        for value in record.decode(after)
        if old.get(value.field.name) != value.value
    ]


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The outcome of `write_record`: the record before (the backup), what was written and what was read back."""

    record: CodingRecord
    before: bytes
    written: bytes
    read_back: Optional[bytes]

    @property
    def verified(self) -> bool:
        return self.read_back == self.written

    def as_dict(self) -> Dict[str, Any]:
        return {
            'record': self.record.name,
            'location': self.record.location(),
            'before': format_hex(self.before),
            'written': format_hex(self.written),
            'read_back': format_hex(self.read_back) if self.read_back is not None else None,
            'verified': self.verified,
            'changed_bytes': [
                {'byte': c.index, 'before': f'{c.before:02X}', 'after': f'{c.after:02X}'}
                for c in byte_changes(self.before, self.written)
            ],
        }


async def write_record(
    client, record: CodingRecord, data: bytes, expected_before: Optional[bytes] = None, verify: bool = True
) -> WriteResult:
    """Write a record safely: read the current data first (the backup); refuse a write whose length differs from it
    (or from the record's length), or — with `expected_before` — when the module's data changed since it was read
    for editing; write; read back (with `verify`)."""
    data = bytes(data)
    record.check_length(data)
    before = await record.read(client)
    if len(before) != len(data):
        raise CodingError(f'The module holds {len(before)} bytes of {record.name}, the new data has {len(data)}')
    if expected_before is not None and bytes(expected_before) != before:
        raise CodingError(f'{record.name} changed in the module since it was read; read it again before writing')
    if before == data:
        return WriteResult(record, before, data, before)
    await record.write(client, data)
    read_back = await record.read(client) if verify else None
    return WriteResult(record, before, data, read_back)


def parse_field(definition: Mapping[str, Any]) -> CodingField:
    """A field from its label-file definition: name, byte, and either bit (+ width, default 1) or a whole byte."""
    if not isinstance(definition, Mapping) or not str(definition.get('name', '')).strip():
        raise CodingError('A coding field needs a "name"')
    name = str(definition['name'])
    try:
        byte = _int(definition.get('byte'), f'field {name!r} byte')
        if 'bit' in definition:
            bit = _int(definition['bit'], f'field {name!r} bit')
            width = _int(definition.get('width', 1), f'field {name!r} width')
        else:
            bit, width = 0, _int(definition.get('width', 8), f'field {name!r} width')
        values = {
            _int(key, f'field {name!r} value'): str(text) for key, text in dict(definition.get('values') or {}).items()
        }
    except (TypeError, ValueError) as e:
        raise CodingError(str(e)) from None
    return CodingField(name, byte, bit, width, values, str(definition.get('description', '')))


def parse_record(name: str, definition: Mapping[str, Any]) -> CodingRecord:
    if not isinstance(definition, Mapping):
        raise CodingError(f'Coding record {name!r} must be an object')
    protocol = str(definition.get('protocol', 'kwp')).strip().lower()
    identifier = _int(definition.get('identifier'), f'record {name!r} identifier')
    length = definition.get('length')
    fields: Sequence[Any] = definition.get('fields') or []
    if not isinstance(fields, list):
        raise CodingError(f'Coding record {name!r}: "fields" must be a list')
    return CodingRecord(
        name,
        protocol,
        identifier,
        tuple(parse_field(f) for f in fields),
        _int(length, f'record {name!r} length') if length is not None else None,
    )


def _int(value: Any, what: str) -> int:
    """An integer from a JSON number or a string ('0x9A' hex, '12' decimal)."""
    if isinstance(value, bool) or value is None:
        raise CodingError(f'Missing or invalid {what}')
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16) if text.lower().startswith('0x') else int(text, 10)
    except ValueError:
        raise CodingError(f'Invalid {what}: {value!r}') from None
