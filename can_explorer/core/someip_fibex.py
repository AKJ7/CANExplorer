"""Read a FIBEX 4 (ASAM MCD-2 NET) file describing SOME/IP service interfaces into a SomeIpDescription, so message
payloads can be deserialized into named signals (see someip_serialization.py).

A subset is supported: service interfaces with their methods (input and return parameters) and events, and the
data types they use — scalars (through a CODING's base data type), fixed and dynamic arrays, length-prefixed and
fixed strings, nested structures and unions (COMPLEX-DATATYPE-CLASS UNION). Optional (TLV) members are read from
a member's DATA-ID, so a member carrying one is decoded as a tagged, optional field. Bit fields have no standard
FIBEX representation and are not auto-read (the serialization core supports them for hand-built descriptions).
ARXML SOME/IP service interfaces are a separate format and not read here.
"""

from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree

from can_explorer.core.someip_serialization import (
    ArrayType,
    DataType,
    MessageType,
    Scalar,
    SomeIpDescription,
    StringType,
    Struct,
    UnionType,
)

# FIBEX / ASAM base data type -> the serialization scalar base
BASE_TYPES = {
    'A_UINT8': 'uint8',
    'A_UINT16': 'uint16',
    'A_UINT32': 'uint32',
    'A_UINT64': 'uint64',
    'A_INT8': 'sint8',
    'A_INT16': 'sint16',
    'A_INT32': 'sint32',
    'A_INT64': 'sint64',
    'A_FLOAT32': 'float32',
    'A_FLOAT64': 'float64',
    'A_BOOLEAN': 'bool',
}
STRING_TYPES = {'A_ASCIISTRING': 'utf-8', 'A_UNICODE2STRING': 'utf-16', 'A_UTF8STRING': 'utf-8'}


class FibexError(ValueError):
    pass


def _strip_namespaces(root: ElementTree.Element) -> ElementTree.Element:
    for element in root.iter():
        if isinstance(element.tag, str) and '}' in element.tag:
            element.tag = element.tag.rsplit('}', 1)[1]
    return root


def _text(element: Optional[ElementTree.Element], path: str) -> Optional[str]:
    if element is None:
        return None
    value = element.findtext(path)
    return value.strip() if value and value.strip() else None


def _int(element: Optional[ElementTree.Element], path: str) -> Optional[int]:
    value = _text(element, path)
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _name(element: ElementTree.Element) -> str:
    return _text(element, 'SHORT-NAME') or element.get('ID') or 'value'


class _Reader:
    def __init__(self, root: ElementTree.Element):
        self.root = _strip_namespaces(root)
        self.index: Dict[str, ElementTree.Element] = {
            element.get('ID'): element for element in self.root.iter() if element.get('ID')
        }
        self.codings: Dict[str, ElementTree.Element] = {
            element.get('ID'): element for element in self.root.iter('CODING') if element.get('ID')
        }

    def resolve(self, element: Optional[ElementTree.Element], ref_tag: str) -> Optional[ElementTree.Element]:
        reference = element.find(ref_tag) if element is not None else None
        return self.index.get(reference.get('ID-REF', '')) if reference is not None else None

    def _coding_base(self, datatype: ElementTree.Element) -> Optional[str]:
        """The ASAM base data type of a common datatype, via its CODING-REF -> CODING -> CODED-TYPE, or a direct
        BASE-DATA-TYPE attribute/element as a shortcut."""
        direct = datatype.get('BASE-DATA-TYPE') or _text(datatype, 'BASE-DATA-TYPE')
        if direct:
            return direct
        coding = self.resolve(datatype, 'CODING-REF')
        if coding is None:
            return None
        coded = coding.find('CODED-TYPE')
        if coded is not None:
            return coded.get('BASE-DATA-TYPE') or _text(coded, 'BASE-DATA-TYPE')
        return None

    def datatype(
        self, element: ElementTree.Element, name: str, attributes: Optional[ElementTree.Element] = None
    ) -> Optional[DataType]:
        """Build the serialization type of a FIBEX datatype element. `attributes` is the referencing parameter or
        member, which may carry SOME/IP length-field and byte-order settings."""
        xsi = _xsi_type(element)
        if 'COMPLEX' in xsi or element.find('COMPLEX-DATATYPE-CLASS') is not None:
            return self._complex(element, name, attributes)
        base = self._coding_base(element)
        if base in STRING_TYPES:
            return StringType(name, STRING_TYPES[base], *_string_length(attributes))
        if base in BASE_TYPES:
            return Scalar(name, BASE_TYPES[base], _byte_order(attributes, element))
        return None

    def _complex(self, element: ElementTree.Element, name: str, attributes) -> Optional[DataType]:
        kind = (_text(element, 'COMPLEX-DATATYPE-CLASS') or 'STRUCTURE').upper()
        members = self._members(element)
        if kind in ('STRUCTURE', 'UNION'):
            built = self._build_members(members)
            if built is None:
                return None
            if kind == 'UNION':
                return UnionType(
                    name,
                    built,
                    length_field_bits=_int(element, 'UNION-LENGTH-FIELD-SIZE') or 32,
                    type_field_bits=_int(element, 'UNION-TYPE-SELECTOR-FIELD-SIZE') or 32,
                )
            return Struct(name, built, length_field_bits=_struct_length(element))
        if kind == 'TYPEDEF' and members:  # A named alias of a single type
            member_name, ref, member = members[0]
            return self.datatype(self.index[ref], name, member)
        if 'ARRAY' in kind and members:
            member_name, ref, member = members[0]
            element_type = self.datatype(self.index[ref], member_name, member)
            if element_type is None:
                return None
            count, length_bits = _array_shape(element, attributes)
            return ArrayType(name, element_type, length_field_bits=length_bits, fixed_count=count)
        return None

    def _build_members(self, members) -> Optional[List[DataType]]:
        """Build each member's type, attaching a DATA-ID (SOME/IP TLV tag) when the member carries one."""
        built: List[DataType] = []
        for member_name, ref, member in members:
            item = self.datatype(self.index[ref], member_name, member)
            if item is None:
                return None
            data_id = _int(member, 'DATA-ID')
            built.append(replace(item, data_id=data_id) if data_id is not None else item)
        return built

    def _members(self, element: ElementTree.Element):
        members = []
        for member in element.findall('MEMBERS/MEMBER'):
            reference = member.find('DATATYPE-REF')
            if reference is not None and reference.get('ID-REF') in self.index:
                members.append((_name(member), reference.get('ID-REF'), member))
        members.sort(key=lambda item: _int(item[2], 'POSITION') or 0)
        return members


