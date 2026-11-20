"""A reader for A2L files (ASAM MCD-2 MC): the measurements and characteristics an ECU exposes — memory address,
data type, layout and conversion — and the interface settings of its XCP or CCP slave, so values can be measured
and calibrated by name in physical units.

What is read:
- MEASUREMENT (scalars and arrays: ARRAY_SIZE, MATRIX_DIM), CHARACTERISTIC of the types VALUE, VAL_BLK, ASCII,
  CURVE and MAP, and AXIS_PTS; with ECU_ADDRESS_EXTENSION, BIT_MASK, BYTE_ORDER, NUMBER, READ_ONLY/READ_WRITE,
  PHYS_UNIT.
- AXIS_DESCR of the attributes STD_AXIS, FIX_AXIS (FIX_AXIS_PAR, _PAR_DIST, _PAR_LIST), COM_AXIS, RES_AXIS and
  CURVE_AXIS (an AXIS_PTS_REF / CURVE_AXIS_REF, read as a common axis).
- RECORD_LAYOUT items FNC_VALUES (ROW_DIR, COLUMN_DIR), AXIS_PTS_X/Y (INDEX_INCR, INDEX_DECR), NO_AXIS_PTS_X/Y,
  FIX_NO_AXIS_PTS_X/Y, RESERVED and ALIGNMENT_*; other positioned items (SRC_ADDR, RIP_ADDR, OFFSET, SHIFT_OP,
  DIST_OP, IDENTIFICATION) are skipped by their size. Without ALIGNMENT_* (here or in MOD_COMMON) items are packed.
- COMPU_METHOD IDENTICAL, LINEAR, RAT_FUNC (also non-linear), TAB_INTP, TAB_NOINTP, TAB_VERB (COMPU_VTAB and
  COMPU_VTAB_RANGE) and FORM (FORMULA / FORMULA_INV, evaluated without running arbitrary code).
- MOD_COMMON BYTE_ORDER and ALIGNMENT_*, MOD_PAR EPK / ADDR_EPK, `/include` files.
- IF_DATA XCP / XCPplus: PROTOCOL_LAYER (timeouts, MAX_CTO/DTO, byte order, address granularity), DAQ (type,
  events, timestamps) and the XCP_ON_CAN (identifiers, bit rate, CAN FD), XCP_ON_UDP_IP and XCP_ON_TCP_IP
  transport layers. IF_DATA ASAP1B_CCP: the TP_BLOB identifiers and station address, rasters and DAQ lists.

Not read: pointer addressing (PBYTE/PWORD/PLONG), ALTERNATE_* index modes, AXIS_RESCALE, CUBOID and higher
characteristics, DEPOSIT DIFFERENCE axes, the TP_BLOB byte-order code (its value is kept raw, unmapped).
"""

import ast
import math
import operator
from dataclasses import dataclass, field
from pathlib import Path
from struct import pack, unpack
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

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
DATA_SIZES = {'BYTE': 1, 'WORD': 2, 'LONG': 4}  # RESERVED item sizes
BYTE_ORDERS = {'MSB_FIRST': 'big', 'BIG_ENDIAN': 'big', 'MSB_LAST': 'little', 'LITTLE_ENDIAN': 'little'}
ALIGNMENT_KEYWORDS = {
    'ALIGNMENT_BYTE': 1,
    'ALIGNMENT_WORD': 2,
    'ALIGNMENT_LONG': 4,
    'ALIGNMENT_INT64': 8,
    'ALIGNMENT_FLOAT16_IEEE': 2,
    'ALIGNMENT_FLOAT32_IEEE': 4,
    'ALIGNMENT_FLOAT64_IEEE': 8,
}
# Time units of IF_DATA timestamps and events, in seconds
TIME_UNITS = {
    'UNIT_1NS': 1e-9,
    'UNIT_10NS': 1e-8,
    'UNIT_100NS': 1e-7,
    'UNIT_1US': 1e-6,
    'UNIT_10US': 1e-5,
    'UNIT_100US': 1e-4,
    'UNIT_1MS': 1e-3,
    'UNIT_10MS': 1e-2,
    'UNIT_100MS': 1e-1,
    'UNIT_1S': 1.0,
    'UNIT_1PS': 1e-12,
    'UNIT_10PS': 1e-11,
    'UNIT_100PS': 1e-10,
}
EVENT_TIME_UNITS = {0: 1e-9, 1: 1e-8, 2: 1e-7, 3: 1e-6, 4: 1e-5, 5: 1e-4, 6: 1e-3, 7: 1e-2, 8: 1e-1, 9: 1.0}

Physical = Union[float, str]


class A2lError(Exception):
    pass


# Tokens and blocks


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
            while index < length:
                if text[index] == '\\' and index + 1 < length:
                    buffer.append(text[index + 1])
                    index += 2
                elif text[index] == '"' and text[index + 1 : index + 2] == '"':  # A doubled quote inside a string
                    buffer.append('"')
                    index += 2
                elif text[index] == '"':
                    break
                else:
                    buffer.append(text[index])
                    index += 1
            tokens.append('"' + ''.join(buffer))  # A leading quote marks a string token
            index += 1
        else:
            start = index
            while index < length and text[index] not in ' \t\r\n"':
                index += 1
            tokens.append(text[start:index])
    return tokens


def _tokenise_file(path: Path, seen: Optional[set] = None) -> List[str]:
    """The tokens of a file, with every `/include "file"` replaced by the included file's tokens."""
    seen = set() if seen is None else seen
    resolved = path.resolve()
    if resolved in seen:
        raise A2lError(f'{path.name} includes itself')
    seen = seen | {resolved}
    tokens = _tokenise(path.read_text(encoding='utf-8', errors='replace'))
    return _expand_includes(tokens, path.parent, seen)


def _expand_includes(tokens: List[str], base: Optional[Path], seen: Optional[set] = None) -> List[str]:
    if '/include' not in tokens:
        return tokens
    result: List[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index] == '/include' and index + 1 < len(tokens):
            name = _value(tokens[index + 1])
            included = Path(name)
            if not included.is_absolute() and base is not None:
                included = base / included
            if not included.exists():
                raise A2lError(f'Included file not found: {name}')
            result += _tokenise_file(included, seen)
            index += 2
        else:
            result.append(tokens[index])
            index += 1
    return result


