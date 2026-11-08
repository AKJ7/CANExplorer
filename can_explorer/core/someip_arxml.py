"""Read SOME/IP service interfaces from an AUTOSAR ARXML file into a SomeIpDescription (see
someip_serialization.py), so message payloads can be decoded into named signals — the OEM counterpart of the
FIBEX reader (someip_fibex.py).

AUTOSAR separates the logical SERVICE-INTERFACE (methods, events and their argument data types) from the
SOMEIP-SERVICE-INTERFACE-DEPLOYMENT, which carries the SOME/IP service id and the method/event ids and references
the interface. This reader resolves those references and the data types (via SW-BASE-TYPE and
IMPLEMENTATION-DATA-TYPE) and builds the serialization model.

A subset is supported: scalars, fixed and variable arrays, strings, nested structures and unions (an
IMPLEMENTATION-DATA-TYPE with CATEGORY UNION). Optional (TLV) members, bit fields and application data types are
not read from ARXML; the serialization core supports TLV and bit fields for hand-built descriptions, and the FIBEX
reader reads TLV data IDs. An argument whose type uses an unsupported construct is simply not decoded.
"""

from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree

from can_explorer.core.someip_fibex import _strip_namespaces
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

# SW-BASE-TYPE size (bits) -> serialization scalar base, by encoding
_UINT = {8: 'uint8', 16: 'uint16', 32: 'uint32', 64: 'uint64'}
_SINT = {8: 'sint8', 16: 'sint16', 32: 'sint32', 64: 'sint64'}


class ArxmlError(ValueError):
    pass


def _text(element: Optional[ElementTree.Element], path: str) -> Optional[str]:
    if element is None:
        return None
    value = element.findtext(path)
    return value.strip() if value and value.strip() else None


def _int(element: Optional[ElementTree.Element], path: str) -> Optional[int]:
    value = _text(element, path)
    try:
        return int(value, 0) if value is not None else None
    except ValueError:
        return None


class _Reader:
    def __init__(self, root: ElementTree.Element):
        self.root = _strip_namespaces(root)
        # Index every element that has a SHORT-NAME by its full AUTOSAR path, so *-REF text resolves
        self.by_path: Dict[str, ElementTree.Element] = {}
        self._index(self.root, '')

    def _index(self, element: ElementTree.Element, prefix: str) -> None:
        for child in element:
            name = child.findtext('SHORT-NAME')
            path = f'{prefix}/{name.strip()}' if name and name.strip() else prefix
            if name and name.strip():
                self.by_path[path] = child
            self._index(child, path)

    def resolve(self, element: Optional[ElementTree.Element], ref_tag: str) -> Optional[ElementTree.Element]:
        reference = element.find(ref_tag) if element is not None else None
        return self.by_path.get(reference.text.strip()) if reference is not None and reference.text else None

    def base_type(self, sw_base: ElementTree.Element) -> Optional[str]:
        size = _int(sw_base, 'BASE-TYPE-SIZE')
        encoding = (_text(sw_base, 'BASE-TYPE-ENCODING') or '').upper()
        if size is None:
            return None
        if 'IEEE754' in encoding or 'FLOAT' in encoding:
            return 'float32' if size <= 32 else 'float64'
        if size == 1 or encoding == 'BOOLEAN':
            return 'bool'
        return (_SINT if encoding == '2C' else _UINT).get(size)

    def datatype(self, element: ElementTree.Element, name: str) -> Optional[DataType]:
        """Build the serialization type of an IMPLEMENTATION-DATA-TYPE (or TYPE-TREF target)."""
        category = (_text(element, 'CATEGORY') or 'VALUE').upper()
        if category == 'TYPE_REFERENCE':
            target = self.resolve(
                element.find('SW-DATA-DEF-PROPS/SW-DATA-DEF-PROPS-VARIANTS/SW-DATA-DEF-PROPS-CONDITIONAL'),
                'IMPLEMENTATION-DATA-TYPE-REF',
            )
            return self.datatype(target, name) if target is not None else None
        if category == 'STRING':
            return StringType(name, 'utf-8')
        if category in ('STRUCTURE', 'UNION'):
            members = [
                self._member(child) for child in element.findall('SUB-ELEMENTS/IMPLEMENTATION-DATA-TYPE-ELEMENT')
            ]
            if not members or not all(member is not None for member in members):
                return None
            return UnionType(name, members) if category == 'UNION' else Struct(name, members)
        if category == 'ARRAY':
            elements = element.findall('SUB-ELEMENTS/IMPLEMENTATION-DATA-TYPE-ELEMENT')
            if not elements:
                return None
            inner = self._member(elements[0])
            if inner is None:
                return None
            count = _int(elements[0], 'ARRAY-SIZE') or 0
            return ArrayType(name, inner, length_field_bits=0 if count else 32, fixed_count=count)
        sw_base = self.resolve(
            element.find('SW-DATA-DEF-PROPS/SW-DATA-DEF-PROPS-VARIANTS/SW-DATA-DEF-PROPS-CONDITIONAL'),
            'BASE-TYPE-REF',
        )
        base = self.base_type(sw_base) if sw_base is not None else None
        return Scalar(name, base) if base else None

    def _member(self, element: ElementTree.Element) -> Optional[DataType]:
        name = _text(element, 'SHORT-NAME') or 'value'
        category = (_text(element, 'CATEGORY') or '').upper()
        if category in ('STRUCTURE', 'ARRAY', 'STRING', 'VALUE'):
            return self.datatype(element, name)  # Inline type definition
        target = self.resolve(element, 'TYPE-TREF')
        return self.datatype(target, name) if target is not None else None

    def parameters(self, prototypes: List[ElementTree.Element], name: str) -> Optional[Struct]:
        members: List[DataType] = []
        for prototype in prototypes:
            member_name = _text(prototype, 'SHORT-NAME') or 'arg'
            target = self.resolve(prototype, 'TYPE-TREF')
            if target is None:
                return None
            built = self.datatype(target, member_name)
            if built is None:
                return None
            members.append(built)
        return Struct(name, members)


