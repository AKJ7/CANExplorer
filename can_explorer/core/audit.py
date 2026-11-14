"""A tamper-evident audit log of what the security manager was used for: unlocking, entries added, removed,
exported and imported, keys used to secure or check frames, authentications, security tests and attack frames.

Each record is a JSON line linked to the one before: its `mac` is an HMAC-SHA-256, under a key kept encrypted in the
key store (`KeyStore.audit_key`), over the previous record's mac and the record itself. Changing, removing or
reordering records breaks the chain, which `verify` reports. Records written while the key store is locked (e.g. a
failed unlock) cannot be signed; they are chained with SHA-256 only and counted as unsigned — anyone could rewrite
those, and the tail of a log can always be cut off: the log shows that recorded history was not altered, it does not
prove that nothing happened afterwards.

The log is a file next to the key store (`<key store>-audit.jsonl`). `record()` writes to the application's log
(set by the security manager) and does nothing when there is none, so the core modules can call it freely.
"""

import datetime
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

GENESIS = '0' * 64


def _canonical(record: Dict[str, Any]) -> bytes:
    return json.dumps({key: value for key, value in record.items() if key != 'mac'}, sort_keys=True).encode('utf-8')


def _mac(key: Optional[bytes], record: Dict[str, Any]) -> str:
    data = _canonical(record)
    if key is None:
        return hashlib.sha256(data).hexdigest()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


@dataclass
class Verification:
    ok: bool
    total: int
    signed: int
    unsigned: int
    problem: str = ''  # The first record that does not fit, and why

    def describe(self) -> str:
        if not self.ok:
            return f'The audit log was altered: {self.problem}'
        text = f'{self.total} records, chain intact'
        return text + (f' ({self.unsigned} unsigned, written while the key store was locked)' if self.unsigned else '')


class AuditLog:
    def __init__(self, path: Path, key: Callable[[], Optional[bytes]] = lambda: None):
        """`key` returns the HMAC key, or None when it is not available (the key store is locked)."""
        self.path = Path(path)
        self._key = key
        self._last: Optional[str] = None

    def _last_mac(self) -> str:
        if self._last is None:
            records = self.records()
            self._last = records[-1]['mac'] if records else GENESIS
        return self._last

    def record(self, action: str, **details: Any) -> Dict[str, Any]:
        key = self._key()
        entry: Dict[str, Any] = {
            'time': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds'),
            'action': action,
            'details': {name: value for name, value in details.items() if value is not None},
            'signed': key is not None,
            'prev': self._last_mac(),
        }
        entry['mac'] = _mac(key, entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as file:
            file.write(json.dumps(entry, sort_keys=True) + '\n')
        self._last = entry['mac']
        return entry

    def records(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """The records, oldest first (the last `limit` with a limit). Unreadable lines are skipped here; `verify`
        reports them."""
        if not self.path.is_file():
            return []
        records = []
        for line in self.path.read_text(encoding='utf-8').splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records[-limit:] if limit else records

    def verify(self, key: Optional[bytes]) -> Verification:
        """Check the chain with the HMAC key (signed records need it; without it they cannot be verified)."""
        if not self.path.is_file():
            return Verification(True, 0, 0, 0)
        previous, signed, unsigned = GENESIS, 0, 0
        lines = [line for line in self.path.read_text(encoding='utf-8').splitlines() if line.strip()]
        for number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
                expected = _mac(key if record['signed'] else None, record)
            except (ValueError, KeyError, TypeError):
                return Verification(False, len(lines), signed, unsigned, f'record {number} is not a valid record')
            if record.get('prev') != previous:
                return Verification(
                    False, len(lines), signed, unsigned, f'record {number} does not follow record {number - 1}'
                )
            if record['signed'] and key is None:
                return Verification(False, len(lines), signed, unsigned, 'the key store is locked: unlock it to verify')
            if not hmac.compare_digest(expected, str(record.get('mac', ''))):
                return Verification(False, len(lines), signed, unsigned, f'record {number} was changed')
            signed, unsigned = signed + record['signed'], unsigned + (not record['signed'])
            previous = record['mac']
        return Verification(True, len(lines), signed, unsigned)


_log: Optional[AuditLog] = None


def set_log(log: Optional[AuditLog]) -> None:
    global _log
    _log = log


def get_log() -> Optional[AuditLog]:
    return _log


def record(action: str, **details: Any) -> None:
    """Add a record to the application's audit log, if there is one (never raises)."""
    if _log is None:
        return
    try:
        _log.record(action, **details)
    except Exception:  # noqa: BLE001 - auditing must not break the operation it records
        pass


def log_path_for(store_path: Path) -> Path:
    store_path = Path(store_path)
    return store_path.with_name(f'{store_path.stem}-audit.jsonl')