def _value(token: str) -> str:
    return token[1:] if token.startswith('"') else token


def _is_number(token: str) -> bool:
    try:
        _number(token)
        return not token.startswith('"')
    except ValueError:
        return False


def _number(token: str) -> float:
    token = _value(token)
    return float(int(token, 16)) if token.lower().startswith(('0x', '-0x')) else float(token)


def _integer(token: str) -> int:
    token = _value(token)
    return int(token, 16) if token.lower().startswith(('0x', '-0x')) else int(float(token))


@dataclass
class Node:
    """A /begin … /end block: its own tokens (in order, without the nested blocks) and the nested blocks."""

    kind: str
    tokens: List[str] = field(default_factory=list)
    children: List['Node'] = field(default_factory=list)

    def find(self, kind: str) -> Optional['Node']:
        return next((child for child in self.children if child.kind == kind), None)

    def all(self, kind: str) -> List['Node']:
        return [child for child in self.children if child.kind == kind]

    def walk(self, kind: str) -> Iterator['Node']:
        for child in self.children:
            if child.kind == kind:
                yield child
            yield from child.walk(kind)

    def keyword(self, name: str, count: int = 1) -> Optional[List[str]]:
        """The `count` tokens after a keyword of this block (not of nested blocks)."""
        for index, token in enumerate(self.tokens):
            if token == name:
                return self.tokens[index + 1 : index + 1 + count]
        return None

    def has(self, name: str) -> bool:
        return name in self.tokens

    def numbers_after(self, name: str) -> List[float]:
        """The numbers following a keyword, e.g. the dimensions of MATRIX_DIM."""
        for index, token in enumerate(self.tokens):
            if token == name:
                values = []
                for following in self.tokens[index + 1 :]:
                    if not _is_number(following):
                        break
                    values.append(_number(following))
                return values
        return []


def parse_tree(tokens: Sequence[str]) -> Node:
    root = Node('ROOT')
    stack = [root]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == '/begin' and index + 1 < len(tokens):
            node = Node(tokens[index + 1])
            stack[-1].children.append(node)
            stack.append(node)
            index += 2
            if node.kind == 'A2ML':  # The A2ML grammar is not A2L: skip it whole
                depth = 1
                while index < len(tokens) and depth:
                    if tokens[index] == '/begin':
                        depth += 1
                    elif tokens[index] == '/end':
                        depth -= 1
                    index += 1
                stack.pop()
                index += 1  # The repeated block type
            continue
        if token == '/end':
            if len(stack) > 1:
                stack.pop()
            index += 2  # Skip the repeated block type
            continue
        stack[-1].tokens.append(token)
        index += 1
    return root


# Conversions