def load_arxml(path: str | Path) -> SomeIpDescription:
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as e:
        raise ArxmlError(f'Invalid XML: {e}') from e
    reader = _Reader(root)
    deployments = list(reader.root.iter('SOMEIP-SERVICE-INTERFACE-DEPLOYMENT'))
    if not deployments:
        raise ArxmlError('No SOME/IP service interface deployments found (is this an ARXML with SOME/IP?)')
    description = SomeIpDescription()
    for deployment in deployments:
        service_id = _int(deployment, 'SERVICE-INTERFACE-ID')
        interface = reader.resolve(deployment, 'SERVICE-INTERFACE-REF')
        if service_id is None or interface is None:
            continue
        description.service_names[service_id] = _text(interface, 'SHORT-NAME') or ''
        _read_methods(reader, description, deployment, interface, service_id)
        _read_events(reader, description, deployment, interface, service_id)
    return description


def _read_methods(reader, description, deployment, interface, service_id) -> None:
    for method_deployment in deployment.findall('METHOD-DEPLOYMENTS/SOMEIP-METHOD-DEPLOYMENT'):
        method_id = _int(method_deployment, 'METHOD-ID')
        operation = reader.resolve(method_deployment, 'METHOD-REF')
        if method_id is None or operation is None:
            continue
        name = _text(operation, 'SHORT-NAME') or ''
        arguments = operation.findall('ARGUMENTS/ARGUMENT-DATA-PROTOTYPE')
        inputs = [arg for arg in arguments if (_text(arg, 'DIRECTION') or 'IN').upper() == 'IN']
        outputs = [arg for arg in arguments if (_text(arg, 'DIRECTION') or '').upper() == 'OUT']
        request = reader.parameters(inputs, name)
        response = reader.parameters(outputs, name)
        if request is not None:
            description.add(service_id, method_id, MessageType(name, request), is_response=False)
        if response is not None:
            description.add(service_id, method_id, MessageType(name, response), is_response=True)


def _read_events(reader, description, deployment, interface, service_id) -> None:
    for event_deployment in deployment.findall('EVENT-DEPLOYMENTS/SOMEIP-EVENT-DEPLOYMENT'):
        event_id = _int(event_deployment, 'EVENT-ID')
        prototype = reader.resolve(event_deployment, 'EVENT-REF')
        if event_id is None or prototype is None:
            continue
        name = _text(prototype, 'SHORT-NAME') or ''
        parameters = reader.parameters([prototype], name)
        if parameters is not None:
            description.add(service_id, event_id, MessageType(name, parameters), is_response=False)
