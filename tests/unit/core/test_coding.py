import pytest

from can_explorer.core.coding import (
    CodingError,
    CodingField,
    CodingRecord,
    byte_changes,
    field_changes,
    parse_field,
    parse_record,
    write_record,
)

# Illustrative layout for the tests only
RECORD = CodingRecord(
    'Coding',
    'kwp',
    0x9A,
    (
        CodingField('Option A', 0, 0, 1, {0: 'off', 1: 'on'}),
        CodingField('Variant', 1, 4, 4),
        CodingField('Region', 2),
    ),
    length=3,
)


class FakeKwp:
    def __init__(self, data: bytes, keeps_writes: bool = True):
        self.data, self.keeps_writes = bytearray(data), keeps_writes
        self.writes = []

    async def read_local_identifier(self, identifier):
        assert identifier == 0x9A
        return bytes(self.data)

    async def write_local_identifier(self, identifier, data):
        self.writes.append((identifier, bytes(data)))
        if self.keeps_writes:
            self.data[:] = data


class FakeUds:
    def __init__(self, data: bytes):
        self.data = bytes(data)

    async def read_data_by_identifier(self, did):
        assert did == 0x0600
        return self.data

    async def write_data_by_identifier(self, did, data):
        self.data = bytes(data)


def test_field_bits():
    field = CodingField('Variant', 1, 4, 4)
    assert field.mask == 0xF0 and field.maximum == 15 and field.location() == 'byte 1 bits 4-7'
    assert field.decode(bytes([0x00, 0xA5])) == 0xA
    assert field.encode(bytes([0x00, 0xA5]), 0x3) == bytes([0x00, 0x35])
    assert CodingField('A', 0, 3, 1).location() == 'byte 0 bit 3' and CodingField('B', 2).location() == 'byte 2'
    with pytest.raises(CodingError, match='takes 0-15'):
        field.encode(bytes(2), 16)
    with pytest.raises(CodingError, match='record has 1 bytes'):
        field.decode(b'\x00')
    for bit, width in ((7, 2), (8, 1), (0, 0), (0, 9)):
        with pytest.raises(CodingError, match='do not fit'):
            CodingField('X', 0, bit, width)
    with pytest.raises(CodingError):
        CodingField('X', -1)


def test_field_text():
    field = RECORD.field('Option A')
    assert field.text(1) == '1 (on)' and field.text(5) == '5'


def test_record_decode_and_apply():
    values = {v.field.name: v for v in RECORD.decode(bytes([0x01, 0x30, 0x07]))}
    assert values['Option A'].text == '1 (on)' and values['Variant'].value == 3 and values['Region'].value == 7
    assert values['Option A'].as_dict() == {
        'name': 'Option A',
        'location': 'byte 0 bit 0',
        'value': 1,
        'meaning': 'on',
        'options': {'0': 'off', '1': 'on'},
    }
    assert [v.field.name for v in RECORD.decode(b'\x01')] == ['Option A']  # A short record covers fewer fields
    edited = RECORD.apply(bytes([0x01, 0x30, 0x07]), {'Option A': 0, 'Variant': 9})
    assert edited == bytes([0x00, 0x90, 0x07])
    assert [(f.name, a, b) for f, a, b in field_changes(RECORD, bytes([0x01, 0x30, 0x07]), edited)] == [
        ('Option A', 1, 0),
        ('Variant', 3, 9),
    ]
    assert [(c.index, c.before, c.after) for c in byte_changes(bytes([1, 0x30, 7]), edited)] == [
        (0, 1, 0),
        (1, 0x30, 0x90),
    ]
    with pytest.raises(CodingError, match='no field'):
        RECORD.apply(bytes(3), {'Missing': 1})
    with pytest.raises(CodingError):
        byte_changes(b'\x00', b'\x00\x00')


def test_record_validation():
    assert RECORD.location() == 'KWP2000 local identifier 0x9A'
    assert CodingRecord('C', 'uds', 0x0600).location() == 'UDS data identifier 0x0600'
    with pytest.raises(CodingError, match='protocol'):
        CodingRecord('C', 'xcp', 1)
    with pytest.raises(CodingError, match='out of range'):
        CodingRecord('C', 'kwp', 0x100)
    with pytest.raises(CodingError, match='unique'):
        CodingRecord('C', 'kwp', 1, (CodingField('A', 0), CodingField('A', 1)))


