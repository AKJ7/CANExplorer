"""The security manager's key store: SecOC keys, TLS certificates and IPsec/MACsec security associations.

One JSON file per user (`default_path()`, or the `CANEXPLORER_KEYSTORE` environment variable). Each entry has
public metadata (name, purpose, parameters such as the SecOC scope, an SPI or a certificate) that can be listed
without the password, and secrets (keys, private keys) that are encrypted:

- a 256-bit key is derived from the password with scrypt (a fresh random salt per store);
- the secrets of each entry are encrypted with AES-256-GCM under that key, with a fresh 96-bit nonce, and the
  entry's name, purpose and parameters as associated data, so editing an entry's metadata in the file makes its
  secrets fail to decrypt instead of applying them elsewhere;
- a check value (a constant encrypted the same way) tells a wrong password from a damaged entry.

Unlocking keeps the derived key in memory until `lock()`; secrets are only decrypted on use. The file is written
atomically and readable by its owner only. Secrets never appear in `repr`, in `entries()` or in log messages.

For headless use (`can-test`, scripts) the password can be given in `CANEXPLORER_KEYSTORE_PASSWORD`.
"""

import ast
import base64
import datetime
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = 'canexplorer-keystore'
BUNDLE_FORMAT = 'canexplorer-keybundle'  # Entries exported with a passphrase of their own
VERSION = 1
FILE_NAME = 'keystore.json'
APP_DIR = 'CANExplorer'
PATH_VARIABLE = 'CANEXPLORER_KEYSTORE'
PASSWORD_VARIABLE = 'CANEXPLORER_KEYSTORE_PASSWORD'
SCRYPT_COST = 2**15  # scrypt N: about 0.1 s and 32 MiB per unlock
_CHECK = b'canexplorer-keystore'

# Purposes
SECOC = 'secoc'  # A SecOC key: secrets {'key'}; params scope ('*', PDU names or Data IDs), algorithm
TLS_CLIENT = 'tls-client'  # A TLS client certificate: params certificate (PEM) …; secrets {'private_key'} (PEM)
TLS_CA = 'tls-ca'  # Trusted TLS certificates (PEM), no secrets
IPSEC = 'ipsec'  # An IPsec ESP security association: params spi, encryption, integrity; secrets the keys
MACSEC = 'macsec'  # A MACsec secure association: params sci ('' for any); secrets {'sak'}
SEED_KEY = 'seed-key'  # A UDS seed/key algorithm: params file (its original name); secrets {'script'} (Python)
CRL = 'crl'  # A certificate revocation list (public): params issuer, updated, next_update, revoked, crl (PEM)
KEY_SERVER = (
    'key-server'  # A key server (core/security_profiles.py): params url, trusted (a tls-ca entry); secrets token
)
AUTH_KEY = 'uds-auth-key'  # A shared key for UDS Authentication by challenge-response: params mac; secrets {'key'}
PURPOSES = (SECOC, TLS_CLIENT, TLS_CA, CRL, IPSEC, MACSEC, SEED_KEY, AUTH_KEY, KEY_SERVER)
EXPIRY_WARNING_DAYS = 30  # Certificates expiring within this many days are announced

SECOC_ALGORITHMS = ('', 'cmac-aes', 'hmac-sha256')  # '' takes the algorithm named in the ARXML
AUTH_KEY_MACS = ('hmac-sha256', 'aes-cmac')  # core/uds_auth.PROOF_MACS
ESP_ENCRYPTIONS = ('aes-gcm', 'aes-cbc', 'null')
ESP_INTEGRITIES = ('none', 'hmac-sha256-128', 'hmac-sha1-96')


class KeyStoreError(Exception):
    pass


class WrongPassword(KeyStoreError):
    def __init__(self) -> None:
        super().__init__('Wrong password for the key store')


class Locked(KeyStoreError):
    def __init__(self) -> None:
        super().__init__('The key store is locked; unlock it with its password')


