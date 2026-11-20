"""Measurement and calibration by name over XCP or CCP: reading and writing A2L variables of every supported shape
(scalars, arrays, value blocks, strings, curves, maps and axis points), polling many measurements with few
uploads, checking the EPK, and calibration datasets (JSON and DCM) that can be saved, compared and written back.

The client is an `XcpClient` or `CcpClient`: anything with `read(address, extension, size)`,
`write(address, extension, data)`, `byte_order` and `address_granularity`.
"""

import json
import math
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from can_explorer.core.a2l import (
    DATA_TYPES,
    A2lDocument,
    A2lVariable,
    Axis,
    LayoutItem,
    Physical,
    decode_raw,
    encode_raw,
)

MERGE_GAP = 16  # Bytes between two measurements still read in one upload
MERGE_LIMIT = 252  # Bytes of one merged upload


class CalibrationError(Exception):
    pass


def _granularity(client) -> int:
    return getattr(client, 'address_granularity', 1) or 1


def _order(client, document: Optional[A2lDocument], variable: Optional[A2lVariable] = None) -> str:
    if variable is not None and variable.byte_order:
        return variable.byte_order
    if document is not None and document.byte_order:
        return document.byte_order
    return getattr(client, 'byte_order', 'little')


# Polling many scalars with few reads


