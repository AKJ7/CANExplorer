"""ECU diagnostic descriptions (Vector CANdelaStudio .cdd): data identifiers and the layout of their data.

Used to name the data identifiers of an ECU and to decode ReadDataByIdentifier responses into fields.

The reader is self-contained: cantools' CDD support is deprecated and scheduled for removal. It covers the
simple data types of CDD files (IDENT, LINCOMP, TEXTTBL and others with a CVALUETYPE): unsigned, signed,
IEEE float and text encodings in both byte orders, with linear scaling and text tables. DIDs whose data uses
other (structured) types are listed but their data is shown as hex.
"""

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

DESCRIPTION_SUFFIXES = ('.cdd',)
TEXT_ENCODINGS = ('asc', 'utf')
BIG_ENDIAN, LITTLE_ENDIAN = '21', '12'


class DescriptionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DataType:
    name: str
    bit_length: int
    encoding: str  # uns, sgn, flt, asc, utf, bcd...
    byte_order: str  # '21' big endian, '12' little endian
    factor: float = 1.0
    offset: float = 0.0
    unit: str = ''
    choices: Tuple[Tuple[int, int, str], ...] = ()  # (first, last, text)


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    data_type: DataType
    bit_offset: int  # From the start of the DID data; union alternatives share offsets


@dataclass(slots=True)
class DataIdentifier:
    identifier: int
    name: str
    fields: List[Field] = field(default_factory=list)
    complete: bool = True  # False when a field uses a data type that cannot be decoded


def _text(element: ElementTree.Element, path: str) -> Optional[str]:
    value = element.findtext(path)
    return value.strip() if value is not None else None


def _number(text: str) -> int:
    return int(text.strip('()'), 0)


def _load_data_types(ecu_doc: ElementTree.Element) -> Dict[str, DataType]:
    data_types = {}
    for element in ecu_doc.iterfind('DATATYPES/*'):
        value_type = element.find('CVALUETYPE')
        if value_type is None or 'id' not in element.attrib:
            continue  # Structured types (STRUCTDT, MUXDT, ...) have no single coded value
        try:
            bit_length = int(value_type.attrib['bl'])
        except (KeyError, ValueError):
            continue
        comp = element.find('COMP')
        choices = []
        for text_map in element.iterfind('TEXTMAP'):
            try:
                first, last = _number(text_map.attrib['s']), _number(text_map.attrib['e'])
            except (KeyError, ValueError):
                continue
            choices.append((first, last, _text(text_map, 'TEXT/TUV') or ''))
        data_types[element.attrib['id']] = DataType(
            name=_text(element, 'NAME/TUV') or element.attrib['id'],
            bit_length=bit_length,
            encoding=value_type.attrib.get('enc', 'uns'),
            byte_order=value_type.attrib.get('bo', BIG_ENDIAN),
            factor=float(comp.attrib.get('f', 1)) if comp is not None else 1.0,
            offset=float(comp.attrib.get('o', 0)) if comp is not None else 0.0,
            unit=_text(element, 'PVALUETYPE/UNIT') or '',
            choices=tuple(choices),
        )
    return data_types


class _Undecodable(Exception):
    pass


def _layout(
    element: ElementTree.Element,
    offset: int,
    data_types: Dict[str, DataType],
    library: Dict[str, ElementTree.Element],
    fields: List[Field],
) -> int:
    """Append the fields of a SIMPLECOMPCONT, STRUCT, UNION or DID structure; returns the size in bits.

    Structures place their members one after the other; the alternatives of a union overlay the same bits.
    """
    if element.tag == 'DATAOBJ':
        data_type = data_types.get(element.attrib.get('dtref', ''))
        if data_type is None:
            raise _Undecodable
        fields.append(Field(_text(element, 'QUAL') or data_type.name, data_type, offset))
        return data_type.bit_length
    if element.tag == 'UNION':
        return max(
            (_layout(child, offset, data_types, library, fields) for child in element if child.tag != 'QUAL'),
            default=0,
        )
    if element.tag == 'DIDDATAREF':
        referenced = library.get(element.attrib.get('didRef', ''))
        structure = referenced.find('STRUCTURE') if referenced is not None else None
        return _layout(structure, offset, data_types, library, fields) if structure is not None else 0
    # STRUCT, STRUCTURE and SIMPLECOMPCONT: members in sequence
    members = [child for child in element if child.tag in ('DATAOBJ', 'STRUCT', 'UNION', 'DIDDATAREF')]
    if element.tag == 'SIMPLECOMPCONT' and any(child.tag == 'DIDDATAREF' for child in members):
        # Next to a DIDDATAREF a structure is not part of the record (e.g. an IO control mask)
        members = [child for child in members if child.tag != 'STRUCT']
    size = 0
    for member in members:
        size += _layout(member, offset + size, data_types, library, fields)
    return size


