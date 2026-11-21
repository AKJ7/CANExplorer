import json

import pytest

from can_explorer.core.diagnostic_labels import LabelError, LabelFile

# Illustrative values for the tests only
LABELS = {
    'formulas': {'0x01': {'expression': 'a * b', 'unit': 'x'}, '0x02': 'a'},
    'groups': {'1': ['Shared name']},
    'modules': {
        '01': {
            'name': 'Engine',
            'formulas': {'0x02': {'expression': 'b', 'unit': 'y'}},
            'groups': {'1': ['First', 'Second']},
            'coding': {
                'Coding': {
                    'protocol': 'kwp',
                    'identifier': '0x9A',
                    'length': 3,
                    'fields': [{'name': 'Option A', 'byte': 0, 'bit': 0}],
                },
                'Other': {'protocol': 'uds', 'identifier': '0x0600'},
            },
        },
        '0x17': {'name': 'Cluster', 'coding': {'Coding': {'identifier': 1}}},
    },
}


def test_modules_and_names():
    labels = LabelFile.from_dict(LABELS)
    assert labels.module_name(0x01) == 'Engine' and labels.module_name(0x17) == 'Cluster'
    assert labels.module_name(0x02) == '' and labels.module(0x02) is None


def test_formula_table_merges_shared_and_module():
    labels = LabelFile.from_dict(LABELS)
    engine = labels.formula_table(0x01)
    assert engine.formulas[0x01].expression == 'a * b'  # Shared
    assert engine.formulas[0x02].expression == 'b' and engine.formulas[0x02].unit == 'y'  # Overridden
    assert engine.groups[1] == ['First', 'Second']
    other = labels.formula_table(0x42)
    assert other.formulas[0x02].expression == 'a' and other.groups[1] == ['Shared name']
    assert labels.formula_table().formulas.keys() == {0x01, 0x02}


def test_coding_records():
    labels = LabelFile.from_dict(LABELS)
    assert [r.name for r in labels.coding_records(0x01)] == ['Coding', 'Other']
    assert labels.coding_record(0x01, 'Other').protocol == 'uds'
    assert labels.coding_record(0x17).identifier == 1 and labels.coding_record(0x17).protocol == 'kwp'
    with pytest.raises(LabelError, match='several coding records'):
        labels.coding_record(0x01)
    with pytest.raises(LabelError, match='no coding record .Missing.'):
        labels.coding_record(0x01, 'Missing')
    with pytest.raises(LabelError, match='no coding record for module 0x02'):
        labels.coding_record(0x02)


def test_a_formula_table_is_a_label_file():
    labels = LabelFile.from_dict({'formulas': {'0x1A': 'a + b'}, 'groups': {'1': ['x']}})
    assert labels.formula_table(0x01).formulas[0x1A].expression == 'a + b' and labels.modules == {}


def test_load(tmp_path):
    path = tmp_path / 'labels.json'
    path.write_text(json.dumps(LABELS), encoding='utf-8')
    labels = LabelFile.load(path)
    assert labels.path == path and labels.module_name(0x01) == 'Engine'
    with pytest.raises(LabelError, match='Cannot read'):
        LabelFile.load(tmp_path / 'missing.json')


@pytest.mark.parametrize(
    'data, message',
    [
        ([], 'JSON object'),
        ({'modules': {'zz': {}}}, 'Invalid module address'),
        ({'modules': {'100': {}}}, 'out of range'),
        ({'modules': {'01': []}}, 'must be an object'),
        ({'modules': {'01': {'coding': []}}}, 'named records'),
        ({'modules': {'01': {'coding': {'C': {'protocol': 'xcp', 'identifier': 1}}}}}, 'protocol'),
        ({'formulas': {'0x01': {}}}, 'expression'),
    ],
)
def test_errors(data, message):
    with pytest.raises(LabelError, match=message):
        LabelFile.from_dict(data)