def _xsi_type(element: ElementTree.Element) -> str:
    for key, value in element.attrib.items():
        if key.endswith('type'):
            return value.rsplit(':', 1)[-1].upper()
    return ''


def _byte_order(attributes, element) -> str:
    for source in (attributes, element):
        order = _text(source, 'BYTE-ORDER') if source is not None else None
        order = order or (source.get('BYTE-ORDER') if source is not None else None)
        if order:
            return 'little' if 'LEAST' in order.upper() or order.lower() == 'little' else 'big'
    return 'big'


def _string_length(attributes):
    bits = _int(attributes, 'STRING-LENGTH-FIELD-SIZE') if attributes is not None else None
    fixed = _int(attributes, 'MAX-LENGTH') if attributes is not None else None
    if bits:
        return bits, 0
    if fixed:
        return 0, fixed
    return 32, 0  # Default: 32-bit length prefix


def _array_shape(element, attributes):
    for source in (attributes, element):
        if source is None:
            continue
        maximum = _int(source, 'ARRAY-DECLARATION/ARRAY-DIMENSION/MAXIMUM-SIZE')
        minimum = _int(source, 'ARRAY-DECLARATION/ARRAY-DIMENSION/MINIMUM-SIZE')
        if maximum is not None and maximum == minimum:
            return maximum, 0  # Fixed-length array
    bits = (_int(attributes, 'ARRAY-LENGTH-FIELD-SIZE') if attributes is not None else None) or 32
    return 0, bits


def _struct_length(element) -> int:
    return _int(element, 'STRUCT-LENGTH-FIELD-SIZE') or 0


def _parameters(reader: _Reader, container: Optional[ElementTree.Element], name: str) -> Optional[Struct]:
    if container is None:
        return Struct(name, [])
    members: List[DataType] = []
    params = (
        container.findall('INPUT-PARAMETER') + container.findall('RETURN-PARAMETER') + container.findall('PARAMETER')
    )
    for param in sorted(params, key=lambda param: _int(param, 'POSITION') or 0):
        reference = param.find('DATATYPE-REF')
        datatype = reader.index.get(reference.get('ID-REF', '')) if reference is not None else None
        if datatype is None:
            return None
        built = reader.datatype(datatype, _name(param), param)
        if built is None:
            return None
        members.append(built)
    return Struct(name, members)


def load_fibex(path: str | Path) -> SomeIpDescription:
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as e:
        raise FibexError(f'Invalid XML: {e}') from e
    reader = _Reader(root)
    interfaces = list(reader.root.iter('SERVICE-INTERFACE'))
    if not interfaces:
        raise FibexError('No SOME/IP service interfaces found (is this a FIBEX file?)')
    description = SomeIpDescription()
    for interface in interfaces:
        service_id = _int(interface, 'SERVICE-IDENTIFIER')
        if service_id is None:
            continue
        description.service_names[service_id] = _name(interface)
        for method in interface.iter('METHOD'):
            method_id = _int(method, 'METHOD-IDENTIFIER')
            if method_id is None:
                continue
            name = _name(method)
            request = _parameters(reader, method.find('INPUT-PARAMETERS'), name)
            response = _parameters(reader, method.find('RETURN-PARAMETERS'), name)
            if request is not None:
                description.add(service_id, method_id, MessageType(name, request), is_response=False)
            if response is not None:
                description.add(service_id, method_id, MessageType(name, response), is_response=True)
        for event in interface.iter('EVENT'):
            event_id = _int(event, 'EVENT-IDENTIFIER') or _int(event, 'NOTIFIER/EVENT-ID')
            if event_id is None:
                continue
            name = _name(event)
            container = event.find('PARAMETERS')
            parameters = _parameters(reader, container if container is not None else event, name)
            if parameters is not None:
                description.add(service_id, event_id, MessageType(name, parameters), is_response=False)
    return description