def load_cdd(path: str | Path) -> List[DataIdentifier]:
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as e:
        raise DescriptionError(f'Invalid XML: {e}') from e
    ecu_doc = root.find('ECUDOC')
    if ecu_doc is None:
        raise DescriptionError('Not a CANdelaStudio description: no ECUDOC element')
    data_types = _load_data_types(ecu_doc)
    library = {element.attrib['id']: element for element in ecu_doc.iterfind('DIDS/DID') if 'id' in element.attrib}
    identifiers = []
    for instance in ecu_doc.iterfind('ECU/VAR/DIAGCLASS/DIAGINST'):
        static_value = instance.find('STATICVALUE')
        name = _text(instance, 'QUAL')
        if static_value is None or not name:
            continue
        did = DataIdentifier(int(static_value.attrib['v'], 0), name)
        container = instance.find('SIMPLECOMPCONT')
        if container is not None:
            try:
                _layout(container, 0, data_types, library, did.fields)
            except _Undecodable:
                did.complete = False
        identifiers.append(did)
    return identifiers


def _extract(data: bytes, bit_offset: int, data_type: DataType) -> int:
    """Raw value of a field.

    CDD offsets are "c-style": bit k of the record is bit k % 8 (counted from the least significant bit)
    of byte k // 8. Little endian fields start at that bit. Big endian fields are located by their most
    significant bit, at byte (k // 8) bit min(7, k % 8 + length - 1), and continue into the following bytes.
    """
    length = data_type.bit_length
    total = len(data) * 8
    if data_type.byte_order == LITTLE_ENDIAN:
        if bit_offset + length > total:
            raise DescriptionError('Data too short')
        return (int.from_bytes(data, 'little') >> bit_offset) & ((1 << length) - 1)
    msb = 8 * (bit_offset // 8) + min(7, bit_offset % 8 + length - 1)
    first = (msb // 8) * 8 + (7 - msb % 8)  # Index of the MSB counting from the first transmitted bit
    if first + length > total:
        raise DescriptionError('Data too short')
    return (int.from_bytes(data, 'big') >> (total - first - length)) & ((1 << length) - 1)


def decode_field(data: bytes, bit_offset: int, data_type: DataType):
    encoding = data_type.encoding
    if encoding in TEXT_ENCODINGS:
        if bit_offset % 8 or data_type.bit_length % 8:
            raise DescriptionError('Text fields must be byte aligned')
        end = (bit_offset + data_type.bit_length) // 8
        if end > len(data):
            raise DescriptionError('Data too short')
        raw = data[bit_offset // 8 : end]
        return raw.rstrip(b'\x00\xff ').decode('utf-8' if encoding == 'utf' else 'latin-1', errors='replace')
    raw = _extract(data, bit_offset, data_type)
    if encoding == 'flt' and data_type.bit_length in (32, 64):
        size = data_type.bit_length // 8
        value = struct.unpack('>f' if size == 4 else '>d', raw.to_bytes(size, 'big'))[0]
    elif encoding == 'sgn' and raw & (1 << (data_type.bit_length - 1)):
        value = raw - (1 << data_type.bit_length)
    else:
        value = raw
    for first, last, text in data_type.choices:
        if first <= value <= last:
            return text
    if data_type.factor != 1 or data_type.offset != 0:
        return value * data_type.factor + data_type.offset
    return value


class DiagnosticDescription:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.suffix.lower() not in DESCRIPTION_SUFFIXES:
            raise DescriptionError(f'Unsupported diagnostic description {self.path.suffix!r}, use .cdd')
        self._dids = {did.identifier: did for did in load_cdd(self.path)}

    def identifiers(self) -> Dict[int, str]:
        return {identifier: did.name for identifier, did in sorted(self._dids.items())}

    def name(self, identifier: int) -> Optional[str]:
        did = self._dids.get(identifier)
        return did.name if did is not None else None

    def fields(self, identifier: int) -> List[Dict[str, str]]:
        did = self._dids.get(identifier)
        if did is None:
            return []
        return [{'name': item.name, 'unit': item.data_type.unit} for item in did.fields]

    def decode(self, identifier: int, data: bytes) -> Optional[Dict[str, object]]:
        """Field values of a DID; None for unknown identifiers, undecodable layouts or too short data."""
        did = self._dids.get(identifier)
        if did is None or not did.complete:
            return None
        data = bytes(data)
        try:
            return {item.name: decode_field(data, item.bit_offset, item.data_type) for item in did.fields}
        except DescriptionError:
            return None

    def describe(self, identifier: int, data: bytes) -> Optional[str]:
        did = self._dids.get(identifier)
        if did is None:
            return None
        values = self.decode(identifier, data)
        if values is None:
            # A known DID whose data layout cannot be decoded is shown with its name and the raw data
            return f'{did.name}: {bytes(data).hex(" ").upper()}' if not did.complete else None
        units = {item.name: item.data_type.unit for item in did.fields if item.data_type.unit}
        fields = []
        for name, value in values.items():
            text = f'{value:g}' if isinstance(value, float) else str(value)
            fields.append(f'{name}={text}' + (f' {units[name]}' if name in units else ''))
        return f'{did.name}: {", ".join(fields)}' if fields else did.name