def default_path(
    environ: Mapping[str, str] = os.environ, platform_name: str = sys.platform, home: Optional[Path] = None
) -> Path:
    """The per-user key store file: `CANEXPLORER_KEYSTORE`, else the platform's place for application data."""
    if environ.get(PATH_VARIABLE):
        return Path(environ[PATH_VARIABLE])
    home = Path.home() if home is None else home
    if platform_name == 'win32':
        base = Path(environ.get('APPDATA') or home / 'AppData' / 'Roaming')
    elif platform_name == 'darwin':
        base = home / 'Library' / 'Application Support'
    else:
        base = Path(environ.get('XDG_CONFIG_HOME') or home / '.config')
    return base / APP_DIR / FILE_NAME


@dataclass(frozen=True)
class KeyInfo:
    """The public part of an entry: what `entries()` lists, without unlocking."""

    name: str
    purpose: str
    params: Dict[str, str] = field(default_factory=dict)
    secret_names: tuple = ()

    def as_dict(self) -> Dict[str, object]:
        return {'name': self.name, 'purpose': self.purpose, **self.params, 'secrets': list(self.secret_names)}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode('ascii'), validate=True)


def _associated_data(name: str, purpose: str, params: Mapping[str, str]) -> bytes:
    return json.dumps({'name': name, 'purpose': purpose, 'params': dict(params)}, sort_keys=True).encode('utf-8')


