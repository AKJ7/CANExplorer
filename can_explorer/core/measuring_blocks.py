"""Measuring blocks ("groups"): the live data of VW-group modules on KWP2000 — ReadDataByLocalIdentifier (0x21) with
the group number as the local identifier, usually over TP 2.0 (`core/tp20.py`).

Layout. A positive response is 0x61, the group, then one three-byte value after another: a formula (display type)
byte and two data bytes, a and b. That layout is taken from the example exchange of the community TP 2.0 description
(https://jazdw.net/tp20), whose 26-byte answer to `21 01` is `61 01` and eight such values; the unit tests replay it.
A response whose length after the group is not a multiple of three is not forced into values: the whole values are
parsed and the remaining bytes kept in `rest`.

Scaling. How a formula byte turns a and b into a value and a unit is VW's own and undocumented, and the community
tables disagree between sources and module generations, so none is built in. A *formula table* — a JSON file you
supply, e.g. from your own label source — maps formula bytes to an expression over `a` and `b` and a unit, and may
name the values of each group:

    {
      "formulas": {"0x01": {"expression": "a * b / 5", "unit": "/min"}},
      "groups": {"1": ["Engine speed", "Coolant temperature"]}
    }

Formula keys are hex (`"0x01"` or `"01"`), group keys decimal (as testers number groups 000-255) or `0x` hex. The
expressions use the safe evaluator of the A2L formulas (arithmetic, comparisons, conditionals and the math
functions, nothing else). The example is illustrative only, not a statement of VW's formulas. A value whose formula
is not in the table is shown raw.
"""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from can_explorer.core.a2l import A2lError, evaluate_expression
from can_explorer.core.frames import format_hex

READ_DATA_BY_LOCAL_IDENTIFIER = 0x21
VALUE_SIZE = 3  # Formula byte, a, b


class MeasuringBlockError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MeasuringValue:
    """One value of a group: its formula byte and data bytes, and — with a formula table — its name, scaled value
    and unit (or why the formula failed)."""

    formula: int
    a: int
    b: int
    name: str = ''
    value: Optional[float] = None
    unit: str = ''
    error: str = ''

    @property
    def raw(self) -> str:
        return f'formula 0x{self.formula:02X}, a={self.a}, b={self.b}'

    @property
    def text(self) -> str:
        if self.value is not None:
            return f'{self.value:g} {self.unit}'.rstrip()
        return self.raw

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {'formula': f'0x{self.formula:02X}', 'a': self.a, 'b': self.b}
        if self.name:
            result['name'] = self.name
        if self.value is not None:
            result['value'] = self.value
            result['unit'] = self.unit
        if self.error:
            result['error'] = self.error
        return result


@dataclass(frozen=True, slots=True)
class MeasuringBlock:
    group: int
    values: Tuple[MeasuringValue, ...]
    rest: bytes = b''  # Bytes after the last whole value (a layout that is not formula/a/b triplets)

    def describe(self) -> str:
        parts = [f'{value.name}: {value.text}' if value.name else value.text for value in self.values]
        if self.rest:
            parts.append(f'unparsed {format_hex(self.rest)}')
        return f'group {self.group}: ' + ('; '.join(parts) if parts else 'no values')

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {'group': self.group, 'values': [value.as_dict() for value in self.values]}
        if self.rest:
            result['unparsed'] = format_hex(self.rest)
        return result


def parse_measuring_values(group: int, data: bytes) -> MeasuringBlock:
    """The values of a group from the data after the echoed group number (what `KwpClient.read_local_identifier`
    returns)."""
    data = bytes(data)
    whole = len(data) - len(data) % VALUE_SIZE
    values = tuple(MeasuringValue(*data[offset : offset + VALUE_SIZE]) for offset in range(0, whole, VALUE_SIZE))
    return MeasuringBlock(group, values, data[whole:])


def parse_measuring_response(response: bytes) -> MeasuringBlock:
    """A whole positive response (`61 <group> …`)."""
    if len(response) < 2 or response[0] != READ_DATA_BY_LOCAL_IDENTIFIER + 0x40:
        raise MeasuringBlockError(f'Not a ReadDataByLocalIdentifier response: {format_hex(response)}')
    return parse_measuring_values(response[1], response[2:])


@dataclass(frozen=True, slots=True)
class Formula:
    expression: str
    unit: str = ''


def _parse_key(key: str, hexadecimal: bool) -> int:
    text = str(key).strip()
    try:
        return int(text, 16) if hexadecimal or text.lower().startswith('0x') else int(text, 10)
    except ValueError:
        raise MeasuringBlockError(f'Invalid key {key!r} in the formula table') from None


@dataclass
class FormulaTable:
    """Formula bytes → expression and unit, and group → value names: the scaling of measuring blocks, supplied by
    the user (see the module docstring for the JSON)."""

    formulas: Dict[int, Formula] = field(default_factory=dict)
    groups: Dict[int, List[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'FormulaTable':
        if not isinstance(data, Mapping):
            raise MeasuringBlockError('A formula table is a JSON object with "formulas" and "groups"')
        formulas: Dict[int, Formula] = {}
        for key, entry in dict(data.get('formulas') or {}).items():
            if isinstance(entry, str):
                entry = {'expression': entry}
            if not isinstance(entry, Mapping) or not str(entry.get('expression', '')).strip():
                raise MeasuringBlockError(f'Formula {key!r} needs an "expression"')
            formulas[_parse_key(key, hexadecimal=True)] = Formula(str(entry['expression']), str(entry.get('unit', '')))
        groups: Dict[int, List[str]] = {}
        for key, names in dict(data.get('groups') or {}).items():
            if not isinstance(names, list):
                raise MeasuringBlockError(f'Group {key!r} must list the names of its values')
            groups[_parse_key(key, hexadecimal=False)] = [str(name) for name in names]
        return cls(formulas, groups)

    @classmethod
    def load(cls, path: str | Path) -> 'FormulaTable':
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise MeasuringBlockError(f'Cannot read the formula table {path.name}: {e}') from None
        return cls.from_dict(data)

    def apply(self, block: MeasuringBlock) -> MeasuringBlock:
        """The block with its values named and scaled where the table knows them."""
        names = self.groups.get(block.group, [])
        values = []
        for index, value in enumerate(block.values):
            name = names[index] if index < len(names) else ''
            formula = self.formulas.get(value.formula)
            if formula is None:
                values.append(replace(value, name=name))
                continue
            try:
                scaled = evaluate_expression(formula.expression, {'a': value.a, 'b': value.b})
            except A2lError as e:
                values.append(replace(value, name=name, error=str(e)))
                continue
            values.append(replace(value, name=name, value=scaled, unit=formula.unit))
        return replace(block, values=tuple(values))


async def read_measuring_block(client, group: int, table: Optional[FormulaTable] = None) -> MeasuringBlock:
    """Read a group with a `KwpClient` (ReadDataByLocalIdentifier), scaled with `table` if given."""
    if not 0x00 <= group <= 0xFF:
        raise MeasuringBlockError(f'A measuring-block group is 0-255, got {group}')
    block = parse_measuring_values(group, await client.read_local_identifier(group))
    return table.apply(block) if table is not None else block
