"""A minimal reader for A2L files (ASAM MCD-2 MC): the measurements and characteristics an ECU exposes, with their
memory address, data type and linear conversion, so XCP/CCP values can be shown and set in physical units by name.

Only what measurement and calibration by polling needs is read: MEASUREMENT and (scalar VALUE) CHARACTERISTIC
blocks, their RECORD_LAYOUT for the data type, and COMPU_METHOD for an IDENTICAL/LINEAR/RAT_FUNC (linear) scaling.
Curves, maps, verbal tables and formulae are skipped.
"""

from dataclasses import dataclass
from pathlib import Path
from struct import pack, unpack
from typing import Dict, List, Optional, Tuple

# A2L data type -> (size in bytes, signed, float)
DATA_TYPES = {
    'UBYTE': (1, False, False),
    'SBYTE': (1, True, False),
    'BYTE': (1, False, False),
    'UWORD': (2, False, False),
    'SWORD': (2, True, False),
    'WORD': (2, False, False),
    'ULONG': (4, False, False),
    'SLONG': (4, True, False),
    'LONG': (4, True, False),
    'A_UINT64': (8, False, False),
    'A_INT64': (8, True, False),
    'FLOAT16_IEEE': (2, False, True),
    'FLOAT32_IEEE': (4, False, True),
    'FLOAT64_IEEE': (8, False, True),
}


class A2lError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class A2lVariable:
    name: str
    description: str
    address: int
    data_type: str
    writable: bool  # A CHARACTERISTIC can be written; a MEASUREMENT is read-only
    factor: float = 1.0
    offset: float = 0.0
    unit: str = ''
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    @property
    def size(self) -> int:
        return DATA_TYPES.get(self.data_type, (1, False, False))[0]

    @property
    def is_float(self) -> bool:
        return DATA_TYPES.get(self.data_type, (1, False, False))[2]

    def decode(self, data: bytes, byte_order: str = 'little') -> float:
        """Physical value from the raw bytes read from the ECU."""
        size, signed, is_float = DATA_TYPES.get(self.data_type, (1, False, False))
        data = bytes(data[:size]).ljust(size, b'\x00')
        if is_float:
            fmt = {2: 'e', 4: 'f', 8: 'd'}[size]
            raw = unpack(('>' if byte_order == 'big' else '<') + fmt, data)[0]
        else:
            raw = int.from_bytes(data, byte_order, signed=signed)
        return raw * self.factor + self.offset

    def encode(self, value: float, byte_order: str = 'little') -> bytes:
        """Raw bytes to write for a physical value (rounded and clamped to the type)."""
        size, signed, is_float = DATA_TYPES.get(self.data_type, (1, False, False))
        raw = (value - self.offset) / self.factor if self.factor else value
        if is_float:
            fmt = {2: 'e', 4: 'f', 8: 'd'}[size]
            return pack(('>' if byte_order == 'big' else '<') + fmt, float(raw))
        low = -(1 << (size * 8 - 1)) if signed else 0
        high = (1 << (size * 8 - 1)) - 1 if signed else (1 << (size * 8)) - 1
        return int(max(low, min(high, round(raw)))).to_bytes(size, byte_order, signed=signed)


