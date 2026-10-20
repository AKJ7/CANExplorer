"""ECU diagnostic descriptions in the ASAM MCD-2 D / ISO 22901 ODX format (.odx, .odx-d) and packaged ODX (.pdx).

Like the CandelaStudio reader (diagnostic_description.py, whose data model and field decoding this reuses), it
reads the ReadDataByIdentifier services of an ECU: the name of each data identifier and the layout of its data, so
the identifiers can be named and their responses decoded into fields.

A .pdx file is a ZIP of .odx-d documents (and a catalog); every ODX document in it is read and its identifiers
combined. Only ReadDataByIdentifier services (request service id 0x22) are read; a DID whose data uses a data
object property this reader cannot express (a structure, a variable length field, a multiplexer, …) is listed, and
its data shown as hex.

Supported data object properties are the ones with a fixed length DIAG-CODED-TYPE (unsigned, signed, IEEE float,
ASCII and UTF-8 text, in both byte orders) and an IDENTICAL, LINEAR or TEXTTABLE compu method (linear scaling and
value texts).
"""

import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree

from can_explorer.core.diagnostic_description import (
    BIG_ENDIAN,
    LITTLE_ENDIAN,
    DataIdentifier,
    DataType,
    DescriptionError,
    Field,
)

ODX_SUFFIXES = ('.odx', '.odx-d', '.pdx')
READ_DATA_BY_IDENTIFIER = 0x22

# ODX base data type -> the encoding of diagnostic_description.DataType (others are shown as hex)
ENCODINGS = {
    'A_UINT32': 'uns',
    'A_INT32': 'sgn',
    'A_FLOAT32': 'flt',
    'A_FLOAT64': 'flt',
    'A_ASCIISTRING': 'asc',
    'A_UTF8STRING': 'utf',
}


def _strip_namespaces(root: ElementTree.Element) -> ElementTree.Element:
    """ODX documents are normally without a default namespace; drop one if present so tags match directly."""
    for element in root.iter():
        if isinstance(element.tag, str) and '}' in element.tag:
            element.tag = element.tag.rsplit('}', 1)[1]
    return root


def _text(element: Optional[ElementTree.Element], path: str) -> Optional[str]:
    if element is None:
        return None
    value = element.findtext(path)
    return value.strip() if value and value.strip() else None


def _name(element: ElementTree.Element) -> Optional[str]:
    """The human readable name of an element: its long name, otherwise its short name."""
    return _text(element, 'LONG-NAME') or _text(element, 'SHORT-NAME')


def _xsi_type(element: ElementTree.Element) -> str:
    for key, value in element.attrib.items():
        if key.endswith('type'):  # xsi:type, with or without the namespace prefix resolved
            return value.rsplit(':', 1)[-1]
    return ''


class _Document:
    """One ODX document, with every element that carries an ID indexed for resolving the ID-REF links."""

    def __init__(self, root: ElementTree.Element):
        self.root = _strip_namespaces(root)
        self.index: Dict[str, ElementTree.Element] = {}
        for element in self.root.iter():
            identifier = element.get('ID')
            if identifier:
                self.index[identifier] = element


def _resolve(
    index: Dict[str, ElementTree.Element], element: Optional[ElementTree.Element]
) -> Optional[ElementTree.Element]:
    """The element a *-REF points to (by its ID-REF attribute)."""
    if element is None:
        return None
    return index.get(element.get('ID-REF', ''))


def _units(index: Dict[str, ElementTree.Element]) -> Dict[str, str]:
    units = {}
    for identifier, element in index.items():
        if element.tag == 'UNIT':
            units[identifier] = _text(element, 'DISPLAY-NAME') or _name(element) or ''
    return units


def _number(text: str) -> float:
    return float(text)


def _compu(dop: ElementTree.Element):
    """The scaling of a data object property: (factor, offset, choices), or None when it cannot be expressed as
    a linear scaling with value texts."""
    method = dop.find('COMPU-METHOD')
    category = _text(method, 'CATEGORY') if method is not None else 'IDENTICAL'
    if method is None or category == 'IDENTICAL':
        return 1.0, 0.0, ()
    scales = method.findall('COMPU-INTERNAL-TO-PHYS/COMPU-SCALES/COMPU-SCALE')
    if category in ('LINEAR', 'SCALE-LINEAR'):
        if len(scales) != 1:
            return None  # Several ranges with different scalings cannot be expressed
        coefficients = scales[0].find('COMPU-RATIONAL-COEFFS')
        if coefficients is None:
            return 1.0, 0.0, ()
        numerator = [_number(v.text) for v in coefficients.findall('COMPU-NUMERATOR/V') if v.text]
        denominator = [_number(v.text) for v in coefficients.findall('COMPU-DENOMINATOR/V') if v.text]
        divisor = denominator[0] if denominator else 1.0
        if len(numerator) > 2 or len(denominator) > 1 or divisor == 0 or not numerator:
            return None
        offset = numerator[0] / divisor
        factor = (numerator[1] / divisor) if len(numerator) == 2 else 0.0
        return factor, offset, ()
    if category == 'TEXTTABLE':
        choices = []
        for scale in scales:
            text = _text(scale, 'COMPU-CONST/VT')
            lower = scale.findtext('LOWER-LIMIT')
            upper = scale.findtext('UPPER-LIMIT')
            if text is None or lower is None:
                continue
            first = int(float(lower))
            last = int(float(upper)) if upper is not None else first
            choices.append((min(first, last), max(first, last), text))
        return 1.0, 0.0, tuple(choices)
    return None  # TAB-INTP, COMPUCODE, ...