async def read_many(client, variables: Sequence[A2lVariable]) -> Dict[str, bytes]:
    """The raw bytes of each variable, reading neighbouring ones together (gaps up to MERGE_GAP bytes, uploads up
    to MERGE_LIMIT bytes, the same address extension)."""
    granularity = _granularity(client)
    spans = sorted(
        ((v.extension, v.address * granularity, v.size, v) for v in variables if v.size > 0),
        key=lambda span: (span[0], span[1]),
    )
    groups: List[List[Tuple[int, int, int, A2lVariable]]] = []
    for span in spans:
        if groups:
            extension, start = groups[-1][0][0], groups[-1][0][1]
            end = max(s[1] + s[2] for s in groups[-1])
            if span[0] == extension and span[1] - end <= MERGE_GAP and span[1] + span[2] - start <= MERGE_LIMIT:
                groups[-1].append(span)
                continue
        groups.append([span])
    result: Dict[str, bytes] = {}
    for group in groups:
        extension, start = group[0][0], group[0][1]
        start -= start % granularity
        end = max(s[1] + s[2] for s in group)
        data = await client.read(start // granularity, extension, end - start)
        for _, offset, size, variable in group:
            result[variable.name] = data[offset - start : offset - start + size]
    return result


# Structured values


@dataclass
class CalibrationValue:
    """The physical value of a variable: a scalar, a list (arrays, blocks, curves, axis points), a list of rows
    (maps, values[y][x]) or a string (ASCII). Curves and maps carry their axes' physical points."""

    name: str
    kind: str
    value: Any
    axes: List[List[Physical]] = field(default_factory=list)
    unit: str = ''
    raw: bytes = b''

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {'kind': self.kind, 'value': self.value}
        if self.axes:
            result['axes'] = self.axes
        if self.unit:
            result['unit'] = self.unit
        return result


@dataclass
class _Plan:
    """Where the parts of a characteristic are, once the axis point counts are known."""

    counts: Dict[str, int]
    offsets: Dict[str, int]  # Layout item kind -> byte offset
    size: int


def _align(offset: int, size: int, alignments: Dict[int, int]) -> int:
    alignment = alignments.get(size, 1) or 1
    return -(-offset // alignment) * alignment


def _item_size(item: LayoutItem, counts: Dict[str, int]) -> int:
    if item.kind == 'FNC_VALUES':
        return item.size * math.prod(counts.values()) if counts else item.size
    if item.kind.startswith('AXIS_PTS_'):
        return item.size * counts.get(item.kind[-1], 0)
    return item.size


async def _plan(client, variable: A2lVariable, document: Optional[A2lDocument], order: str) -> _Plan:
    layout = variable.layout
    if layout is None:
        raise CalibrationError(f'{variable.name} has no record layout')
    counts: Dict[str, int] = {}
    names = 'XY'
    if variable.kind == 'AXIS_PTS':
        counts['X'] = layout.fixed('X') or variable.max_points
    elif variable.kind in ('CURVE', 'MAP'):
        for index, axis in enumerate(variable.axes[: 1 if variable.kind == 'CURVE' else 2]):
            letter = names[index]
            if axis.attribute == 'FIX_AXIS' and axis.fixed:
                counts[letter] = len(axis.fixed)
            elif layout.fixed(letter):
                counts[letter] = layout.fixed(letter)
            elif axis.attribute in ('COM_AXIS', 'RES_AXIS', 'CURVE_AXIS'):
                points = await _axis_points(client, axis, document)
                counts[letter] = len(points) if points is not None else axis.max_points
            else:
                counts[letter] = axis.max_points
        if variable.kind == 'MAP' and len(counts) < 2:
            raise CalibrationError(f'{variable.name}: a map needs two axes')
    elif variable.kind in ('VAL_BLK', 'ASCII'):
        counts['X'] = variable.count
    alignments = dict(layout.alignments)
    granularity = _granularity(client)
    offsets: Dict[str, int] = {}
    offset = 0
    for item in layout.items:
        offset = _align(offset, item.size, alignments)
        offsets[item.kind] = offset
        if item.kind.startswith('NO_AXIS_PTS_') and variable.kind in ('CURVE', 'MAP', 'AXIS_PTS'):
            letter = item.kind[-1]
            axis_index = names.index(letter) if letter in names else 0
            uses_layout = variable.kind == 'AXIS_PTS' or (
                axis_index < len(variable.axes) and variable.axes[axis_index].attribute == 'STD_AXIS'
            )
            if uses_layout:  # The number of points is in the ECU's memory
                address = variable.address + offset // granularity
                raw = await client.read(address, variable.extension, item.size)
                maximum = variable.max_points if variable.kind == 'AXIS_PTS' else variable.axes[axis_index].max_points
                count = int(decode_raw(item.data_type, raw, order))
                counts[letter] = max(0, min(count, maximum or count))
        offset += _item_size(item, counts)
    return _Plan(counts, offsets, offset)


async def _axis_points(client, axis: Axis, document: Optional[A2lDocument]) -> Optional[List[Physical]]:
    if not axis.reference or document is None:
        return None
    reference = document.variable(axis.reference)
    if reference is None:
        return None
    if reference.kind == 'CURVE':  # CURVE_AXIS: the values of a curve are the axis
        return (await read_value(client, reference, document)).value
    return (await read_value(client, reference, document)).value


def _elements(data: bytes, item: LayoutItem, count: int, offset: int, variable: A2lVariable, order: str) -> List[float]:
    size = item.size
    return [decode_raw(item.data_type, data[offset + i * size : offset + (i + 1) * size], order, variable.bit_mask)
            for i in range(count)]  # fmt: skip


async def read_value(client, variable: A2lVariable, document: Optional[A2lDocument] = None) -> CalibrationValue:
    """Read a variable of any supported kind and convert it to physical values."""
    if variable.unsupported:
        raise CalibrationError(f'{variable.name}: {variable.unsupported}')
    order = _order(client, document, variable)
    compu = variable.compu
    if variable.kind == 'MEASUREMENT' or variable.layout is None:
        data = await client.read(variable.address, variable.extension, variable.size)
        if not variable.shape:
            return CalibrationValue(
                variable.name, variable.kind, variable.decode(data, order), unit=variable.unit, raw=data
            )
        values = variable.decode_values(data, order)
        return CalibrationValue(
            variable.name, variable.kind, _shape(values, variable.shape), unit=variable.unit, raw=data
        )
    plan = await _plan(client, variable, document, order)
    data = await client.read(variable.address, variable.extension, plan.size)
    layout = variable.layout
    values_item = layout.values
    if variable.kind == 'ASCII':
        offset = plan.offsets.get('FNC_VALUES', 0)
        text = data[offset : offset + variable.count].split(b'\x00')[0].decode('latin-1', 'replace')
        return CalibrationValue(variable.name, 'ASCII', text, raw=data)
    if variable.kind == 'AXIS_PTS':
        item = layout.item('AXIS_PTS_X')
        raw = _elements(data, item, plan.counts['X'], plan.offsets['AXIS_PTS_X'], variable, order)
        if item.index_mode == 'INDEX_DECR':
            raw.reverse()
        return CalibrationValue(
            variable.name, 'AXIS_PTS', [compu.to_physical(r) for r in raw], unit=variable.unit, raw=data
        )
    count = math.prod(plan.counts.values()) if plan.counts else 1
    raw_values = _elements(data, values_item, count, plan.offsets['FNC_VALUES'], variable, order)
    physical = [compu.to_physical(r) for r in raw_values]
    if variable.kind == 'VALUE':
        return CalibrationValue(variable.name, 'VALUE', physical[0], unit=variable.unit, raw=data)
    if variable.kind == 'VAL_BLK':
        return CalibrationValue(
            variable.name, 'VAL_BLK', _shape(physical, variable.shape), unit=variable.unit, raw=data
        )
    axes = [await _axis(client, variable, document, data, plan, index, order) for index in range(len(plan.counts))]
    if variable.kind == 'CURVE':
        return CalibrationValue(variable.name, 'CURVE', physical, axes, variable.unit, data)
    nx, ny = plan.counts['X'], plan.counts['Y']
    if values_item.index_mode == 'COLUMN_DIR':  # x outer, y inner
        rows = [[physical[x * ny + y] for x in range(nx)] for y in range(ny)]
    else:  # ROW_DIR: y outer, x inner
        rows = [physical[y * nx : (y + 1) * nx] for y in range(ny)]
    return CalibrationValue(variable.name, 'MAP', rows, axes, variable.unit, data)


async def _axis(client, variable, document, data, plan: _Plan, index: int, order: str) -> List[Physical]:
    axis = variable.axes[index]
    letter = 'XY'[index]
    count = plan.counts[letter]
    if axis.attribute == 'FIX_AXIS':
        return [axis.conversion.to_physical(p) for p in axis.fixed[:count]]
    if axis.attribute in ('COM_AXIS', 'RES_AXIS', 'CURVE_AXIS'):
        points = await _axis_points(client, axis, document)
        return list(points[:count]) if points is not None else list(range(count))
    item = variable.layout.item(f'AXIS_PTS_{letter}')
    if item is None:
        return list(range(count))
    raw = _elements(data, item, count, plan.offsets[item.kind], variable, order)
    if item.index_mode == 'INDEX_DECR':
        raw.reverse()
    return [axis.conversion.to_physical(r) for r in raw]


def _shape(values: List[Any], shape: Sequence[int]) -> Any:
    if len(shape) <= 1:
        return values
    inner = math.prod(shape[1:])
    return [_shape(values[i * inner : (i + 1) * inner], shape[1:]) for i in range(shape[0])]


def _flatten(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple)):
        return [item for part in value for item in _flatten(part)]
    return [value]


def _encode_all(variable: A2lVariable, item: LayoutItem, values: Iterable[Physical], order: str) -> bytes:
    data = bytearray()
    compu = variable.compu
    for value in values:
        variable.check_limits(value)
        data += encode_raw(item.data_type, compu.to_raw(value), order)
    return bytes(data)


async def write_value(
    client, variable: A2lVariable, value: Any, document: Optional[A2lDocument] = None, axes: Optional[List] = None
) -> None:
    """Write a variable's physical value(s): a scalar, a (nested) list, a list of map rows (values[y][x]) or a
    string (ASCII). `axes` also writes the points of standard axes stored with a curve or map."""
    if variable.unsupported:
        raise CalibrationError(f'{variable.name}: {variable.unsupported}')
    if not variable.writable:
        raise CalibrationError(f'{variable.name} is read-only')
    order = _order(client, document, variable)
    granularity = _granularity(client)
    if variable.kind == 'MEASUREMENT' or variable.layout is None:
        values = _flatten(value)
        if len(values) != variable.count:
            raise CalibrationError(f'{variable.name} has {variable.count} values, {len(values)} were given')
        if variable.bit_mask and variable.count == 1:
            current = await client.read(variable.address, variable.extension, variable.size)
            variable.check_limits(values[0])
            data = variable.encode(values[0], order, current)
        else:
            data = b''.join(_check_and_encode(variable, v, order) for v in values)
        await client.write(variable.address, variable.extension, data)
        return
    plan = await _plan(client, variable, document, order)
    layout = variable.layout
    if variable.kind == 'ASCII':
        text = str(value).encode('latin-1')
        if len(text) > variable.count:
            raise CalibrationError(f'{variable.name} holds at most {variable.count} characters')
        offset = plan.offsets.get('FNC_VALUES', 0)
        await client.write(
            variable.address + offset // granularity, variable.extension, text.ljust(variable.count, b'\0')
        )
        return
    if variable.kind == 'AXIS_PTS':
        item = layout.item('AXIS_PTS_X')
        points = _flatten(value)
        if len(points) != plan.counts['X']:
            raise CalibrationError(f'{variable.name} has {plan.counts["X"]} points, {len(points)} were given')
        if item.index_mode == 'INDEX_DECR':
            points = list(reversed(points))
        await client.write(variable.address + plan.offsets['AXIS_PTS_X'] // granularity, variable.extension,
                           _encode_all(variable, item, points, order))  # fmt: skip
        return
    values_item = layout.values
    if variable.kind == 'VALUE':
        values = _flatten(value)[:1]
        if variable.bit_mask:
            address = variable.address + plan.offsets['FNC_VALUES'] // granularity
            current = await client.read(address, variable.extension, values_item.size)
            variable.check_limits(values[0])
            raw = variable.compu.to_raw(values[0])
            data = encode_raw(values_item.data_type, raw, order, variable.bit_mask, current)
            await client.write(address, variable.extension, data)
            return
    elif variable.kind == 'MAP':
        rows = value
        nx, ny = plan.counts['X'], plan.counts['Y']
        if len(rows) != ny or any(len(row) != nx for row in rows):
            raise CalibrationError(f'{variable.name} is {nx} × {ny}')
        if values_item.index_mode == 'COLUMN_DIR':
            values = [rows[y][x] for x in range(nx) for y in range(ny)]
        else:
            values = [rows[y][x] for y in range(ny) for x in range(nx)]
    else:
        values = _flatten(value)
    expected = math.prod(plan.counts.values()) if plan.counts else 1
    if len(values) != expected:
        raise CalibrationError(f'{variable.name} has {expected} values, {len(values)} were given')
    data = _encode_all(variable, values_item, values, order)
    await client.write(variable.address + plan.offsets['FNC_VALUES'] // granularity, variable.extension, data)
    for index, points in enumerate(axes or []):
        if points is None or index >= len(variable.axes) or variable.axes[index].attribute != 'STD_AXIS':
            continue
        letter = 'XY'[index]
        item = layout.item(f'AXIS_PTS_{letter}')
        if item is None or len(points) != plan.counts[letter]:
            raise CalibrationError(f'{variable.name}: axis {letter} has {plan.counts[letter]} points')
        ordered = list(reversed(points)) if item.index_mode == 'INDEX_DECR' else list(points)
        raw = b''.join(encode_raw(item.data_type, variable.axes[index].conversion.to_raw(p), order) for p in ordered)
        await client.write(variable.address + plan.offsets[item.kind] // granularity, variable.extension, raw)


def _check_and_encode(variable: A2lVariable, value: Physical, order: str) -> bytes:
    variable.check_limits(value)
    return variable.encode(value, order)


def parse_value(variable: A2lVariable, text: str) -> Any:
    """A value typed by the user: a number, a verbal value, a list ("1, 2, 3"), map rows ("1 2; 3 4") or JSON."""
    text = str(text).strip()
    if variable.kind == 'ASCII':
        return text
    if text.startswith('['):
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise CalibrationError(f'Not valid JSON: {e}') from None

    def scalar(part: str) -> Physical:
        part = part.strip()
        try:
            return float(part)
        except ValueError:
            if variable.compu.is_verbal:
                return part
            raise CalibrationError(f'{part!r} is not a number') from None

    if variable.kind == 'MAP':
        return [[scalar(p) for p in re.split(r'[,\s]+', row.strip()) if p] for row in text.split(';') if row.strip()]
    if variable.kind in ('CURVE', 'VAL_BLK', 'AXIS_PTS') or variable.shape:
        return [scalar(p) for p in re.split(r'[,;\s]+', text) if p]
    return scalar(text)


def format_value(value: Any, unit: str = '', digits: int = 6) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (list, tuple)):
            return '; '.join(format_value(row, digits=digits) for row in value)
        return ', '.join(format_value(v, digits=digits) for v in value)
    text = f'{value:.{digits}g}' if isinstance(value, float) else str(value)
    return f'{text} {unit}' if unit else text


# EPK


async def check_epk(client, document: A2lDocument) -> Dict[str, Any]:
    """Compare the A2L's EPK (its build identifier) with the ECU's memory at ADDR_EPK."""
    if not document.epk or document.epk_address is None:
        return {'expected': document.epk, 'ecu': None, 'match': None}
    data = await client.read(document.epk_address, 0, len(document.epk.encode('latin-1')))
    ecu = data.decode('latin-1', 'replace')
    return {'expected': document.epk, 'ecu': ecu, 'match': ecu == document.epk}


# Datasets

Dataset = Dict[str, Dict[str, Any]]  # name -> {'kind', 'value', 'axes'?, 'unit'?}


async def read_dataset(client, document: A2lDocument, names: Optional[Iterable[str]] = None) -> Tuple[Dataset, Dict]:
    """The values of the writable characteristics (or of `names`): (dataset, errors by name)."""
    dataset: Dataset = {}
    errors: Dict[str, str] = {}
    selected = set(names) if names is not None else None
    for variable in document.variables:
        if selected is not None and variable.name not in selected:
            continue
        if selected is None and (variable.kind == 'MEASUREMENT' or not variable.writable or variable.unsupported):
            continue
        try:
            dataset[variable.name] = (await read_value(client, variable, document)).as_dict()
        except Exception as e:
            errors[variable.name] = str(e)
    return dataset, errors


async def write_dataset(client, document: A2lDocument, dataset: Dataset) -> Dict[str, str]:
    """Write every value of a dataset (with standard axes); returns the errors by name."""
    errors: Dict[str, str] = {}
    for name, entry in dataset.items():
        variable = document.variable(name)
        if variable is None:
            errors[name] = 'not in the A2L'
            continue
        try:
            await write_value(client, variable, entry['value'], document, entry.get('axes'))
        except Exception as e:
            errors[name] = str(e)
    return errors


def _close(a: Any, b: Any, tolerance: float) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y, tolerance) for x, y in zip(a, b, strict=True))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)
    return a == b


