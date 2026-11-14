import json

import pytest

from can_explorer.core import audit
from can_explorer.core.audit import AuditLog
from can_explorer.core.keystore import KeyStore

KEY = b'k' * 32


def lines(log):
    return log.path.read_text().splitlines()


def write(log, records):
    log.path.write_text('\n'.join(records) + '\n')


@pytest.fixture
def log(tmp_path):
    log = AuditLog(tmp_path / 'audit.jsonl', lambda: KEY)
    for index in range(4):
        log.record('entry used', entry=f'key {index}', skipped=None)
    return log


def test_records_and_verification(log):
    records = log.records()
    assert [record['details'] for record in records] == [{'entry': f'key {index}'} for index in range(4)]
    assert records[0]['prev'] == audit.GENESIS and records[1]['prev'] == records[0]['mac']
    result = log.verify(KEY)
    assert result.ok and (result.total, result.signed, result.unsigned) == (4, 4, 0)
    assert result.describe() == '4 records, chain intact'
    assert log.records(limit=2) == records[2:]
    reopened = AuditLog(log.path, lambda: KEY)  # Continues the chain of an existing file
    reopened.record('lock')
    assert reopened.verify(KEY).ok and reopened.records()[-1]['prev'] == records[-1]['mac']


def test_tampering_is_detected(log):
    original = lines(log)
    changed = json.loads(original[1])
    changed['details']['entry'] = 'another key'
    write(log, [original[0], json.dumps(changed, sort_keys=True), *original[2:]])
    assert log.verify(KEY).describe() == 'The audit log was altered: record 2 was changed'
    write(log, [original[0], *original[2:]])
    assert log.verify(KEY).problem == 'record 2 does not follow record 1'
    write(log, [original[1], original[0], *original[2:]])
    assert not log.verify(KEY).ok
    write(log, original)
    assert not log.verify(b'x' * 32).ok  # A forger without the key cannot re-sign
    assert log.verify(None).problem == 'the key store is locked: unlock it to verify'
    write(log, [*original, 'not json'])
    assert log.verify(KEY).problem == 'record 5 is not a valid record'


def test_unsigned_records(tmp_path):
    key = [None]
    log = AuditLog(tmp_path / 'audit.jsonl', lambda: key[0])
    log.record('unlock failed', error='Wrong password')
    key[0] = KEY
    log.record('unlock')
    result = log.verify(KEY)
    assert result.ok and (result.signed, result.unsigned) == (1, 1)
    assert result.describe().endswith('(1 unsigned, written while the key store was locked)')


def test_module_level_log(tmp_path):
    audit.set_log(None)
    audit.record('ignored')  # No log: nothing happens
    log = AuditLog(tmp_path / 'audit.jsonl')
    audit.set_log(log)
    try:
        audit.record('secoc attack frame', pdu='Speed', attack='replay')
        assert log.records()[0]['action'] == 'secoc attack frame' and not log.records()[0]['signed']
        assert audit.get_log() is log
    finally:
        audit.set_log(None)
    assert audit.log_path_for(tmp_path / 'keystore.json') == tmp_path / 'keystore-audit.jsonl'


def test_audit_key_in_the_key_store(tmp_path):
    store = KeyStore(tmp_path / 'keys.json', scrypt_cost=16)
    assert store.audit_key() is None  # No store yet
    store.create('pw')
    key = store.audit_key()
    assert len(key) == 32 and store.audit_key() == key
    assert key.hex() not in store.path.read_text()
    store.change_password('pw', 'new')
    reopened = KeyStore(store.path, scrypt_cost=16)
    assert reopened.audit_key() is None  # Locked
    reopened.unlock('new')
    assert reopened.audit_key() == key  # The same key after the password change: old records still verify