def _data_type(units: Dict[str, str], dop: Optional[ElementTree.Element]):
    """The DataType of a data object property, or None when its data cannot be decoded (a structure, a variable
    length field, a multiplexer, an unsupported base type or scaling)."""
    if dop is None or dop.tag != 'DATA-OBJECT-PROP':
        return None
    coded = dop.find('DIAG-CODED-TYPE')
    if coded is None or _xsi_type(coded) not in ('', 'STANDARD-LENGTH-TYPE'):
        return None  # LEADING-LENGTH-INFO-TYPE, MIN-MAX-LENGTH-TYPE, PARAM-LENGTH-INFO-TYPE: variable length
    encoding = ENCODINGS.get(coded.get('BASE-DATA-TYPE', ''))
    bit_length = coded.findtext('BIT-LENGTH')
    if encoding is None or bit_length is None:
        return None
    high_low = coded.get('IS-HIGHLOW-BYTE-ORDER', _text(coded, 'IS-HIGHLOW-BYTE-ORDER') or 'true')
    scaling = _compu(dop)
    if scaling is None:
        return None
    factor, offset, choices = scaling
    return DataType(
        name=_name(dop) or 'value',
        bit_length=int(bit_length),
        encoding=encoding,
        byte_order=LITTLE_ENDIAN if str(high_low).lower() == 'false' else BIG_ENDIAN,
        factor=factor,
        offset=offset,
        unit=_unit_of(units, dop),
        choices=choices,
    )


def _unit_of(units: Dict[str, str], dop: ElementTree.Element) -> str:
    reference = dop.find('UNIT-REF')
    return units.get(reference.get('ID-REF', ''), '') if reference is not None else ''


def _coded_consts(request: ElementTree.Element):
    """The constant parameters of a request or response, by ascending byte position: (value, byte position, bytes)."""
    result = []
    for param in request.findall('PARAMS/PARAM'):
        if _xsi_type(param) != 'CODED-CONST':
            continue
        value = param.findtext('CODED-VALUE')
        position = param.findtext('BYTE-POSITION')
        if value is None or position is None:
            continue
        bit_length = param.findtext('DIAG-CODED-TYPE/BIT-LENGTH') or '8'
        result.append((int(value), int(position), (int(bit_length) + 7) // 8))
    return sorted(result, key=lambda item: item[1])


def _service_identifier(index: Dict[str, ElementTree.Element], service: ElementTree.Element) -> Optional[int]:
    """The data identifier a ReadDataByIdentifier service reads, or None for other services."""
    request = _resolve(index, service.find('REQUEST-REF'))
    if request is None:
        return None
    constants = _coded_consts(request)
    if len(constants) < 2 or constants[0][0] != READ_DATA_BY_IDENTIFIER:
        return None  # Only ReadDataByIdentifier; its service id is followed by the identifier
    return constants[1][0]


def _response_fields(index: Dict[str, ElementTree.Element], units: Dict[str, str], service: ElementTree.Element):
    """The fields of a service's positive response, relative to the start of the identifier's data, and whether
    they are all decodable."""
    response = _resolve(index, service.find('POS-RESPONSE-REFS/POS-RESPONSE-REF'))
    if response is None:
        return [], True
    header_end = max((position + size for _, position, size in _coded_consts(response)), default=0)
    fields: List[Field] = []
    complete = True
    for param in response.findall('PARAMS/PARAM'):
        if _xsi_type(param) not in ('VALUE', ''):
            continue
        reference = param.find('DOP-REF')
        if reference is None:
            continue
        data_type = _data_type(units, _resolve(index, reference))
        if data_type is None:
            complete = False
            continue
        position = param.findtext('BYTE-POSITION')
        if position is None:
            complete = False
            continue
        bit = int(param.findtext('BIT-POSITION') or 0)
        offset = (int(position) - header_end) * 8 + bit
        if offset < 0:
            complete = False
            continue
        fields.append(Field(_name(param) or data_type.name, data_type, offset))
    return fields, complete


def _read_documents(path: Path) -> List[_Document]:
    if path.suffix.lower() == '.pdx':
        documents = []
        try:
            with zipfile.ZipFile(path) as archive:
                members = [name for name in archive.namelist() if name.lower().endswith(('.odx', '.odx-d'))]
                if not members:
                    raise DescriptionError('The PDX archive contains no ODX documents')
                for name in members:
                    try:
                        documents.append(_Document(ElementTree.fromstring(archive.read(name))))
                    except ElementTree.ParseError as e:
                        raise DescriptionError(f'Invalid ODX document {name} in the PDX: {e}') from e
        except zipfile.BadZipFile as e:
            raise DescriptionError(f'Not a PDX archive: {e}') from e
        return documents
    try:
        return [_Document(ElementTree.parse(path).getroot())]
    except ElementTree.ParseError as e:
        raise DescriptionError(f'Invalid XML: {e}') from e


def load_odx(path: str | Path) -> List[DataIdentifier]:
    documents = _read_documents(Path(path))
    if not any(document.root.tag in ('ODX', 'ODX-F') for document in documents):
        raise DescriptionError('Not an ODX description: no ODX root element')
    identifiers: Dict[int, DataIdentifier] = {}
    for document in documents:
        index = document.index
        units = _units(index)
        for service in document.root.iter('DIAG-SERVICE'):
            identifier = _service_identifier(index, service)
            if identifier is None or identifier in identifiers:
                continue
            fields, complete = _response_fields(index, units, service)
            did = DataIdentifier(identifier, _name(service) or f'DID_{identifier:04X}', fields, complete)
            identifiers[identifier] = did
    return list(identifiers.values())
