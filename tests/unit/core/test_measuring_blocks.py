import json

import pytest

from can_explorer.core.measuring_blocks import (
    FormulaTable,
    MeasuringBlockError,
    parse_measuring_response,
    parse_measuring_values,
    read_measuring_block,
)
from tests.unit.core.test_tp20 import GROUP_1

# Illustrative formulas for the tests only — not a statement of VW's formula table
TABLE = {
    'formulas': {'0x1A': {'expression': 'a * b / 10', 'unit': 'u'}, '25': 'a - b', '0x22': {'expression': 'c'}},
    'groups': {'1': ['First', 'Second', 'Third', 'Fourth']},
}


def test_example_response():
    block = parse_measuring_response(GROUP_1)
    assert block.group == 1
    assert len(block.values) == 8
    assert (block.values[3].formula, block.values[3].a, block.values[3].b) == (0x1A, 0x32, 0x4B)
    assert block.rest == b''
    assert block.describe().startswith('group 1: formula 0x01, a=0, b=0; formula 0x27')


def test_layout_that_is_not_triplets_keeps_the_rest():
    block = parse_measuring_values(7, bytes.fromhex('01 02 03 04 05'))
    assert len(block.values) == 1
    assert block.rest == bytes.fromhex('0405')
    assert block.as_dict()['unparsed'] == '04 05'
    assert 'unparsed 04 05' in block.describe()


def test_not_a_measuring_block_response():
    with pytest.raises(MeasuringBlockError):
        parse_measuring_response(bytes.fromhex('7F2131'))


def test_formula_table_scales_and_names():
    block = FormulaTable.from_dict(TABLE).apply(parse_measuring_response(GROUP_1))
    first, second, third, fourth = block.values[:4]
    assert first.name == 'First' and first.value is None and first.text == 'formula 0x01, a=0, b=0'
    assert fourth.name == 'Fourth' and fourth.value == pytest.approx(0x32 * 0x4B / 10) and fourth.unit == 'u'
    assert fourth.text == f'{0x32 * 0x4B / 10:g} u'
    assert 'unknown name c' in third.error and third.value is None  # A bad expression is reported per value
    assert block.values[4].value == 0x02 - 0x7A and block.values[4].name == ''  # Formula '25' is hex 0x25
    assert block.as_dict()['values'][3] == {
        'formula': '0x1A',
        'a': 0x32,
        'b': 0x4B,
        'name': 'Fourth',
        'value': pytest.approx(375.0),
        'unit': 'u',
    }
    assert 'Fourth: 375 u' in block.describe()


def test_formula_table_load(tmp_path):
    path = tmp_path / 'formulas.json'
    path.write_text(json.dumps(TABLE), encoding='utf-8')
    table = FormulaTable.load(path)
    assert table.formulas[0x1A].unit == 'u' and table.groups[1][0] == 'First'
    with pytest.raises(MeasuringBlockError, match='Cannot read'):
        FormulaTable.load(tmp_path / 'missing.json')
    path.write_text('{', encoding='utf-8')
    with pytest.raises(MeasuringBlockError, match='Cannot read'):
        FormulaTable.load(path)


@pytest.mark.parametrize(
    'data, message',
    [
        ([], 'JSON object'),
        ({'formulas': {'0x01': {'unit': 'x'}}}, 'expression'),
        ({'formulas': {'zz': 'a'}}, 'Invalid key'),
        ({'groups': {'1': 'Engine speed'}}, 'list the names'),
        ({'groups': {'one': ['x']}}, 'Invalid key'),
    ],
)
def test_formula_table_errors(data, message):
    with pytest.raises(MeasuringBlockError, match=message):
        FormulaTable.from_dict(data)


def test_group_keys_accept_hex():
    assert FormulaTable.from_dict({'groups': {'0x10': ['x']}}).groups == {16: ['x']}


async def test_read_measuring_block_with_a_client():
    class Client:
        async def read_local_identifier(self, identifier):
            assert identifier == 1
            return GROUP_1[2:]

    block = await read_measuring_block(Client(), 1, FormulaTable.from_dict(TABLE))
    assert block.values[3].value == pytest.approx(375.0)
    with pytest.raises(MeasuringBlockError, match='0-255'):
        await read_measuring_block(Client(), 256)