_FUNCTIONS = {
    name: getattr(math, name)
    for name in ('sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'sinh', 'cosh', 'tanh', 'exp', 'sqrt', 'log10')
}
_FUNCTIONS.update(log=math.log, abs=abs, pow=pow, min=min, max=max, floor=math.floor, ceil=math.ceil)
_FUNCTIONS.update(arcsin=math.asin, arccos=math.acos, arctan=math.atan)
_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: lambda a, b: int(a) & int(b),
    ast.BitOr: lambda a, b: int(a) | int(b),
    ast.BitXor: lambda a, b: int(a) ^ int(b),
    ast.LShift: lambda a, b: int(a) << int(b),
    ast.RShift: lambda a, b: int(a) >> int(b),
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Invert: lambda a: ~int(a),
    ast.Not: lambda a: float(not a),
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


def _formula_to_python(formula: str) -> str:
    text = formula.replace('&&', ' and ').replace('||', ' or ')
    text = text.replace('!=', '\0').replace('!', ' not ').replace('\0', '!=')
    return text


def evaluate_formula(formula: str, x: float) -> float:
    """Evaluate an A2L FORMULA with X1 (or X) = x: arithmetic, comparisons, bit operators and the usual math
    functions, nothing else."""
    try:
        tree = ast.parse(_formula_to_python(formula).strip(), mode='eval')
    except SyntaxError as e:
        raise A2lError(f'Formula {formula!r}: {e.msg}') from None

    def run(node) -> float:
        if isinstance(node, ast.Expression):
            return run(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id.upper() in ('X', 'X1'):
                return x
            if node.id == 'pi':
                return math.pi
            raise A2lError(f'Formula {formula!r}: unknown name {node.id}')
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](run(node.left), run(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](run(node.operand))
        if isinstance(node, ast.BoolOp):
            values = [run(value) for value in node.values]
            return float(all(values) if isinstance(node.op, ast.And) else any(values))
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _OPERATORS:
            return float(_OPERATORS[type(node.ops[0])](run(node.left), run(node.comparators[0])))
        if isinstance(node, ast.IfExp):
            return run(node.body) if run(node.test) else run(node.orelse)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS:
            return _FUNCTIONS[node.func.id](*(run(arg) for arg in node.args))
        raise A2lError(f'Formula {formula!r}: unsupported expression')

    try:
        return float(run(tree))
    except (ArithmeticError, ValueError, TypeError) as e:
        raise A2lError(f'Formula {formula!r} at X={x:g}: {e}') from None


@dataclass(frozen=True)
class Conversion:
    """A COMPU_METHOD: raw <-> physical."""

    name: str = ''
    kind: str = 'IDENTICAL'
    coeffs: Tuple[float, ...] = ()
    table: Tuple[Tuple[float, float], ...] = ()  # TAB_INTP / TAB_NOINTP: (raw, physical), by raw
    verbal: Tuple[Tuple[float, float, str], ...] = ()  # TAB_VERB: (raw low, raw high, text)
    default: Optional[Physical] = None
    formula: str = ''
    inverse: str = ''
    unit: str = ''

    @property
    def is_verbal(self) -> bool:
        return self.kind == 'TAB_VERB'

    @property
    def linear(self) -> Optional[Tuple[float, float]]:
        """(factor, offset) of a linear conversion (physical = raw * factor + offset), else None."""
        if self.kind == 'IDENTICAL':
            return 1.0, 0.0
        if self.kind == 'LINEAR' and len(self.coeffs) == 2:
            return self.coeffs
        if self.kind == 'RAT_FUNC' and len(self.coeffs) == 6:
            a, b, c, d, e, f = self.coeffs
            if a == 0 and d == 0 and e == 0 and b != 0:
                return f / b, -c / b
        return None

    def to_physical(self, raw: float) -> Physical:
        if self.kind == 'IDENTICAL' or (self.kind in ('LINEAR', 'RAT_FUNC') and not self.coeffs):
            return float(raw)
        if self.kind == 'LINEAR':
            return self.coeffs[0] * raw + self.coeffs[1]
        if self.kind == 'RAT_FUNC':
            a, b, c, d, e, f = self.coeffs
            # raw = (a p² + b p + c) / (d p² + e p + f)  =>  (a - raw d) p² + (b - raw e) p + (c - raw f) = 0
            qa, qb, qc = a - raw * d, b - raw * e, c - raw * f
            if qa == 0:
                if qb == 0:
                    raise A2lError(f'{self.name}: raw value {raw:g} has no physical value')
                return -qc / qb
            discriminant = qb * qb - 4 * qa * qc
            if discriminant < 0:
                raise A2lError(f'{self.name}: raw value {raw:g} has no physical value')
            return (-qb + math.sqrt(discriminant)) / (2 * qa)
        if self.kind == 'TAB_INTP':
            return self._interpolate(raw, self.table)
        if self.kind == 'TAB_NOINTP':
            for entry_raw, physical in self.table:
                if entry_raw == raw:
                    return physical
            if isinstance(self.default, (int, float)):
                return float(self.default)
            return min(self.table, key=lambda entry: abs(entry[0] - raw))[1] if self.table else float(raw)
        if self.kind == 'TAB_VERB':
            for low, high, text in self.verbal:
                if low <= raw <= high:
                    return text
            return self.default if isinstance(self.default, str) else f'{raw:g}'
        if self.kind == 'FORM':
            return evaluate_formula(self.formula, raw)
        return float(raw)

    def to_raw(self, value: Physical) -> float:
        if self.kind == 'TAB_VERB':
            if isinstance(value, str):
                for low, _high, text in self.verbal:
                    if text == value:
                        return low
                for low, _high, text in self.verbal:
                    if text.lower() == value.strip().lower():
                        return low
                try:
                    return float(value)
                except ValueError:
                    choices = ', '.join(text for _, _, text in self.verbal)
                    raise A2lError(f'{self.name}: {value!r} is not one of {choices}') from None
            return float(value)
        value = float(value)
        if self.kind == 'IDENTICAL' or (self.kind in ('LINEAR', 'RAT_FUNC') and not self.coeffs):
            return value
        if self.kind == 'LINEAR':
            factor, offset = self.coeffs
            if factor == 0:
                raise A2lError(f'{self.name}: the factor is 0')
            return (value - offset) / factor
        if self.kind == 'RAT_FUNC':
            a, b, c, d, e, f = self.coeffs
            denominator = d * value * value + e * value + f
            if denominator == 0:
                raise A2lError(f'{self.name}: {value:g} has no raw value')
            return (a * value * value + b * value + c) / denominator
        if self.kind == 'TAB_INTP':
            return self._interpolate(value, tuple(sorted((p, r) for r, p in self.table)))
        if self.kind == 'TAB_NOINTP':
            if not self.table:
                return value
            return min(self.table, key=lambda entry: abs(entry[1] - value))[0]
        if self.kind == 'FORM':
            if not self.inverse:
                raise A2lError(f'{self.name}: the formula has no FORMULA_INV, so values cannot be written')
            return evaluate_formula(self.inverse, value)
        return value

    @staticmethod
    def _interpolate(x: float, table: Sequence[Tuple[float, float]]) -> float:
        if not table:
            return x
        if x <= table[0][0]:
            return table[0][1]
        if x >= table[-1][0]:
            return table[-1][1]
        for (x0, y0), (x1, y1) in zip(table, table[1:], strict=False):
            if x0 <= x <= x1:
                return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return table[-1][1]


IDENTICAL = Conversion()


# Layouts and axes


@dataclass(frozen=True)
class LayoutItem:
    position: int
    kind: str  # FNC_VALUES, AXIS_PTS_X, AXIS_PTS_Y, NO_AXIS_PTS_X, NO_AXIS_PTS_Y, RESERVED, or another sized item
    data_type: str = ''
    size: int = 0  # Bytes of one element
    index_mode: str = ''  # FNC_VALUES: ROW_DIR / COLUMN_DIR …; AXIS_PTS: INDEX_INCR / INDEX_DECR
    addressing: str = 'DIRECT'


@dataclass(frozen=True)
class RecordLayout:
    name: str
    items: Tuple[LayoutItem, ...] = ()
    fixed_points: Tuple[Tuple[str, int], ...] = ()  # FIX_NO_AXIS_PTS_X/Y: (axis, count)
    alignments: Tuple[Tuple[int, int], ...] = ()  # (element size, alignment)
    unsupported: str = ''

    def item(self, kind: str) -> Optional[LayoutItem]:
        return next((item for item in self.items if item.kind == kind), None)

    def fixed(self, axis: str) -> Optional[int]:
        return dict(self.fixed_points).get(axis)

    @property
    def values(self) -> Optional[LayoutItem]:
        return self.item('FNC_VALUES')


@dataclass(frozen=True)
class Axis:
    """An AXIS_DESCR of a CURVE or MAP."""

    attribute: str  # STD_AXIS, FIX_AXIS, COM_AXIS, RES_AXIS, CURVE_AXIS
    input_quantity: str
    conversion: Conversion
    max_points: int
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    reference: str = ''  # AXIS_PTS_REF / CURVE_AXIS_REF
    fixed: Tuple[float, ...] = ()  # The points of a FIX_AXIS (raw)
    unit: str = ''


@dataclass(frozen=True)
class A2lVariable:
    """A MEASUREMENT, CHARACTERISTIC or AXIS_PTS."""

    name: str
    description: str
    address: int
    data_type: str
    writable: bool  # A CHARACTERISTIC can be written unless READ_ONLY; a MEASUREMENT only with READ_WRITE
    factor: float = 1.0
    offset: float = 0.0
    unit: str = ''
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    extension: int = 0
    byte_order: Optional[str] = None  # None: the module's (MOD_COMMON) or the protocol's
    bit_mask: Optional[int] = None
    kind: str = 'MEASUREMENT'  # MEASUREMENT, VALUE, VAL_BLK, ASCII, CURVE, MAP, AXIS_PTS
    shape: Tuple[int, ...] = ()  # Array dimensions of a MEASUREMENT or VAL_BLK
    conversion: Optional[Conversion] = None
    layout: Optional[RecordLayout] = None
    axes: Tuple[Axis, ...] = ()
    max_points: int = 0  # AXIS_PTS
    unsupported: str = ''  # Why the value cannot be read (it is still listed)

    @property
    def element_size(self) -> int:
        return DATA_TYPES.get(self.data_type, (1, False, False))[0]

    @property
    def count(self) -> int:
        return math.prod(self.shape) if self.shape else 1

    @property
    def size(self) -> int:
        """Bytes of a measurement, value or block (for a curve, map or axis: of the values only, at most)."""
        if self.kind == 'ASCII':
            return self.count
        return self.element_size * self.count

    @property
    def is_scalar(self) -> bool:
        return self.kind in ('MEASUREMENT', 'VALUE') and not self.shape and not self.unsupported

    @property
    def is_float(self) -> bool:
        return DATA_TYPES.get(self.data_type, (1, False, False))[2]

    @property
    def compu(self) -> Conversion:
        if self.conversion is not None:
            return self.conversion
        return Conversion(kind='LINEAR', coeffs=(self.factor, self.offset), unit=self.unit)

    def order(self, byte_order: str = 'little') -> str:
        return self.byte_order or byte_order

    def raw_value(self, data: bytes, byte_order: str = 'little') -> float:
        """The raw number of one element, after the bit mask."""
        return decode_raw(self.data_type, data, self.order(byte_order), self.bit_mask)

    def decode(self, data: bytes, byte_order: str = 'little') -> Physical:
        """Physical value of a scalar from the raw bytes read from the ECU (a str for a verbal conversion)."""
        return self.compu.to_physical(self.raw_value(data, byte_order))

    def decode_values(self, data: bytes, byte_order: str = 'little') -> List[Physical]:
        """Physical values of every element of an array or block."""
        size = self.element_size
        return [self.decode(data[i * size : (i + 1) * size], byte_order) for i in range(len(data) // size)]

    def check_limits(self, value: Physical) -> None:
        if isinstance(value, str):
            return
        if self.minimum is not None and value < self.minimum - 1e-9 * max(1.0, abs(self.minimum)):
            raise A2lError(f'{self.name}: {value:g} is below its minimum {self.minimum:g}')
        if self.maximum is not None and value > self.maximum + 1e-9 * max(1.0, abs(self.maximum)):
            raise A2lError(f'{self.name}: {value:g} is above its maximum {self.maximum:g}')

    def encode(self, value: Physical, byte_order: str = 'little', current: Optional[bytes] = None) -> bytes:
        """Raw bytes to write for a physical value (rounded and clamped to the type). With a bit mask, the other
        bits come from `current` (the bytes read from the ECU)."""
        raw = self.compu.to_raw(value)
        return encode_raw(self.data_type, raw, self.order(byte_order), self.bit_mask, current)


def decode_raw(data_type: str, data: bytes, byte_order: str, bit_mask: Optional[int] = None) -> float:
    size, signed, is_float = DATA_TYPES.get(data_type, (1, False, False))
    data = bytes(data[:size]).ljust(size, b'\x00')
    if is_float:
        return unpack(('>' if byte_order == 'big' else '<') + {2: 'e', 4: 'f', 8: 'd'}[size], data)[0]
    if bit_mask:
        value = int.from_bytes(data, byte_order) & bit_mask
        shift = (bit_mask & -bit_mask).bit_length() - 1
        value >>= shift
        width = (bit_mask >> shift).bit_length()
        if signed and value & (1 << (width - 1)):
            value -= 1 << width
        return float(value)
    return float(int.from_bytes(data, byte_order, signed=signed))


def encode_raw(
    data_type: str, raw: float, byte_order: str, bit_mask: Optional[int] = None, current: Optional[bytes] = None
) -> bytes:
    size, signed, is_float = DATA_TYPES.get(data_type, (1, False, False))
    if is_float:
        return pack(('>' if byte_order == 'big' else '<') + {2: 'e', 4: 'f', 8: 'd'}[size], float(raw))
    if bit_mask:
        shift = (bit_mask & -bit_mask).bit_length() - 1
        width = (bit_mask >> shift).bit_length()
        low, high = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if signed else (0, (1 << width) - 1)
        field_value = int(max(low, min(high, round(raw)))) & ((1 << width) - 1)
        base = int.from_bytes(bytes(current or bytes(size))[:size].ljust(size, b'\x00'), byte_order)
        value = (base & ~bit_mask & ((1 << (8 * size)) - 1)) | ((field_value << shift) & bit_mask)
        return value.to_bytes(size, byte_order)
    low = -(1 << (size * 8 - 1)) if signed else 0
    high = (1 << (size * 8 - 1)) - 1 if signed else (1 << (size * 8)) - 1
    return int(max(low, min(high, round(raw)))).to_bytes(size, byte_order, signed=signed)


# Interface settings (IF_DATA)


@dataclass(frozen=True)
class A2lEvent:
    name: str
    short_name: str
    channel: int
    cycle: float  # Seconds, 0 for a not cyclic event
    priority: int = 0


@dataclass(frozen=True)
class XcpTransportLayer:
    kind: str  # CAN, UDP, TCP
    master_id: Optional[int] = None  # CAN: the command identifier
    slave_id: Optional[int] = None  # CAN: the response identifier
    extended: bool = False
    baudrate: Optional[int] = None
    fd: bool = False
    max_dlc: int = 8
    data_baudrate: Optional[int] = None
    daq_ids: Tuple[Tuple[int, int], ...] = ()  # CAN: (DAQ list, identifier) of DAQ_LIST_CAN_ID
    host: str = ''
    port: Optional[int] = None


@dataclass(frozen=True)
class XcpSettings:
    timeouts: Tuple[float, ...] = ()  # T1..T7, seconds
    max_cto: Optional[int] = None
    max_dto: Optional[int] = None
    byte_order: Optional[str] = None
    address_granularity: Optional[int] = None
    daq_dynamic: Optional[bool] = None
    max_daq: Optional[int] = None
    events: Tuple[A2lEvent, ...] = ()
    timestamp_size: int = 0
    timestamp_resolution: float = 0.0
    transports: Tuple[XcpTransportLayer, ...] = ()

    def transport(self, kind: str) -> Optional[XcpTransportLayer]:
        return next((t for t in self.transports if t.kind == kind), None)


@dataclass(frozen=True)
class CcpSettings:
    cro_id: Optional[int] = None
    dto_id: Optional[int] = None
    extended: bool = False
    station_address: Optional[int] = None
    byte_order_code: Optional[int] = None  # The TP_BLOB byte-order value, unmapped
    version: Optional[int] = None
    baudrate: Optional[int] = None
    rasters: Tuple[Tuple[str, int], ...] = ()  # (name, event channel)
    daq_lists: Tuple[Tuple[int, int, Optional[int], Optional[int], Optional[int]], ...] = ()
    # ^ (number, length, CAN id, first PID, raster)


@dataclass
class A2lDocument:
    path: str = ''
    project: str = ''
    module: str = ''
    variables: List[A2lVariable] = field(default_factory=list)
    byte_order: Optional[str] = None  # MOD_COMMON BYTE_ORDER
    epk: str = ''
    epk_address: Optional[int] = None
    xcp: Optional[XcpSettings] = None
    ccp: Optional[CcpSettings] = None

    def variable(self, name: str) -> Optional[A2lVariable]:
        return self._by_name().get(name)

    def _by_name(self) -> Dict[str, A2lVariable]:
        cache = getattr(self, '_names', None)
        if cache is None or len(cache) != len(self.variables):
            cache = {v.name: v for v in self.variables}
            self._names = cache
        return cache

    @property
    def events(self) -> Tuple[A2lEvent, ...]:
        return self.xcp.events if self.xcp is not None else ()


def _can_id(token: str) -> Tuple[int, bool]:
    value = _integer(token)
    return value & 0x1FFFFFFF, bool(value & 0x80000000)


def _parse_xcp(node: Node) -> XcpSettings:
    settings: Dict[str, Any] = {}
    layer = node.find('PROTOCOL_LAYER')
    if layer is not None and len(layer.tokens) >= 10:
        tokens = layer.tokens
        settings['timeouts'] = tuple(_number(t) / 1000 for t in tokens[1:8] if _is_number(t))
        settings['max_cto'] = _integer(tokens[8])
        settings['max_dto'] = _integer(tokens[9])
        if layer.has('BYTE_ORDER_MSB_FIRST'):
            settings['byte_order'] = 'big'
        elif layer.has('BYTE_ORDER_MSB_LAST'):
            settings['byte_order'] = 'little'
        for keyword, size in (('ADDRESS_GRANULARITY_BYTE', 1), ('ADDRESS_GRANULARITY_WORD', 2)):
            if layer.has(keyword):
                settings['address_granularity'] = size
        if layer.has('ADDRESS_GRANULARITY_DWORD'):
            settings['address_granularity'] = 4
    daq = node.find('DAQ')
    if daq is not None and daq.tokens:
        settings['daq_dynamic'] = daq.tokens[0] == 'DYNAMIC'
        if len(daq.tokens) > 1 and _is_number(daq.tokens[1]):
            settings['max_daq'] = _integer(daq.tokens[1])
        events = []
        for event in daq.all('EVENT'):
            tokens = event.tokens
            if len(tokens) < 8:
                continue
            try:
                cycle = _integer(tokens[5]) * EVENT_TIME_UNITS.get(_integer(tokens[6]), 0)
                events.append(A2lEvent(_value(tokens[0]), _value(tokens[1]), _integer(tokens[2]), cycle,
                                       _integer(tokens[7])))  # fmt: skip
            except ValueError:
                continue
        settings['events'] = tuple(events)
        timestamp = daq.find('TIMESTAMP_SUPPORTED')
        if timestamp is not None and timestamp.tokens:
            ticks = _integer(timestamp.tokens[0]) if _is_number(timestamp.tokens[0]) else 1
            sizes = {'SIZE_BYTE': 1, 'SIZE_WORD': 2, 'SIZE_DWORD': 4}
            settings['timestamp_size'] = next((s for k, s in sizes.items() if timestamp.has(k)), 0)
            unit = next((u for k, u in TIME_UNITS.items() if timestamp.has(k)), 1e-6)
            settings['timestamp_resolution'] = ticks * unit
    transports = []
    for can in node.all('XCP_ON_CAN'):
        options: Dict[str, Any] = {}
        for keyword, name in (('CAN_ID_MASTER', 'master_id'), ('CAN_ID_SLAVE', 'slave_id')):
            value = can.keyword(keyword)
            if value:
                options[name], extended = _can_id(value[0])
                options['extended'] = options.get('extended', False) or extended
        baudrate = can.keyword('BAUDRATE')
        if baudrate:
            options['baudrate'] = _integer(baudrate[0])
        daq_ids = []
        for entry in can.all('DAQ_LIST_CAN_ID'):
            fixed = entry.keyword('FIXED')
            if entry.tokens and fixed:
                daq_ids.append((_integer(entry.tokens[0]), _can_id(fixed[0])[0]))
        options['daq_ids'] = tuple(daq_ids)
        fd = can.find('CAN_FD')
        if fd is not None:
            options['fd'] = True
            max_dlc = fd.keyword('MAX_DLC')
            options['max_dlc'] = _integer(max_dlc[0]) if max_dlc else 64
            rate = fd.keyword('CAN_FD_DATA_TRANSFER_BAUDRATE')
            if rate:
                options['data_baudrate'] = _integer(rate[0])
        transports.append(XcpTransportLayer('CAN', **options))
    for keyword, kind in (('XCP_ON_UDP_IP', 'UDP'), ('XCP_ON_TCP_IP', 'TCP')):
        for ip in node.all(keyword):
            port = _integer(ip.tokens[1]) if len(ip.tokens) > 1 and _is_number(ip.tokens[1]) else None
            host = ip.keyword('ADDRESS') or ip.keyword('HOST_NAME') or ip.keyword('ADDRESS_V6')
            transports.append(XcpTransportLayer(kind, host=_value(host[0]) if host else '', port=port))
    settings['transports'] = tuple(transports)
    return XcpSettings(**settings)


def _parse_ccp(node: Node) -> CcpSettings:
    settings: Dict[str, Any] = {}
    blob = node.find('TP_BLOB')
    if blob is not None and len(blob.tokens) >= 5:
        tokens = blob.tokens
        try:
            settings['version'] = _integer(tokens[0])
            settings['cro_id'], cro_extended = _can_id(tokens[2])
            settings['dto_id'], dto_extended = _can_id(tokens[3])
            settings['extended'] = cro_extended or dto_extended
            settings['station_address'] = _integer(tokens[4])
            if len(tokens) > 5 and _is_number(tokens[5]):
                settings['byte_order_code'] = _integer(tokens[5])
        except ValueError:
            pass
        baudrate = blob.keyword('BAUDRATE')
        if baudrate:
            settings['baudrate'] = _integer(baudrate[0])
    rasters = []
    for raster in node.walk('RASTER'):
        if len(raster.tokens) >= 3 and _is_number(raster.tokens[2]):
            rasters.append((_value(raster.tokens[0]), _integer(raster.tokens[2])))
    settings['rasters'] = tuple(rasters)
    lists = []
    for blob in node.walk('QP_BLOB'):
        if not blob.tokens or not _is_number(blob.tokens[0]):
            continue

        def number(keyword, blob=blob):
            value = blob.keyword(keyword)
            return _integer(value[0]) if value and _is_number(value[0]) else None

        lists.append(
            (_integer(blob.tokens[0]), number('LENGTH') or 0, number('CAN_ID_FIXED'), number('FIRST_PID'),
             number('RASTER'))
        )  # fmt: skip
    settings['daq_lists'] = tuple(lists)
    return CcpSettings(**settings)


# Parsing


def _order(node: Node) -> Optional[str]:
    value = node.keyword('BYTE_ORDER')
    return BYTE_ORDERS.get(value[0]) if value else None


def _extension(node: Node) -> int:
    value = node.keyword('ECU_ADDRESS_EXTENSION')
    return _integer(value[0]) if value else 0


def _bit_mask(node: Node) -> Optional[int]:
    value = node.keyword('BIT_MASK')
    mask = _integer(value[0]) if value else None
    return mask or None


def _limits(tokens: Sequence[str], at: int) -> Tuple[Optional[float], Optional[float]]:
    try:
        return _number(tokens[at]), _number(tokens[at + 1])
    except (ValueError, IndexError):
        return None, None


def _parse_conversion(node: Node, tabs: Dict[str, Node], formats: Dict[str, Tuple]) -> Conversion:
    tokens = node.tokens
    name = tokens[0]
    kind = tokens[2] if len(tokens) > 2 else 'IDENTICAL'
    unit = _value(tokens[4]) if len(tokens) > 4 else ''
    if kind == 'LINEAR':
        coeffs = node.keyword('COEFFS_LINEAR', 2)
        if coeffs and len(coeffs) == 2:
            return Conversion(name, 'LINEAR', tuple(_number(c) for c in coeffs), unit=unit)
        return Conversion(name, 'IDENTICAL', unit=unit)
    if kind == 'RAT_FUNC':
        coeffs = node.keyword('COEFFS', 6)
        if coeffs and len(coeffs) == 6:
            return Conversion(name, 'RAT_FUNC', tuple(_number(c) for c in coeffs), unit=unit)
        return Conversion(name, 'IDENTICAL', unit=unit)
    if kind == 'FORM':
        formula = node.find('FORMULA')
        if formula is None or not formula.tokens:
            return Conversion(name, 'IDENTICAL', unit=unit)
        inverse = formula.keyword('FORMULA_INV')
        return Conversion(
            name, 'FORM', formula=_value(formula.tokens[0]), inverse=_value(inverse[0]) if inverse else '', unit=unit
        )
    if kind in ('TAB_INTP', 'TAB_NOINTP', 'TAB_VERB'):
        reference = node.keyword('COMPU_TAB_REF')
        table = tabs.get(reference[0]) if reference else None
        if table is None:
            return Conversion(name, 'IDENTICAL', unit=unit)
        parsed = _parse_table(table)
        if kind == 'TAB_VERB':
            return Conversion(name, 'TAB_VERB', verbal=parsed['verbal'], default=parsed['default_text'], unit=unit)
        return Conversion(name, kind, table=parsed['table'], default=parsed['default_number'], unit=unit)
    return Conversion(name, 'IDENTICAL', unit=unit)


def _parse_table(node: Node) -> Dict[str, Any]:
    """A COMPU_TAB, COMPU_VTAB or COMPU_VTAB_RANGE."""
    tokens = node.tokens
    result: Dict[str, Any] = {'table': (), 'verbal': (), 'default_text': None, 'default_number': None}
    default = node.keyword('DEFAULT_VALUE')
    if default:
        result['default_text'] = _value(default[0])
    default_number = node.keyword('DEFAULT_VALUE_NUMERIC')
    if default_number:
        result['default_number'] = _number(default_number[0])
    try:
        if node.kind == 'COMPU_VTAB_RANGE':
            count = _integer(tokens[2])
            entries = tokens[3 : 3 + 3 * count]
            result['verbal'] = tuple(
                (_number(entries[i]), _number(entries[i + 1]), _value(entries[i + 2]))
                for i in range(0, len(entries), 3)
            )
        elif node.kind == 'COMPU_VTAB':
            count = _integer(tokens[3])
            entries = tokens[4 : 4 + 2 * count]
            result['verbal'] = tuple(
                (_number(entries[i]), _number(entries[i]), _value(entries[i + 1])) for i in range(0, len(entries), 2)
            )
        else:  # COMPU_TAB
            count = _integer(tokens[3])
            entries = tokens[4 : 4 + 2 * count]
            pairs = [(_number(entries[i]), _value(entries[i + 1])) for i in range(0, len(entries), 2)]
            if pairs and not all(_is_number(p) for _, p in pairs):  # A COMPU_TAB used verbally
                result['verbal'] = tuple((r, r, p) for r, p in pairs)
            else:
                result['table'] = tuple(sorted((r, float(p)) for r, p in pairs))
    except (ValueError, IndexError):
        pass
    return result


_SIZED_ITEMS = ('SRC_ADDR_', 'RIP_ADDR_', 'OFFSET_', 'SHIFT_OP_', 'DIST_OP_', 'IDENTIFICATION', 'RESERVED')


def _parse_layout(node: Node, module_alignments: Dict[int, int]) -> RecordLayout:
    tokens = node.tokens
    name = tokens[0]
    items: List[LayoutItem] = []
    fixed: List[Tuple[str, int]] = []
    alignments = dict(module_alignments)
    unsupported = ''
    index = 1
    while index < len(tokens):
        token = tokens[index]
        try:
            if token == 'FNC_VALUES':
                data_type, mode, addressing = tokens[index + 2], tokens[index + 3], tokens[index + 4]
                items.append(LayoutItem(_integer(tokens[index + 1]), token, data_type,
                                        DATA_TYPES.get(data_type, (1,))[0], mode, addressing))  # fmt: skip
                if addressing != 'DIRECT':
                    unsupported = f'{addressing} (pointer) addressing is not supported'
                index += 5
            elif token in ('AXIS_PTS_X', 'AXIS_PTS_Y', 'AXIS_PTS_Z'):
                data_type, mode, addressing = tokens[index + 2], tokens[index + 3], tokens[index + 4]
                items.append(LayoutItem(_integer(tokens[index + 1]), token, data_type,
                                        DATA_TYPES.get(data_type, (1,))[0], mode, addressing))  # fmt: skip
                if addressing != 'DIRECT':
                    unsupported = f'{addressing} (pointer) addressing is not supported'
                index += 5
            elif token.startswith('NO_AXIS_PTS_') or token.startswith(_SIZED_ITEMS[:-1]):
                data_type = tokens[index + 2]
                items.append(
                    LayoutItem(_integer(tokens[index + 1]), token, data_type, DATA_TYPES.get(data_type, (1,))[0])
                )
                index += 3
            elif token == 'RESERVED':
                size = DATA_SIZES.get(tokens[index + 2], 1)
                items.append(LayoutItem(_integer(tokens[index + 1]), token, '', size))
                index += 3
            elif token.startswith('FIX_NO_AXIS_PTS_'):
                fixed.append((token[-1], _integer(tokens[index + 1])))
                index += 2
            elif token in ALIGNMENT_KEYWORDS:
                alignments[ALIGNMENT_KEYWORDS[token]] = _integer(tokens[index + 1])
                index += 2
            elif token.startswith('AXIS_RESCALE_'):
                unsupported = 'AXIS_RESCALE layouts are not supported'
                index += 1
            else:
                index += 1
        except (IndexError, ValueError):
            index += 1
    items.sort(key=lambda item: item.position)
    return RecordLayout(name, tuple(items), tuple(fixed), tuple(sorted(alignments.items())), unsupported)


def _parse_axis(node: Node, conversion, unit_of) -> Axis:
    tokens = node.tokens
    attribute, quantity = tokens[0], tokens[1]
    compu = conversion(tokens[2])
    max_points = _integer(tokens[3]) if len(tokens) > 3 and _is_number(tokens[3]) else 0
    minimum, maximum = _limits(tokens, 4)
    reference = node.keyword('AXIS_PTS_REF') or node.keyword('CURVE_AXIS_REF')
    fixed: Tuple[float, ...] = ()
    par = node.keyword('FIX_AXIS_PAR', 3)
    dist = node.keyword('FIX_AXIS_PAR_DIST', 3)
    if par and len(par) == 3:
        offset, shift, count = _number(par[0]), _integer(par[1]), _integer(par[2])
        fixed = tuple(offset + i * (1 << shift) for i in range(count))
    elif dist and len(dist) == 3:
        offset, distance, count = _number(dist[0]), _number(dist[1]), _integer(dist[2])
        fixed = tuple(offset + i * distance for i in range(count))
    else:
        listed = node.find('FIX_AXIS_PAR_LIST')
        if listed is not None:
            fixed = tuple(_number(t) for t in listed.tokens if _is_number(t))
    return Axis(attribute, quantity, compu, max_points, minimum, maximum, reference[0] if reference else '', fixed,
                unit_of(node, compu))  # fmt: skip


def parse_document(tokens: Sequence[str], path: str = '') -> A2lDocument:
    root = parse_tree(tokens)
    document = A2lDocument(path=path)
    project = next(root.walk('PROJECT'), None)
    if project is not None and project.tokens:
        document.project = project.tokens[0]
    module = next(root.walk('MODULE'), None) or root
    if module is not root and module.tokens:
        document.module = module.tokens[0]

    module_alignments: Dict[int, int] = {}
    common = next(module.walk('MOD_COMMON'), None)
    if common is not None:
        document.byte_order = _order(common)
        for keyword, size in ALIGNMENT_KEYWORDS.items():
            value = common.keyword(keyword)
            if value and _is_number(value[0]):
                module_alignments[size] = _integer(value[0])
    parameters = next(module.walk('MOD_PAR'), None)
    if parameters is not None:
        epk = parameters.keyword('EPK')
        if epk:
            document.epk = _value(epk[0])
        address = parameters.keyword('ADDR_EPK')
        if address and _is_number(address[0]):
            document.epk_address = _integer(address[0])
    for interface in module.walk('IF_DATA'):
        if not interface.tokens:
            continue
        if interface.tokens[0] in ('XCP', 'XCPplus') and document.xcp is None:
            document.xcp = _parse_xcp(interface)
        elif interface.tokens[0] == 'ASAP1B_CCP' and document.ccp is None:
            document.ccp = _parse_ccp(interface)

    tabs: Dict[str, Node] = {}
    for kind in ('COMPU_TAB', 'COMPU_VTAB', 'COMPU_VTAB_RANGE'):
        for node in module.walk(kind):
            if node.tokens:
                tabs[node.tokens[0]] = node
    conversions: Dict[str, Conversion] = {}
    for node in module.walk('COMPU_METHOD'):
        if node.tokens:
            conversions[node.tokens[0]] = _parse_conversion(node, tabs, {})
    layouts: Dict[str, RecordLayout] = {}
    for node in module.walk('RECORD_LAYOUT'):
        if node.tokens:
            layouts[node.tokens[0]] = _parse_layout(node, module_alignments)

    def conversion(name: str) -> Conversion:
        return conversions.get(name, IDENTICAL if name == 'NO_COMPU_METHOD' else Conversion(name))

    def unit_of(node: Node, compu: Conversion) -> str:
        unit = node.keyword('PHYS_UNIT')
        return _value(unit[0]) if unit else compu.unit

    def linear(compu: Conversion) -> Dict[str, float]:
        factor, offset = compu.linear or (1.0, 0.0)
        return {'factor': factor, 'offset': offset}

    variables: List[A2lVariable] = []
    for node in module.walk('MEASUREMENT'):
        tokens = node.tokens
        if len(tokens) < 8:
            continue
        name, description, data_type = tokens[0], _value(tokens[1]), tokens[2]
        if data_type not in DATA_TYPES:
            continue
        address = node.keyword('ECU_ADDRESS')
        if not address:
            continue  # No address: not readable over XCP/CCP by itself
        compu = conversion(tokens[3])
        minimum, maximum = _limits(tokens, 6)
        shape: Tuple[int, ...] = ()
        dimensions = node.numbers_after('MATRIX_DIM')
        array = node.keyword('ARRAY_SIZE')
        if dimensions:
            shape = tuple(int(d) for d in dimensions if d > 1) or ()
        elif array and _is_number(array[0]) and _integer(array[0]) > 1:
            shape = (_integer(array[0]),)
        variables.append(
            A2lVariable(
                name,
                description,
                _integer(address[0]),
                data_type,
                node.has('READ_WRITE'),
                unit=unit_of(node, compu),
                minimum=minimum,
                maximum=maximum,
                extension=_extension(node),
                byte_order=_order(node),
                bit_mask=_bit_mask(node),
                kind='MEASUREMENT',
                shape=shape,
                conversion=compu,
                **linear(compu),
            )
        )

    for node in module.walk('AXIS_PTS'):
        tokens = node.tokens
        if len(tokens) < 10:
            continue
        layout = layouts.get(tokens[4])
        axis_item = layout.item('AXIS_PTS_X') if layout is not None else None
        if axis_item is None:
            continue
        compu = conversion(tokens[6])
        minimum, maximum = _limits(tokens, 8)
        variables.append(
            A2lVariable(
                tokens[0],
                _value(tokens[1]),
                _integer(tokens[2]),
                axis_item.data_type,
                not node.has('READ_ONLY'),
                unit=unit_of(node, compu),
                minimum=minimum,
                maximum=maximum,
                extension=_extension(node),
                byte_order=_order(node),
                kind='AXIS_PTS',
                conversion=compu,
                layout=layout,
                max_points=_integer(tokens[7]),
                unsupported=layout.unsupported,
                **linear(compu),
            )
        )

    for node in module.walk('CHARACTERISTIC'):
        tokens = node.tokens
        if len(tokens) < 9:
            continue
        name, description, kind = tokens[0], _value(tokens[1]), tokens[2]
        layout = layouts.get(tokens[4])
        compu = conversion(tokens[6])
        minimum, maximum = _limits(tokens, 7)
        values = layout.values if layout is not None else None
        unsupported = layout.unsupported if layout is not None else 'no record layout'
        data_type = values.data_type if values is not None else 'UBYTE'
        if kind not in ('VALUE', 'VAL_BLK', 'ASCII', 'CURVE', 'MAP'):
            unsupported = f'{kind} characteristics are not supported'
        elif values is None and not unsupported:
            unsupported = 'the record layout has no FNC_VALUES'
        elif data_type not in DATA_TYPES and not unsupported:
            unsupported = f'data type {data_type} is not supported'
        axes = tuple(_parse_axis(axis, conversion, unit_of) for axis in node.all('AXIS_DESCR'))
        if kind == 'MAP' and values is not None and values.index_mode.startswith('ALTERNATE') and not unsupported:
            unsupported = f'{values.index_mode} maps are not supported'
        needed = {'CURVE': 1, 'MAP': 2}.get(kind, 0)
        if len(axes) < needed and not unsupported:
            unsupported = f'a {kind.lower()} needs {needed} AXIS_DESCR'
        if any(a.attribute not in ('STD_AXIS', 'FIX_AXIS', 'COM_AXIS', 'RES_AXIS', 'CURVE_AXIS') for a in axes):
            unsupported = unsupported or 'an axis type that is not supported'
        shape: Tuple[int, ...] = ()
        if kind in ('VAL_BLK', 'ASCII'):
            dimensions = node.numbers_after('MATRIX_DIM')
            number = node.keyword('NUMBER')
            if dimensions:
                shape = tuple(int(d) for d in dimensions if d > 1) or (1,)
            elif number and _is_number(number[0]):
                shape = (_integer(number[0]),)
        variables.append(
            A2lVariable(
                name,
                description,
                _integer(tokens[3]),
                data_type,
                not node.has('READ_ONLY'),
                unit=unit_of(node, compu),
                minimum=minimum,
                maximum=maximum,
                extension=_extension(node),
                byte_order=_order(node),
                bit_mask=_bit_mask(node),
                kind=kind,
                shape=shape,
                conversion=compu,
                layout=layout,
                axes=axes,
                unsupported=unsupported,
                **linear(compu),
            )
        )
    document.variables = variables
    return document


def parse_a2l_document(text: str, base: Optional[Path] = None) -> A2lDocument:
    return parse_document(_expand_includes(_tokenise(text), base))


def load_a2l(path: Union[str, Path]) -> A2lDocument:
    """Read an A2L file (with its /include files) into a document: variables, EPK and the XCP/CCP settings."""
    path = Path(path)
    return parse_document(_tokenise_file(path), str(path))


def parse_a2l(text: str) -> List[A2lVariable]:
    """The measurements, characteristics and axes of an A2L text."""
    return parse_a2l_document(text).variables


def read_a2l(path: Union[str, Path]) -> List[A2lVariable]:
    return load_a2l(path).variables