def compare_datasets(reference: Dataset, other: Dataset, tolerance: float = 1e-6) -> List[Dict[str, Any]]:
    """The differences between two datasets: values (and axes) that differ, and names only in one of them."""
    differences = []
    for name in sorted(set(reference) | set(other)):
        a, b = reference.get(name), other.get(name)
        if a is None or b is None:
            differences.append({'name': name, 'reference': a and a['value'], 'other': b and b['value'],
                                'missing': 'reference' if a is None else 'other'})  # fmt: skip
        elif not _close(a['value'], b['value'], tolerance) or not _close(a.get('axes'), b.get('axes'), tolerance):
            differences.append({'name': name, 'reference': a['value'], 'other': b['value']})
    return differences


def save_json(path: Union[str, Path], dataset: Dataset, document: Optional[A2lDocument] = None) -> None:
    content = {
        'format': 'CANExplorer calibration dataset',
        'version': 1,
        'a2l': Path(document.path).name if document is not None and document.path else '',
        'epk': document.epk if document is not None else '',
        'values': dataset,
    }
    Path(path).write_text(json.dumps(content, indent=2), encoding='utf-8')


def load_json(path: Union[str, Path]) -> Dataset:
    content = json.loads(Path(path).read_text(encoding='utf-8'))
    values = content.get('values', content) if isinstance(content, dict) else None
    if not isinstance(values, dict):
        raise CalibrationError('Not a calibration dataset')
    return values