def _tokenise(text: str) -> List[str]:
    tokens: List[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char in ' \t\r\n':
            index += 1
        elif text[index : index + 2] == '/*':
            end = text.find('*/', index + 2)
            index = end + 2 if end >= 0 else length
        elif text[index : index + 2] == '//':
            end = text.find('\n', index + 2)
            index = end + 1 if end >= 0 else length
        elif char == '"':
            buffer, index = [], index + 1
            while index < length and text[index] != '"':
                if text[index] == '\\' and index + 1 < length:
                    buffer.append(text[index + 1])
                    index += 2
                else:
                    buffer.append(text[index])
                    index += 1
            tokens.append('"' + ''.join(buffer))  # A leading quote marks a string token
            index += 1
        else:
            start = index
            while index < length and text[index] not in ' \t\r\n':
                index += 1
            tokens.append(text[start:index])
    return tokens


def _value(token: str) -> str:
    return token[1:] if token.startswith('"') else token


def _number(token: str) -> float:
    token = _value(token)
    return float(int(token, 16)) if token.lower().startswith('0x') else float(token)


def _integer(token: str) -> int:
    token = _value(token)
    return int(token, 16) if token.lower().startswith('0x') else int(token)


def _blocks(tokens: List[str]):
    """Yield (block_type, inner_tokens) for every top-level and nested /begin … /end block."""
    index = 0
    while index < len(tokens):
        if tokens[index] == '/begin' and index + 1 < len(tokens):
            block_type = tokens[index + 1]
            depth, end = 1, index + 2
            while end < len(tokens) and depth:
                if tokens[end] == '/begin':
                    depth += 1
                elif tokens[end] == '/end':
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            inner = tokens[index + 2 : end]
            yield block_type, inner
            yield from _blocks(inner)  # Descend into nested blocks (MODULE holds the rest)
            index = end + 2  # Skip '/end' and the repeated block type
        else:
            index += 1


def _keyword(inner: List[str], keyword: str) -> int:
    depth = 0
    for position, token in enumerate(inner):
        if token == '/begin':
            depth += 1
        elif token == '/end':
            depth -= 1
        elif depth == 0 and token == keyword:
            return position
    return -1


def _parse_compu_method(inner: List[str]) -> Tuple[str, float, float, str]:
    """(name, factor, offset, unit) for an IDENTICAL/LINEAR/RAT_FUNC (linear) conversion."""
    name = inner[0]
    conversion = inner[2] if len(inner) > 2 else 'IDENTICAL'
    unit = _value(inner[4]) if len(inner) > 4 else ''
    factor, offset = 1.0, 0.0
    position = _keyword(inner, 'COEFFS_LINEAR')
    if conversion == 'LINEAR' and position >= 0:
        factor, offset = _number(inner[position + 1]), _number(inner[position + 2])
    position = _keyword(inner, 'COEFFS')
    if conversion == 'RAT_FUNC' and position >= 0:
        # int = (a*phys^2 + b*phys + c) / (d*phys^2 + e*phys + f); the linear case a=d=e=0 gives phys = (f*int - c)/b
        a, b, c, d, e, f = (_number(inner[position + offset]) for offset in range(1, 7))
        if a == 0 and d == 0 and e == 0 and b != 0:
            factor, offset = f / b, -c / b
    return name, factor, offset, unit


def _parse_record_layout(inner: List[str]) -> Tuple[str, Optional[str]]:
    """(name, data type) from the FNC_VALUES of a record layout."""
    name = inner[0]
    position = _keyword(inner, 'FNC_VALUES')
    data_type = inner[position + 2] if 0 <= position and position + 2 < len(inner) else None
    return name, data_type


def parse_a2l(text: str) -> List[A2lVariable]:
    """The measurements and scalar characteristics of an A2L file, by name."""
    tokens = _tokenise(text)
    compu: Dict[str, Tuple[float, float, str]] = {}
    layouts: Dict[str, Optional[str]] = {}
    measurements: List[List[str]] = []
    characteristics: List[List[str]] = []
    for block_type, inner in _blocks(tokens):
        if block_type == 'COMPU_METHOD' and inner:
            name, factor, offset, unit = _parse_compu_method(inner)
            compu[name] = (factor, offset, unit)
        elif block_type == 'RECORD_LAYOUT' and inner:
            name, data_type = _parse_record_layout(inner)
            layouts[name] = data_type
        elif block_type == 'MEASUREMENT':
            measurements.append(inner)
        elif block_type == 'CHARACTERISTIC':
            characteristics.append(inner)

    def conversion(name: str) -> Tuple[float, float, str]:
        return compu.get(name, (1.0, 0.0, ''))

    variables: List[A2lVariable] = []
    for inner in measurements:
        if len(inner) < 8:
            continue
        name, description, data_type, compu_name = inner[0], _value(inner[1]), inner[2], inner[3]
        if data_type not in DATA_TYPES:
            continue
        address_at = _keyword(inner, 'ECU_ADDRESS')
        if address_at < 0:
            continue  # No address: not readable over XCP/CCP by itself
        factor, offset, unit = conversion(compu_name)
        try:
            minimum, maximum = _number(inner[6]), _number(inner[7])
        except ValueError:
            minimum = maximum = None
        variables.append(
            A2lVariable(
                name,
                description,
                _integer(inner[address_at + 1]),
                data_type,
                False,
                factor,
                offset,
                unit,
                minimum,
                maximum,
            )
        )
    for inner in characteristics:
        if len(inner) < 9 or inner[2] != 'VALUE':
            continue  # Only scalar values (not curves, maps, …)
        name, description = inner[0], _value(inner[1])
        data_type = layouts.get(inner[4])
        if data_type not in DATA_TYPES:
            continue
        factor, offset, unit = conversion(inner[6])
        try:
            minimum, maximum = _number(inner[7]), _number(inner[8])
        except ValueError:
            minimum = maximum = None
        variables.append(
            A2lVariable(name, description, _integer(inner[3]), data_type, True, factor, offset, unit, minimum, maximum)
        )
    return variables


def read_a2l(path: str | Path) -> List[A2lVariable]:
    return parse_a2l(Path(path).read_text(encoding='utf-8', errors='replace'))