class KeyStore:
    """An encrypted key store file. Create it with `create(password)`, or open an existing one with `unlock`."""

    def __init__(self, path: Optional[Path] = None, scrypt_cost: int = SCRYPT_COST):
        self.path = Path(path) if path is not None else default_path()
        self._scrypt_cost = scrypt_cost
        self._key: Optional[bytes] = None
        self._data: Optional[dict] = None
        self._stamp: Optional[tuple] = None  # (mtime, size) of the file when it was last read

    def __repr__(self) -> str:
        return f'KeyStore({str(self.path)!r}, {"unlocked" if self.unlocked else "locked"})'

    # File

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _load(self) -> dict:
        """The parsed file, re-read when it changed on disk."""
        try:
            stamp = self._file_stamp()
        except OSError as e:
            raise KeyStoreError(f'No key store at {self.path} yet; create one first') from e
        if self._data is not None and stamp == self._stamp:
            return self._data
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise KeyStoreError(f'Cannot read the key store {self.path}: {e}') from e
        if not isinstance(data, dict) or data.get('format') != FORMAT:
            raise KeyStoreError(f'{self.path} is not a CANExplorer key store')
        if data.get('version') != VERSION:
            raise KeyStoreError(f'Key store version {data.get("version")} is not supported (expected {VERSION})')
        self._data, self._stamp = data, stamp
        return data

    def _file_stamp(self) -> tuple:
        status = self.path.stat()
        return status.st_mtime_ns, status.st_size

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2)
        descriptor, temporary = tempfile.mkstemp(prefix='.keystore-', dir=self.path.parent)
        try:
            if hasattr(os, 'fchmod'):
                os.fchmod(descriptor, 0o600)  # Owner only, before anything is written
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                file.write(text)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        self._data, self._stamp = data, self._file_stamp()

    # Password

    def _derive(self, password: str, kdf: dict) -> bytes:
        return Scrypt(salt=_unb64(kdf['salt']), length=32, n=int(kdf['n']), r=int(kdf['r']), p=int(kdf['p'])).derive(
            password.encode('utf-8')
        )

    def create(self, password: str) -> None:
        """Create an empty key store protected by `password`, and unlock it."""
        if self.exists:
            raise KeyStoreError(f'A key store exists already at {self.path}')
        if not password:
            raise KeyStoreError('The key store needs a password')
        kdf = {'name': 'scrypt', 'salt': _b64(os.urandom(16)), 'n': self._scrypt_cost, 'r': 8, 'p': 1}
        key = self._derive(password, kdf)
        nonce = os.urandom(12)
        check = {'nonce': _b64(nonce), 'data': _b64(AESGCM(key).encrypt(nonce, _CHECK, b'check'))}
        self._save({'format': FORMAT, 'version': VERSION, 'kdf': kdf, 'check': check, 'entries': []})
        self._key = key

    def unlock(self, password: str) -> None:
        data = self._load()
        try:
            key = self._derive(password, data['kdf'])
            check = data['check']
            if AESGCM(key).decrypt(_unb64(check['nonce']), _unb64(check['data']), b'check') != _CHECK:
                raise WrongPassword()
        except InvalidTag:
            raise WrongPassword() from None
        except (KeyError, TypeError, ValueError) as e:
            raise KeyStoreError(f'The key store {self.path} is damaged: {e}') from e
        self._key = key

    def unlock_from_environment(self, environ: Mapping[str, str] = os.environ) -> bool:
        """Unlock with `CANEXPLORER_KEYSTORE_PASSWORD` if it is set and the store exists. Raises on a wrong one."""
        password = environ.get(PASSWORD_VARIABLE)
        if not password or not self.exists:
            return False
        self.unlock(password)
        return True

    def lock(self) -> None:
        self._key = None

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    def change_password(self, old: str, new: str) -> None:
        """Re-encrypt every entry under a new password."""
        if not new:
            raise KeyStoreError('The key store needs a password')
        self.unlock(old)
        decrypted = [(entry, self._decrypt(entry)) for entry in self._load()['entries']]
        audit_key = self._audit_key_if_any()
        kdf = {'name': 'scrypt', 'salt': _b64(os.urandom(16)), 'n': self._scrypt_cost, 'r': 8, 'p': 1}
        self._key = self._derive(new, kdf)
        nonce = os.urandom(12)
        data = dict(self._load())
        data['kdf'] = kdf
        data['check'] = {'nonce': _b64(nonce), 'data': _b64(AESGCM(self._key).encrypt(nonce, _CHECK, b'check'))}
        data['entries'] = [self._encrypt(entry['name'], entry['purpose'], entry['params'], s) for entry, s in decrypted]
        if audit_key is not None:
            data['audit'] = self._seal_audit_key(audit_key)
        self._save(data)

    # The audit log's HMAC key (core/audit.py), kept encrypted like the entries

    def _seal_audit_key(self, key: bytes) -> Dict[str, str]:
        nonce = os.urandom(12)
        return {'nonce': _b64(nonce), 'data': _b64(AESGCM(self._key).encrypt(nonce, key, b'audit'))}

    def _audit_key_if_any(self) -> Optional[bytes]:
        sealed = self._load().get('audit')
        if sealed is None or self._key is None:
            return None
        try:
            return AESGCM(self._key).decrypt(_unb64(sealed['nonce']), _unb64(sealed['data']), b'audit')
        except (InvalidTag, KeyError, ValueError) as e:
            raise KeyStoreError('The audit key of the key store is damaged') from e

    def audit_key(self) -> Optional[bytes]:
        """The audit log's HMAC key (created on first use); None while the store is locked or does not exist."""
        if self._key is None or not self.exists:
            return None
        key = self._audit_key_if_any()
        if key is None:
            key = os.urandom(32)
            data = dict(self._load())
            data['audit'] = self._seal_audit_key(key)
            self._save(data)
        return key

    # Entries

    def entries(self, purpose: Optional[str] = None) -> List[KeyInfo]:
        """The entries' public metadata (no password needed)."""
        if not self.exists:
            return []
        return [
            KeyInfo(
                entry['name'], entry['purpose'], dict(entry.get('params', {})), tuple(entry.get('secret_names', ()))
            )
            for entry in self._load()['entries']
            if purpose is None or entry['purpose'] == purpose
        ]

    def names(self) -> List[str]:
        return [info.name for info in self.entries()]

    def _entry(self, name: str) -> dict:
        for entry in self._load()['entries']:
            if entry['name'] == name:
                return entry
        raise KeyStoreError(f'No key store entry named {name!r}')

    def _encrypt(self, name: str, purpose: str, params: Mapping[str, str], secrets: Mapping[str, bytes]) -> dict:
        if self._key is None:
            raise Locked()
        nonce = os.urandom(12)
        plain = json.dumps({key: value.hex() for key, value in secrets.items()}).encode('utf-8')
        sealed = AESGCM(self._key).encrypt(nonce, plain, _associated_data(name, purpose, params))
        return {
            'name': name,
            'purpose': purpose,
            'params': dict(params),
            'secret_names': sorted(secrets),
            'nonce': _b64(nonce),
            'secrets': _b64(sealed),
        }

    def _decrypt(self, entry: dict) -> Dict[str, bytes]:
        if self._key is None:
            raise Locked()
        try:
            plain = AESGCM(self._key).decrypt(
                _unb64(entry['nonce']),
                _unb64(entry['secrets']),
                _associated_data(entry['name'], entry['purpose'], entry.get('params', {})),
            )
            return {key: bytes.fromhex(value) for key, value in json.loads(plain).items()}
        except (InvalidTag, KeyError, ValueError) as e:
            raise KeyStoreError(f'The key store entry {entry.get("name")!r} is damaged or was modified') from e

    def add(
        self,
        name: str,
        purpose: str,
        params: Optional[Mapping[str, str]] = None,
        secrets: Optional[Mapping[str, bytes]] = None,
        replace: bool = False,
    ) -> KeyInfo:
        """Add an entry (prefer the add_… helpers, which validate). Parameters are public; secrets are encrypted."""
        name = name.strip()
        if not name:
            raise KeyStoreError('An entry needs a name')
        if purpose not in PURPOSES:
            raise KeyStoreError(f'Unknown purpose {purpose!r}; one of {", ".join(PURPOSES)}')
        params = {key: str(value) for key, value in (params or {}).items()}
        data = dict(self._load())
        entries = [entry for entry in data['entries'] if entry['name'] != name]
        if len(entries) != len(data['entries']) and not replace:
            raise KeyStoreError(f'An entry named {name!r} exists already')
        entries.append(self._encrypt(name, purpose, params, dict(secrets or {})))
        data['entries'] = entries
        self._save(data)
        return self.entries_by_name()[name]

    def entries_by_name(self) -> Dict[str, KeyInfo]:
        return {info.name: info for info in self.entries()}

    def remove(self, name: str) -> None:
        data = dict(self._load())
        entries = [entry for entry in data['entries'] if entry['name'] != name]
        if len(entries) == len(data['entries']):
            raise KeyStoreError(f'No key store entry named {name!r}')
        data['entries'] = entries
        self._save(data)

    def secrets(self, name: str) -> Dict[str, bytes]:
        """The decrypted secrets of an entry (needs the store unlocked)."""
        return self._decrypt(self._entry(name))

    # Bundles: entries exported for another computer or a colleague

    def export_bundle(self, path: Path, names: Sequence[str], passphrase: str) -> int:
        """Write the named entries (decrypted, then encrypted under `passphrase`) to a bundle file. Returns how many."""
        if not passphrase:
            raise KeyStoreError('An export needs a passphrase')
        if not self.unlocked:
            raise Locked()
        by_name = {entry['name']: entry for entry in self._load()['entries']}
        missing = [name for name in names if name not in by_name]
        if missing:
            raise KeyStoreError(f'No key store entry named {missing[0]!r}')
        if not names:
            raise KeyStoreError('Choose the entries to export')
        entries = [
            {
                'name': name,
                'purpose': by_name[name]['purpose'],
                'params': by_name[name].get('params', {}),
                'secrets': {key: value.hex() for key, value in self._decrypt(by_name[name]).items()},
            }
            for name in names
        ]
        kdf = {'name': 'scrypt', 'salt': _b64(os.urandom(16)), 'n': self._scrypt_cost, 'r': 8, 'p': 1}
        key, nonce = self._derive(passphrase, kdf), os.urandom(12)
        sealed = AESGCM(key).encrypt(nonce, json.dumps(entries).encode('utf-8'), BUNDLE_FORMAT.encode('ascii'))
        bundle = {'format': BUNDLE_FORMAT, 'version': VERSION, 'kdf': kdf, 'nonce': _b64(nonce), 'data': _b64(sealed)}
        Path(path).write_text(json.dumps(bundle, indent=2), encoding='utf-8')
        return len(entries)

    def import_bundle(self, path: Path, passphrase: str, replace: bool = False) -> Tuple[List[str], List[str]]:
        """Add the entries of a bundle file. Returns (imported names, names skipped because they exist)."""
        if not self.unlocked:
            raise Locked()
        try:
            bundle = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise KeyStoreError(f'Cannot read {path}: {e}') from e
        if not isinstance(bundle, dict) or bundle.get('format') != BUNDLE_FORMAT:
            raise KeyStoreError(f'{path} is not a CANExplorer key bundle')
        try:
            key = self._derive(passphrase, bundle['kdf'])
            plain = AESGCM(key).decrypt(_unb64(bundle['nonce']), _unb64(bundle['data']), BUNDLE_FORMAT.encode('ascii'))
            entries = json.loads(plain)
        except InvalidTag:
            raise KeyStoreError('Wrong passphrase for the key bundle') from None
        except (KeyError, TypeError, ValueError) as e:
            raise KeyStoreError(f'The key bundle is damaged: {e}') from e
        existing = set(self.names())
        imported, skipped = [], []
        for entry in entries:
            if entry['name'] in existing and not replace:
                skipped.append(entry['name'])
                continue
            secrets = {key: bytes.fromhex(value) for key, value in entry.get('secrets', {}).items()}
            self.add(entry['name'], entry['purpose'], entry.get('params', {}), secrets, replace=True)
            imported.append(entry['name'])
        return imported, skipped

    # Validating helpers, one per purpose

    def add_secoc_key(
        self, name: str, key: bytes, scope: str = '*', algorithm: str = '', replace: bool = False
    ) -> KeyInfo:
        """A SecOC key for the PDUs in `scope`: '*' (all), or PDU names and Data IDs separated by commas
        (e.g. 'EngineData, 0x12'). `algorithm` '' uses the one the ARXML names."""
        if algorithm not in SECOC_ALGORITHMS:
            raise KeyStoreError(f'Unknown SecOC algorithm {algorithm!r}; one of {", ".join(SECOC_ALGORITHMS[1:])}')
        if algorithm in ('', 'cmac-aes') and len(key) not in (16, 24, 32):
            raise KeyStoreError(f'A CMAC/AES key has 16, 24 or 32 bytes, not {len(key)}')
        if not key:
            raise KeyStoreError('The key is empty')
        scope = ', '.join(part.strip() for part in (scope or '*').split(',') if part.strip()) or '*'
        return self.add(name, SECOC, {'scope': scope, 'algorithm': algorithm}, {'key': bytes(key)}, replace)

    def add_tls_client(
        self,
        name: str,
        certificate_pem: bytes,
        private_key_pem: bytes,
        key_password: Optional[bytes] = None,
        replace: bool = False,
    ) -> KeyInfo:
        """A TLS client certificate (PEM, optionally with its chain) and its private key, for mutual TLS."""
        certificates = _load_certificates(certificate_pem)
        try:
            private_key = serialization.load_pem_private_key(private_key_pem, password=key_password or None)
        except (ValueError, TypeError) as e:
            raise KeyStoreError(f'Cannot read the private key: {e}') from e
        public = (
            certificates[0]
            .public_key()
            .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        if (
            private_key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            != public
        ):
            raise KeyStoreError('The private key does not belong to the certificate')
        unencrypted = private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        params = _certificate_params(certificates)
        return self.add(name, TLS_CLIENT, params, {'private_key': unencrypted}, replace)

    def add_key_server(self, name: str, url: str, token: str = '', trusted: str = '', replace: bool = False) -> KeyInfo:
        """A key server answering the REST API of core/security_profiles.py, with its bearer token (kept encrypted)
        and optionally the trusted certificates its HTTPS certificate is checked against."""
        url = url.strip()
        if not url.startswith(('https://', 'http://')):
            raise KeyStoreError('A key server URL starts with https:// (or http:// for local tests)')
        if trusted and (
            self.entries_by_name().get(trusted) is None or self.entries_by_name()[trusted].purpose != TLS_CA
        ):
            raise KeyStoreError(f'No trusted certificates named {trusted!r}')
        return self.add(name, KEY_SERVER, {'url': url, 'trusted': trusted}, {'token': token.encode('utf-8')}, replace)

    def add_crl(self, name: str, data: bytes, replace: bool = False) -> KeyInfo:
        """A certificate revocation list (PEM or DER): certificates it lists are refused for UDS Authentication
        and DoIP TLS (when its issuer is trusted and its signature verifies)."""
        data = bytes(data)
        try:
            crl = (
                x509.load_pem_x509_crl(data)
                if data.lstrip().startswith(b'-----BEGIN')
                else x509.load_der_x509_crl(data)
            )
        except ValueError as e:
            raise KeyStoreError(f'Cannot read the revocation list (PEM or DER expected): {e}') from e
        next_update = crl.next_update_utc
        params = {
            'issuer': crl.issuer.rfc4514_string(),
            'updated': crl.last_update_utc.isoformat(timespec='seconds'),
            'next_update': next_update.isoformat(timespec='seconds') if next_update else '',
            'revoked': str(len(list(crl))),
            'crl': crl.public_bytes(serialization.Encoding.PEM).decode('ascii'),
        }
        return self.add(name, CRL, params, {}, replace)

    def revocation_lists(self) -> List[x509.CertificateRevocationList]:
        """The revocation lists of the store (readable while it is locked)."""
        return [x509.load_pem_x509_crl(info.params['crl'].encode('ascii')) for info in self.entries(CRL)]

    def add_tls_ca(self, name: str, certificates_pem: bytes, replace: bool = False) -> KeyInfo:
        """Trusted certificates (PEM, one or more) to verify a DoIP entity's certificate against."""
        return self.add(name, TLS_CA, _certificate_params(_load_certificates(certificates_pem)), {}, replace)

    def add_ipsec_sa(
        self,
        name: str,
        spi: int,
        encryption: str,
        encryption_key: bytes = b'',
        integrity: str = 'none',
        integrity_key: bytes = b'',
        replace: bool = False,
    ) -> KeyInfo:
        """An IPsec ESP security association for decrypting captures. `aes-gcm` takes the key followed by the
        4-byte salt (RFC 4106: 20, 28 or 36 bytes) and no separate integrity; `aes-cbc` (RFC 3602) and `null`
        (RFC 2410) need an integrity algorithm (RFC 4868 HMAC-SHA-256-128, or RFC 2404 HMAC-SHA-1-96)."""
        if not 0 < spi <= 0xFFFFFFFF:
            raise KeyStoreError('The SPI is a 32-bit number other than 0')
        if encryption not in ESP_ENCRYPTIONS:
            raise KeyStoreError(f'Unknown ESP encryption {encryption!r}; one of {", ".join(ESP_ENCRYPTIONS)}')
        if integrity not in ESP_INTEGRITIES:
            raise KeyStoreError(f'Unknown ESP integrity {integrity!r}; one of {", ".join(ESP_INTEGRITIES)}')
        if encryption == 'aes-gcm':
            if len(encryption_key) not in (20, 28, 36):
                raise KeyStoreError('An AES-GCM key is 16, 24 or 32 bytes followed by the 4-byte salt')
            if integrity != 'none':
                raise KeyStoreError('AES-GCM authenticates by itself; choose no integrity algorithm')
        elif encryption == 'aes-cbc' and len(encryption_key) not in (16, 24, 32):
            raise KeyStoreError(f'An AES-CBC key has 16, 24 or 32 bytes, not {len(encryption_key)}')
        elif encryption == 'null' and encryption_key:
            raise KeyStoreError('NULL encryption has no key')
        if encryption != 'aes-gcm':
            if integrity == 'none':
                raise KeyStoreError(f'ESP with {encryption} encryption needs an integrity algorithm')
            if not integrity_key:
                raise KeyStoreError('The integrity algorithm needs a key')
        params = {'spi': f'0x{spi:08X}', 'encryption': encryption, 'integrity': integrity}
        secrets = {'encryption_key': bytes(encryption_key), 'integrity_key': bytes(integrity_key)}
        return self.add(name, IPSEC, params, secrets, replace)

    def add_macsec_sa(self, name: str, sak: bytes, sci: Optional[int] = None, replace: bool = False) -> KeyInfo:
        """A MACsec secure association key (GCM-AES-128 with 16 bytes, GCM-AES-256 with 32) for the secure
        channel `sci` (64 bits), or any channel when None."""
        if len(sak) not in (16, 32):
            raise KeyStoreError(f'A MACsec SAK has 16 (GCM-AES-128) or 32 bytes (GCM-AES-256), not {len(sak)}')
        if sci is not None and not 0 <= sci <= 0xFFFFFFFFFFFFFFFF:
            raise KeyStoreError('The SCI is a 64-bit number')
        params = {'sci': f'{sci:016X}' if sci is not None else '', 'cipher': f'gcm-aes-{len(sak) * 8}'}
        return self.add(name, MACSEC, params, {'sak': bytes(sak)}, replace)

    def add_seed_key(self, name: str, script: bytes, file_name: str = '', replace: bool = False) -> KeyInfo:
        """A UDS seed/key algorithm: Python source defining `compute_key(level: int, seed: bytes) -> bytes`, kept
        encrypted (manufacturer algorithms and their constants are secrets). It is checked to compile and to define
        compute_key, not run."""
        try:
            tree = ast.parse(bytes(script).decode('utf-8'), file_name or name)
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            raise KeyStoreError(f'The seed/key script does not compile: {e}') from e
        defined = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if 'compute_key' not in defined:
            raise KeyStoreError('The seed/key script does not define compute_key(level, seed)')
        return self.add(name, SEED_KEY, {'file': file_name}, {'script': bytes(script)}, replace)

    def add_auth_key(self, name: str, key: bytes, mac: str = 'hmac-sha256', replace: bool = False) -> KeyInfo:
        """A key shared with ECUs for UDS Authentication by challenge-response (symmetric, sub-functions
        0x05..0x07): the proof of ownership is a MAC over the ECU's challenge (core/uds_auth.py)."""
        if mac not in AUTH_KEY_MACS:
            raise KeyStoreError(f'Unknown MAC {mac!r}; one of {", ".join(AUTH_KEY_MACS)}')
        if not key:
            raise KeyStoreError('The key is empty')
        if mac == 'aes-cmac' and len(key) not in (16, 24, 32):
            raise KeyStoreError(f'An AES-CMAC key has 16, 24 or 32 bytes, not {len(key)}')
        return self.add(name, AUTH_KEY, {'mac': mac}, {'key': bytes(key)}, replace)


_default_store: Optional[KeyStore] = None


def default_store() -> KeyStore:
    """The application's key store (the Security Manager's), created on first use at `default_path()`."""
    global _default_store
    if _default_store is None:
        _default_store = KeyStore()
    return _default_store


def set_default_store(store: KeyStore) -> None:
    global _default_store
    _default_store = store


def _load_certificates(pem: bytes) -> Sequence[x509.Certificate]:
    try:
        certificates = x509.load_pem_x509_certificates(bytes(pem))
    except ValueError as e:
        raise KeyStoreError(f'Cannot read the certificate (PEM expected): {e}') from e
    if not certificates:
        raise KeyStoreError('No certificate in the file')
    return certificates


def _certificate_params(certificates: Sequence[x509.Certificate]) -> Dict[str, str]:
    first = certificates[0]
    expires = min(certificate.not_valid_after_utc for certificate in certificates)
    pem = b''.join(certificate.public_bytes(serialization.Encoding.PEM) for certificate in certificates)
    return {
        'subject': first.subject.rfc4514_string(),
        'issuer': first.issuer.rfc4514_string(),
        'expires': expires.isoformat(timespec='seconds'),
        'count': str(len(certificates)),
        'certificate': pem.decode('ascii'),
    }


def expires_in_days(info: KeyInfo, now: Optional[datetime.datetime] = None) -> Optional[float]:
    """Days until a certificate entry expires (negative once expired); None for entries without certificates."""
    expires = info.params.get('expires')
    if not expires:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (datetime.datetime.fromisoformat(expires) - now).total_seconds() / 86400


def expired(info: KeyInfo, now: Optional[datetime.datetime] = None) -> bool:
    """Whether a certificate entry has expired (False for entries without certificates)."""
    expires = info.params.get('expires')
    if not expires:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return datetime.datetime.fromisoformat(expires) < now


def parse_hex(text: str, what: str = 'key') -> bytes:
    """Bytes from hex as people paste it: '00112233', '00 11 22 33', '00:11:22:33' or '0x0011…'."""
    cleaned = ''.join(text.split()).replace(':', '').replace('-', '')
    if cleaned.lower().startswith('0x'):
        cleaned = cleaned[2:]
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise KeyStoreError(f'The {what} is not hexadecimal') from None