def _number_text(value: Physical) -> str:
    return f'{value:.10g}' if isinstance(value, (int, float)) else f'"{value}"'


def _dcm_values(keyword: str, values: Sequence[Physical], per_line: int = 6) -> List[str]:
    """WERT/ST/X lines; verbal values go on TEXT/ST_TX lines."""
    lines = []
    textual = any(isinstance(v, str) for v in values)
    keyword = {'WERT': 'TEXT', 'ST/X': 'ST_TX/X', 'ST/Y': 'ST_TX/Y'}[keyword] if textual else keyword
    for start in range(0, len(values), per_line):
        lines.append(f'   {keyword} ' + ' '.join(_number_text(v) for v in values[start : start + per_line]))
    return lines


def to_dcm(dataset: Dataset, document: Optional[A2lDocument] = None) -> str:
    """A dataset in the DCM (DAMOS) format, version 2.0."""
    lines = ['* Calibration dataset written by CANExplorer', 'KONSERVIERUNG_FORMAT 2.0', '']
    for name, entry in dataset.items():
        variable = document.variable(name) if document is not None else None
        description = variable.description if variable is not None else ''
        kind, value, unit = entry['kind'], entry['value'], entry.get('unit', '')
        axes = entry.get('axes') or []
        attribute = variable.axes[0].attribute if variable is not None and variable.axes else 'STD_AXIS'
        if kind in ('VALUE', 'MEASUREMENT') and not isinstance(value, list):
            lines.append(f'FESTWERT {name}')
        elif kind == 'ASCII':
            lines.append(f'TEXTSTRING {name}')
        elif kind == 'AXIS_PTS':
            lines.append(f'STUETZSTELLENVERTEILUNG {name} {len(value)}')
        elif kind == 'CURVE':
            prefix = {'FIX_AXIS': 'FEST', 'COM_AXIS': 'GRUPPEN'}.get(attribute, '')
            lines.append(f'{prefix}KENNLINIE {name} {len(value)}')
        elif kind == 'MAP':
            prefix = {'FIX_AXIS': 'FEST', 'COM_AXIS': 'GRUPPEN'}.get(attribute, '')
            lines.append(f'{prefix}KENNFELD {name} {len(value[0]) if value else 0} {len(value)}')
        else:  # VAL_BLK and arrays
            if value and isinstance(value[0], list):
                lines.append(f'FESTWERTEBLOCK {name} {len(value[0])} @ {len(value)}')
            else:
                lines.append(f'FESTWERTEBLOCK {name} {len(_flatten(value))}')
        if description:
            lines.append(f'   LANGNAME "{description}"')
        if unit:
            lines.append(f'   EINHEIT_W "{unit}"')
        if kind == 'ASCII':
            lines.append(f'   TEXT "{value}"')
        elif kind == 'AXIS_PTS':
            lines += _dcm_values('ST/X', value)
        elif kind == 'CURVE':
            lines += _dcm_values('ST/X', axes[0] if axes else list(range(len(value))))
            lines += _dcm_values('WERT', value)
        elif kind == 'MAP':
            lines += _dcm_values('ST/X', axes[0] if axes else list(range(len(value[0]) if value else 0)))
            y_axis = axes[1] if len(axes) > 1 else list(range(len(value)))
            for y, row in zip(y_axis, value, strict=False):
                lines += _dcm_values('ST/Y', [y])
                lines += _dcm_values('WERT', row)
        elif isinstance(value, list):
            rows = value if value and isinstance(value[0], list) else [value]
            for row in rows:
                lines += _dcm_values('WERT', row)
        else:
            lines += _dcm_values('WERT', [value])
        lines += ['END', '']
    return '\n'.join(lines)