async def test_write_record_reads_backup_writes_and_verifies():
    module = FakeKwp(bytes([0x01, 0x30, 0x07]))
    result = await write_record(module, RECORD, bytes([0x00, 0x30, 0x07]))
    assert result.before == bytes([0x01, 0x30, 0x07]) and result.verified
    assert module.writes == [(0x9A, bytes([0x00, 0x30, 0x07]))]
    info = result.as_dict()
    assert info['verified'] and info['changed_bytes'] == [{'byte': 0, 'before': '01', 'after': '00'}]
    assert info['location'] == 'KWP2000 local identifier 0x9A'


async def test_write_record_refusals():
    module = FakeKwp(bytes([0x01, 0x30, 0x07]))
    with pytest.raises(CodingError, match='is 3 bytes'):
        await write_record(module, RECORD, bytes(4))
    with pytest.raises(CodingError, match='changed in the module'):
        await write_record(module, RECORD, bytes(3), expected_before=bytes([0x02, 0x30, 0x07]))
    unsized = CodingRecord('Raw', 'kwp', 0x9A)
    with pytest.raises(CodingError, match='holds 3 bytes'):
        await write_record(module, unsized, bytes(2))
    assert module.writes == []
    unchanged = await write_record(module, RECORD, bytes([0x01, 0x30, 0x07]))
    assert unchanged.verified and module.writes == []  # Nothing to write


async def test_write_record_not_kept_by_the_module():
    module = FakeKwp(bytes([0x01, 0x30, 0x07]), keeps_writes=False)
    result = await write_record(module, RECORD, bytes([0x00, 0x30, 0x07]))
    assert not result.verified and result.read_back == bytes([0x01, 0x30, 0x07])
    unverified = await write_record(module, RECORD, bytes([0x00, 0x30, 0x07]), verify=False)
    assert unverified.read_back is None and not unverified.verified


async def test_uds_record():
    record = CodingRecord('Long coding', 'uds', 0x0600)
    module = FakeUds(bytes(4))
    assert await record.read(module) == bytes(4)
    assert (await write_record(module, record, b'\x01\x02\x03\x04')).verified


def test_parse_definitions():
    record = parse_record(
        'Coding',
        {
            'protocol': 'KWP',
            'identifier': '0x9A',
            'length': 3,
            'fields': [
                {'name': 'Option A', 'byte': 0, 'bit': 0, 'values': {'0': 'off', '1': 'on'}},
                {'name': 'Variant', 'byte': '1', 'bit': 4, 'width': 4, 'description': 'Variant code'},
                {'name': 'Region', 'byte': 2},
            ],
        },
    )
    assert record == RECORD.__class__(
        'Coding',
        'kwp',
        0x9A,
        (
            CodingField('Option A', 0, 0, 1, {0: 'off', 1: 'on'}),
            CodingField('Variant', 1, 4, 4, {}, 'Variant code'),
            CodingField('Region', 2),
        ),
        3,
    )


@pytest.mark.parametrize(
    'definition, message',
    [
        ({'identifier': '0x9A', 'fields': [{'byte': 0}]}, 'needs a "name"'),
        ({'identifier': '0x9A', 'fields': [{'name': 'A'}]}, 'byte'),
        ({'identifier': '0x9A', 'fields': [{'name': 'A', 'byte': 'x'}]}, 'Invalid'),
        ({'identifier': '0x9A', 'fields': {'name': 'A'}}, 'must be a list'),
        ({'fields': []}, 'identifier'),
        ({'identifier': True}, 'identifier'),
        ([], 'must be an object'),
    ],
)
def test_parse_errors(definition, message):
    with pytest.raises(CodingError, match=message):
        parse_record('Coding', definition)


def test_parse_field_rejects_non_objects():
    with pytest.raises(CodingError):
        parse_field('Option A')