def save_dcm(path: Union[str, Path], dataset: Dataset, document: Optional[A2lDocument] = None) -> None:
    Path(path).write_text(to_dcm(dataset, document), encoding='latin-1', errors='replace')


_DCM_KINDS = {
    'FESTWERT': 'VALUE',
    'FESTWERTEBLOCK': 'VAL_BLK',
    'TEXTSTRING': 'ASCII',
    'STUETZSTELLENVERTEILUNG': 'AXIS_PTS',
    'KENNLINIE': 'CURVE',
    'FESTKENNLINIE': 'CURVE',
    'GRUPPENKENNLINIE': 'CURVE',
    'KENNFELD': 'MAP',
    'FESTKENNFELD': 'MAP',
    'GRUPPENKENNFELD': 'MAP',
}


def _dcm_tokens(text: str) -> List[Physical]:
    try:
        parts = shlex.split(text, posix=True)
    except ValueError:
        parts = text.split()
    values: List[Physical] = []
    for part in parts:
        try:
            values.append(float(part))
        except ValueError:
            values.append(part)
    return values


def parse_dcm(text: str) -> Dataset:
    """Read the values of a DCM file (FESTWERT, FESTWERTEBLOCK, TEXTSTRING, STUETZSTELLENVERTEILUNG and the
    KENNLINIE / KENNFELD families)."""
    dataset: Dataset = {}
    current: Optional[Dict[str, Any]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(('*', '!', '.')):
            continue
        keyword, _, rest = line.partition(' ')
        rest = rest.strip()
        if current is None:
            if keyword in _DCM_KINDS:
                parts = rest.split()
                current = {'name': parts[0] if parts else '', 'kind': _DCM_KINDS[keyword], 'values': [], 'x': [],
                           'y': [], 'rows': [], 'block': parts[1:]}  # fmt: skip
            continue
        if keyword == 'END':
            dataset[current['name']] = _dcm_entry(current)
            current = None
        elif keyword in ('WERT', 'TEXT'):
            values = _dcm_tokens(rest)
            current['values'] += values
            if current['kind'] == 'MAP':
                if not current['rows']:
                    current['rows'].append([])
                current['rows'][-1] += values
        elif keyword in ('ST/X', 'ST_TX/X'):
            current['x'] += _dcm_tokens(rest)
        elif keyword in ('ST/Y', 'ST_TX/Y'):
            current['y'] += _dcm_tokens(rest)
            current['rows'].append([])
        elif keyword == 'EINHEIT_W':
            current['unit'] = rest.strip('"')
    return dataset


def _dcm_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    kind = entry['kind']
    result: Dict[str, Any] = {'kind': kind}
    if entry.get('unit'):
        result['unit'] = entry['unit']
    if kind == 'VALUE':
        result['value'] = entry['values'][0] if entry['values'] else 0.0
    elif kind == 'ASCII':
        result['value'] = str(entry['values'][0]) if entry['values'] else ''
    elif kind == 'AXIS_PTS':
        result['value'] = entry['x']
    elif kind == 'CURVE':
        result['value'] = entry['values']
        result['axes'] = [entry['x']]
    elif kind == 'MAP':
        result['value'] = entry['rows']
        result['axes'] = [entry['x'], entry['y']]
    elif '@' in entry['block'] and entry['block'][0].isdigit():  # FESTWERTEBLOCK name nx @ ny
        width = int(entry['block'][0]) or 1
        values = entry['values']
        result['value'] = [values[i : i + width] for i in range(0, len(values), width)]
    else:
        result['value'] = entry['values']
    return result


def load_dcm(path: Union[str, Path]) -> Dataset:
    return parse_dcm(Path(path).read_text(encoding='latin-1'))


def load_dataset(path: Union[str, Path]) -> Dataset:
    return load_dcm(path) if str(path).lower().endswith('.dcm') else load_json(path)


def save_dataset(path: Union[str, Path], dataset: Dataset, document: Optional[A2lDocument] = None) -> None:
    if str(path).lower().endswith('.dcm'):
        save_dcm(path, dataset, document)
    else:
        save_json(path, dataset, document)


def daq_size(variable: A2lVariable) -> int:
    """Bytes DAQ measures for a variable (a measurement or scalar characteristic)."""
    return variable.size if variable.data_type in DATA_TYPES else 0
