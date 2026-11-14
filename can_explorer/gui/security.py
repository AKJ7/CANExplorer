"""The security manager: the key store, and the SecOC, TLS and IPsec/MACsec users of its keys.

One per application (`connectionManager.security`). It hands out what the other tools need — a SecOC verifier per
connection, a packet decryptor for the Ethernet capture, a TLS context for DoIP — and tells them when the keys
change (`keys_changed`), e.g. on unlocking. Secrets never leave Python: QML sees entry metadata only.
"""

import dataclasses
import json
import ssl
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QSettings, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core import audit, keychain, keystore, security_profiles, test_pki
from can_explorer.core.audit import AuditLog
from can_explorer.core.decoding import FrameDecoder
from can_explorer.core.keystore import KeyStore, KeyStoreError, parse_hex
from can_explorer.core.packet_decryption import PacketDecryptor
from can_explorer.core.secoc_check import (
    ALGORITHMS,
    ChainedSecOcKeys,
    CounterFreshness,
    FreshnessManager,
    KeyStoreSecOcKeys,
    SecOcError,
    SecOcVerifier,
    TripResetConfig,
    TripResetFreshness,
    load_freshness_script,
)
from can_explorer.core.tls import client_context
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

PURPOSE_NAMES = {
    keystore.SECOC: 'SecOC key',
    keystore.TLS_CLIENT: 'TLS client certificate',
    keystore.TLS_CA: 'Trusted TLS certificates',
    keystore.CRL: 'Revocation list',
    keystore.IPSEC: 'IPsec ESP association',
    keystore.MACSEC: 'MACsec association',
    keystore.SEED_KEY: 'Seed/key algorithm',
    keystore.AUTH_KEY: 'UDS authentication key',
    keystore.KEY_SERVER: 'Key server',
}


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


def _details(info: keystore.KeyInfo) -> str:
    params = info.params
    if info.purpose == keystore.SECOC:
        algorithm = params.get('algorithm') or 'algorithm from the ARXML'
        return f'for {params.get("scope", "*")} · {algorithm}'
    if info.purpose in (keystore.TLS_CLIENT, keystore.TLS_CA):
        count = int(params.get('count', '1'))
        extra = f' (+{count - 1} more)' if count > 1 else ''
        state = 'expired' if keystore.expired(info) else 'expires'
        return f'{params.get("subject", "")}{extra} · {state} {params.get("expires", "")[:10]}'
    if info.purpose == keystore.IPSEC:
        integrity = '' if params.get('integrity') == 'none' else f' + {params.get("integrity")}'
        return f'SPI {params.get("spi")} · {params.get("encryption")}{integrity}'
    if info.purpose == keystore.MACSEC:
        return f'SCI {params.get("sci") or "any"} · {params.get("cipher")}'
    if info.purpose == keystore.CRL:
        next_update = params.get('next_update', '')[:10]
        return f'{params.get("issuer", "")} · {params.get("revoked", "0")} revoked' + (
            f' · next update {next_update}' if next_update else ''
        )
    if info.purpose == keystore.KEY_SERVER:
        trusted = params.get('trusted')
        return params.get('url', '') + (f' · checked against {trusted}' if trusted else '')
    if info.purpose == keystore.AUTH_KEY:
        return f'challenge-response · {params.get("mac", "")}'
    if info.purpose == keystore.SEED_KEY:
        return f'compute_key from {params.get("file") or "a script"}'
    return ''


class SecurityController(QObject):
    changed = pyqtSignal()  # The store's state or entries
    keys_changed = pyqtSignal()  # What the keys decrypt or verify may have changed: re-check, re-decode
    notice = pyqtSignal(str)  # Something to tell the user, e.g. a certificate about to expire

    def __init__(
        self,
        store: Optional[KeyStore] = None,
        settings: Optional[QSettings] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._settings = settings
        self.store = store if store is not None else KeyStore()
        keystore.set_default_store(self.store)  # Seed/key algorithms are looked up here (core/uds.load_key_source)
        self._announced: set = set()  # Expiry notices given, (entry, expired)
        self.profiles = security_profiles.ProfileRegistry()
        security_profiles.set_registry(self.profiles)  # Key sources 'profile:<name>' are looked up here
        # SecOC keys: the security profiles first (e.g. a key server), then the key store
        self.secoc_keys = ChainedSecOcKeys(self.profiles, KeyStoreSecOcKeys(self.store))
        self._freshness_script = ''
        self._trip_reset: Optional[TripResetConfig] = None  # The AUTOSAR trip/reset freshness counters, when chosen
        self._load_freshness()
        self._decoders: 'weakref.WeakSet[FrameDecoder]' = weakref.WeakSet()
        self._error = ''
        try:
            if self.store.unlock_from_environment():
                logger.info('Unlocked the key store with CANEXPLORER_KEYSTORE_PASSWORD')
        except KeyStoreError as e:
            logger.warning(f'Key store: {e}')
        self.audit_log = AuditLog(audit.log_path_for(self.store.path), self._audit_key)
        audit.set_log(self.audit_log)
        self._unlock_from_keychain()
        self.keys_changed.connect(self.secoc_keys.invalidate)
        self.keys_changed.connect(self._refresh_key_servers)
        for loaded in security_profiles.entry_point_profiles():
            self.profiles.add(loaded)
        for path in self._saved_profile_files():
            self._load_profile_file(path)
        self._refresh_key_servers()

    def _keys_changed(self) -> None:
        self.changed.emit()
        self.keys_changed.emit()
        self.check_expiry()

    def check_expiry(self) -> List[str]:
        """Announce (`notice`) certificates that expired or expire within EXPIRY_WARNING_DAYS, once each."""
        try:
            entries = self.store.entries()
        except KeyStoreError:
            return []
        messages = []
        for info in entries:
            days = keystore.expires_in_days(info)
            if days is None or days > keystore.EXPIRY_WARNING_DAYS:
                continue
            state = (info.name, days < 0)
            if state in self._announced:
                continue
            self._announced.add(state)
            kind = PURPOSE_NAMES.get(info.purpose, info.purpose)
            if days < 0:
                text = f'{kind} {info.name!r} has expired ({info.params.get("expires", "")[:10]})'
            else:
                text = (
                    f'{kind} {info.name!r} expires in {max(0, int(days))} days ({info.params.get("expires", "")[:10]})'
                )
            messages.append(text)
            self.notice.emit(text)
        return messages

    def _run(self, action, audit_action: str = '', **details) -> str:
        """Run a store action, and record it in the audit log; returns its error, or ''."""
        try:
            action()
        except (KeyStoreError, OSError, ValueError) as e:
            if audit_action:
                audit.record(f'{audit_action} failed', error=str(e), **details)
            return str(e)
        if audit_action:
            audit.record(audit_action, **details)
        self._keys_changed()
        return ''

    # State

    @pyqtProperty(str, constant=True)
    def path(self) -> str:
        return str(self.store.path)

    @pyqtProperty(bool, notify=changed)
    def exists(self) -> bool:
        return self.store.exists

    @pyqtProperty(bool, notify=changed)
    def unlocked(self) -> bool:
        return self.store.unlocked

    @pyqtProperty('QVariantList', notify=changed)
    def entries(self) -> List[Dict[str, Any]]:
        """Entry metadata for the list (no secrets): name, purpose, kind (readable), details, expired."""
        try:
            entries = self.store.entries()
        except KeyStoreError as e:
            return [{'name': '', 'purpose': '', 'kind': '', 'details': str(e), 'expired': False}]
        return [
            {
                'name': info.name,
                'purpose': info.purpose,
                'kind': PURPOSE_NAMES.get(info.purpose, info.purpose),
                'details': _details(info),
                'expired': keystore.expired(info),
            }
            for info in sorted(entries, key=lambda info: (keystore.PURPOSES.index(info.purpose), info.name.lower()))
        ]

    @pyqtProperty('QStringList', constant=True)
    def secoc_algorithms(self) -> List[str]:
        return list(ALGORITHMS)

    @pyqtProperty('QStringList', constant=True)
    def auth_key_macs(self) -> List[str]:
        return list(keystore.AUTH_KEY_MACS)

    @pyqtProperty('QStringList', constant=True)
    def esp_encryptions(self) -> List[str]:
        return list(keystore.ESP_ENCRYPTIONS)

    @pyqtProperty('QStringList', constant=True)
    def esp_integrities(self) -> List[str]:
        return list(keystore.ESP_INTEGRITIES)

    # Password

    @pyqtSlot(str, str, result=str)
    def create(self, password: str, repeated: str) -> str:
        if password != repeated:
            return 'The passwords differ'
        return self._run(lambda: self.store.create(password), 'create key store')

    @pyqtSlot(str, result=str)
    def unlock(self, password: str) -> str:
        return self._run(lambda: self.store.unlock(password), 'unlock')

    @pyqtSlot(str, bool, result=str)
    def unlock_and_remember(self, password: str, remember: bool) -> str:
        """Unlock, and keep the password in the OS keychain (or forget a kept one) so later starts unlock."""
        error = self.unlock(password)
        if error:
            return error
        try:
            if remember:
                keychain.remember(self.store.path, password)
                audit.record('password kept in the keychain')
            else:
                keychain.forget(self.store.path)
        except keychain.KeychainError as e:
            return str(e)
        self.changed.emit()
        return ''

    def _unlock_from_keychain(self) -> None:
        if self.store.unlocked or not self.store.exists:
            return
        password = keychain.recall(self.store.path)
        if password is None:
            return
        try:
            self.store.unlock(password)
            logger.info('Unlocked the key store with the password kept in the keychain')
            audit.record('unlock with the keychain')
        except KeyStoreError as e:  # Changed elsewhere: forget the stale password
            logger.warning(f'The password kept in the keychain does not unlock the key store: {e}')
            keychain.forget(self.store.path)

    def _audit_key(self) -> Optional[bytes]:
        try:
            return self.store.audit_key()
        except KeyStoreError:
            return None

    @pyqtProperty('QVariantList', notify=changed)
    def audit_records(self) -> List[Dict[str, Any]]:
        """The latest audit records, newest first: time, action and details as text."""
        rows = []
        for record in reversed(self.audit_log.records(limit=200)):
            details = ', '.join(f'{key}={value}' for key, value in record.get('details', {}).items())
            rows.append(
                {
                    'time': record.get('time', ''),
                    'action': record.get('action', ''),
                    'details': details,
                    'signed': bool(record.get('signed')),
                }
            )
        return rows

    @pyqtSlot()
    def refresh(self):
        """Re-read what changes outside the controller (the audit log grows as keys are used)."""
        self.changed.emit()

    @pyqtSlot(result=str)
    def verify_audit(self) -> str:
        return self.audit_log.verify(self._audit_key()).describe()

    @pyqtSlot(QUrl, result=str)
    def export_audit(self, url: QUrl) -> str:
        path = _local_path(url)
        try:
            path.write_bytes(self.audit_log.path.read_bytes() if self.audit_log.path.is_file() else b'')
        except OSError as e:
            return f'Could not write {path.name}: {e}'
        return f'Exported the audit log to {path.name}'

    @pyqtProperty(bool, constant=True)
    def keychain_available(self) -> bool:
        return keychain.available()

    @pyqtProperty(bool, notify=changed)
    def remembered(self) -> bool:
        """Whether the OS keychain keeps the password of this key store."""
        return keychain.recall(self.store.path) is not None

    @pyqtSlot()
    def forget_password(self):
        keychain.forget(self.store.path)
        audit.record('password removed from the keychain')
        self.changed.emit()

    @pyqtSlot()
    def lock(self):
        audit.record('locked')
        self.store.lock()
        self._keys_changed()

    @pyqtSlot(str, str, str, result=str)
    def change_password(self, old: str, new: str, repeated: str) -> str:
        if new != repeated:
            return 'The new passwords differ'
        remembered = keychain.recall(self.store.path) is not None
        error = self._run(lambda: self.store.change_password(old, new), 'change password')
        if not error and remembered:
            try:
                keychain.remember(self.store.path, new)  # Keep the kept password current
            except keychain.KeychainError as e:
                return str(e)
        return error

    # Moving entries between computers

    @pyqtSlot(QUrl, 'QStringList', str, str, result=str)
    def export_entries(self, url: QUrl, names: List[str], passphrase: str, repeated: str) -> str:
        """Export entries to a bundle file encrypted with its own passphrase. Returns an error, or ''."""
        if passphrase != repeated:
            return 'The passphrases differ'
        try:
            self.store.export_bundle(_local_path(url), list(names), passphrase)
        except (KeyStoreError, OSError) as e:
            return str(e)
        audit.record('entries exported', entries=list(names), file=str(_local_path(url)))
        return ''

    @pyqtSlot(QUrl, str, bool, result='QVariantMap')
    def import_entries(self, url: QUrl, passphrase: str, replace: bool) -> Dict[str, Any]:
        """Import the entries of a bundle: {error, imported, skipped} (names)."""
        try:
            imported, skipped = self.store.import_bundle(_local_path(url), passphrase, replace)
        except (KeyStoreError, OSError) as e:
            return {'error': str(e), 'imported': [], 'skipped': []}
        audit.record('entries imported', entries=imported, kept=skipped, file=str(_local_path(url)))
        self._keys_changed()
        return {'error': '', 'imported': imported, 'skipped': skipped}

    # Test certificates

    @pyqtProperty('QStringList', constant=True)
    def test_key_types(self) -> List[str]:
        return list(test_pki.KEY_TYPES)

    @pyqtSlot('QVariantMap', result=str)
    def create_test_pki(self, options: Dict[str, Any]) -> str:
        """Options: prefix, key_type, days, addresses (comma separated), directory (URL, optional). Returns an error."""
        try:
            directory = options.get('directory')
            directory_path = _local_path(directory) if directory and str(QUrl(directory).toString()) else None
            addresses = [part.strip() for part in str(options.get('addresses') or '').split(',') if part.strip()]
            test_pki.create_test_pki(
                self.store,
                str(options.get('prefix') or 'Test'),
                str(options.get('key_type') or 'ec-p256'),
                int(options.get('days') or 365),
                addresses,
                directory_path,
            )
        except (test_pki.TestPkiError, KeyStoreError, OSError, ValueError) as e:
            return str(e)
        audit.record('test certificates created', prefix=str(options.get('prefix') or 'Test'))
        self._keys_changed()
        return ''

    # Entries

    @pyqtSlot(str, result=str)
    def remove(self, name: str) -> str:
        return self._run(lambda: self.store.remove(name), 'remove entry', entry=name)

    @pyqtSlot(str, str, str, str, result=str)
    def add_secoc_key(self, name: str, key_hex: str, scope: str, algorithm: str) -> str:
        return self._run(
            lambda: self.store.add_secoc_key(name, parse_hex(key_hex), scope, algorithm), 'add entry', entry=name
        )

    @pyqtSlot(str, QUrl, QUrl, str, result=str)
    def add_tls_client(self, name: str, certificate: QUrl, private_key: QUrl, key_password: str) -> str:
        def add():
            self.store.add_tls_client(
                name,
                _local_path(certificate).read_bytes(),
                _local_path(private_key).read_bytes(),
                key_password.encode('utf-8') if key_password else None,
            )

        return self._run(add, 'add entry', entry=name)

    @pyqtSlot(str, QUrl, result=str)
    def add_crl(self, name: str, crl: QUrl) -> str:
        return self._run(lambda: self.store.add_crl(name, _local_path(crl).read_bytes()), 'add entry', entry=name)

    @pyqtSlot(str, QUrl, result=str)
    def add_tls_ca(self, name: str, certificates: QUrl) -> str:
        return self._run(
            lambda: self.store.add_tls_ca(name, _local_path(certificates).read_bytes()), 'add entry', entry=name
        )

    @pyqtSlot(str, str, str, str, str, str, result=str)
    def add_ipsec_sa(
        self, name: str, spi: str, encryption: str, encryption_key: str, integrity: str, integrity_key: str
    ) -> str:
        def add():
            try:
                spi_value = int(spi.strip(), 16)
            except ValueError:
                raise KeyStoreError('The SPI is hexadecimal, e.g. 0x00001000') from None
            self.store.add_ipsec_sa(
                name,
                spi_value,
                encryption,
                parse_hex(encryption_key, 'encryption key'),
                integrity,
                parse_hex(integrity_key, 'integrity key'),
            )

        return self._run(add, 'add entry', entry=name)

    @pyqtSlot(str, str, str, result=str)
    def add_macsec_sa(self, name: str, sak: str, sci: str) -> str:
        def add():
            sci_value = None
            if sci.strip():
                try:
                    sci_value = int(sci.strip().replace(':', '').replace('-', ''), 16)
                except ValueError:
                    raise KeyStoreError(
                        'The SCI is 16 hex digits: the MAC address and port, e.g. 0011223344550001'
                    ) from None
            self.store.add_macsec_sa(name, parse_hex(sak, 'SAK'), sci_value)

        return self._run(add, 'add entry', entry=name)

    @pyqtSlot(str, QUrl, result=str)
    def add_seed_key(self, name: str, script: QUrl) -> str:
        path = _local_path(script)
        return self._run(lambda: self.store.add_seed_key(name, path.read_bytes(), path.name), 'add entry', entry=name)

    @pyqtSlot(str, str, str, result=str)
    def add_auth_key(self, name: str, key_hex: str, mac: str) -> str:
        return self._run(lambda: self.store.add_auth_key(name, parse_hex(key_hex), mac), 'add entry', entry=name)

    # SecOC

    FRESHNESS_KEY = 'security/freshness'

    def _load_freshness(self) -> None:
        if self._settings is None:
            return
        try:
            data = json.loads(str(self._settings.value(self.FRESHNESS_KEY, '{}')))
            if data.get('mode') == 'script' and data.get('script'):
                self._freshness_script = str(data['script'])
            elif data.get('mode') == 'trip-reset':
                self._trip_reset = TripResetConfig(**data.get('trip_reset', {}))
        except (ValueError, TypeError, SecOcError) as e:
            logger.warning(f'Ignoring the saved SecOC freshness settings: {e}')

    def _save_freshness(self) -> None:
        if self._settings is None:
            return
        if self._trip_reset is not None:
            data = {'mode': 'trip-reset', 'trip_reset': dataclasses.asdict(self._trip_reset)}
        elif self._freshness_script:
            data = {'mode': 'script', 'script': self._freshness_script}
        else:
            data = {'mode': 'counter'}
        self._settings.setValue(self.FRESHNESS_KEY, json.dumps(data))

    def _apply_freshness(self) -> None:
        audit.record('secoc freshness changed', mode=self.freshness_mode)
        self._save_freshness()
        for decoder in list(self._decoders):
            if decoder.secoc is not None:
                decoder.secoc.freshness = self._freshness()
                decoder.secoc.reset()
        self._keys_changed()

    @pyqtProperty(str, notify=changed)
    def freshness_mode(self) -> str:
        """'counter', 'script' or 'trip-reset'."""
        return 'trip-reset' if self._trip_reset is not None else 'script' if self._freshness_script else 'counter'

    @pyqtProperty('QVariantMap', notify=changed)
    def trip_reset(self) -> Dict[str, Any]:
        return dataclasses.asdict(self._trip_reset if self._trip_reset is not None else TripResetConfig())

    @pyqtSlot('QVariantMap', result=str)
    def set_trip_reset(self, options: Dict[str, Any]) -> str:
        """Use the AUTOSAR trip/reset counters. Options: trip_bits, reset_bits, message_bits, flag_bits, sync_pdu,
        clear_acceptance_window. Returns an error, or ''."""
        try:
            numbers = ('trip_bits', 'reset_bits', 'message_bits', 'flag_bits', 'clear_acceptance_window')
            values: Dict[str, Any] = {key: int(options[key]) for key in numbers if str(options.get(key, '')) != ''}
            values['sync_pdu'] = str(options.get('sync_pdu') or '').strip()
            config = TripResetConfig(**values)
        except (ValueError, TypeError, SecOcError) as e:
            return str(e)
        self._trip_reset, self._freshness_script = config, ''
        self._apply_freshness()
        return ''

    @pyqtProperty(str, notify=changed)
    def freshness_script(self) -> str:
        return self._freshness_script

    @pyqtSlot(QUrl, result=str)
    def set_freshness_script(self, url: QUrl) -> str:
        """Use a freshness value manager from a Python file (an empty URL: the built-in counter)."""
        path = str(_local_path(url)) if not url.isEmpty() else ''
        if path:
            try:
                load_freshness_script(path)  # Fail here, not on every connection
            except SecOcError as e:
                return str(e)
        self._freshness_script, self._trip_reset = path, None
        self._apply_freshness()
        return ''

    def _freshness(self) -> FreshnessManager:
        from_profile = self.profiles.freshness()
        if from_profile is not None:
            return from_profile
        if self._trip_reset is not None:
            return TripResetFreshness(self._trip_reset)
        if self._freshness_script:
            try:
                return load_freshness_script(self._freshness_script)
            except SecOcError as e:
                logger.warning(f'SecOC: {e}; using the counter freshness')
        return CounterFreshness()

    def attach(self, decoder: FrameDecoder) -> None:
        """Check the secured PDUs a connection's frames carry (each connection keeps its own freshness state)."""
        decoder.secoc = SecOcVerifier(self.secoc_keys, self._freshness())
        self._decoders.add(decoder)

    # Security profiles

    PROFILES_KEY = 'security/profile_files'

    def _saved_profile_files(self) -> List[str]:
        if self._settings is None:
            return []
        try:
            return [str(path) for path in json.loads(str(self._settings.value(self.PROFILES_KEY, '[]')))]
        except ValueError:
            return []

    def _save_profile_files(self) -> None:
        if self._settings is not None:
            files = [loaded.source for loaded in self.profiles.profiles if Path(loaded.source).suffix == '.py']
            self._settings.setValue(self.PROFILES_KEY, json.dumps(files))

    def _load_profile_file(self, path: str) -> str:
        try:
            profile = security_profiles.load_profile_file(path)
        except security_profiles.SecurityProfileError as e:
            logger.warning(f'Security profile: {e}')
            return str(e)
        self.profiles.add(security_profiles.LoadedProfile(profile, str(path)))
        return ''

    def _refresh_key_servers(self) -> None:
        """One profile per key server entry of the key store, while it is unlocked (the token is a secret)."""
        for loaded in list(self.profiles.profiles):
            if loaded.source.startswith('key server '):
                self.profiles.remove(loaded.name)
        if not self.store.unlocked:
            return
        for info in self.store.entries(keystore.KEY_SERVER):
            try:
                token = self.store.secrets(info.name).get('token', b'').decode('utf-8')
                trusted = info.params.get('trusted', '')
                context = client_context(self.store, trusted=trusted, keylog='') if trusted else None
                profile = security_profiles.KeyServerProfile(info.name, info.params['url'], token, context)
            except (KeyStoreError, security_profiles.SecurityProfileError, ssl.SSLError) as e:
                logger.warning(f'Key server {info.name}: {e}')
                continue
            self.profiles.profiles.append(security_profiles.LoadedProfile(profile, f'key server {info.name}'))

    @pyqtProperty('QVariantList', notify=changed)
    def security_profiles(self) -> List[Dict[str, Any]]:
        return [loaded.as_dict() for loaded in self.profiles.profiles]

    @pyqtSlot(QUrl, result=str)
    def load_profile(self, url: QUrl) -> str:
        """Load a security profile from a Python file (executed: trusted files only). Returns an error, or ''."""
        error = self._load_profile_file(str(_local_path(url)))
        if not error:
            self._save_profile_files()
            self._keys_changed()
        return error

    @pyqtSlot(str)
    def remove_profile(self, name: str):
        self.profiles.remove(name)
        audit.record('security profile removed', profile=name)
        self._save_profile_files()
        self._keys_changed()

    @pyqtSlot(str, str, str, str, result=str)
    def add_key_server(self, name: str, url: str, token: str, trusted: str) -> str:
        return self._run(lambda: self.store.add_key_server(name, url, token, trusted), 'add entry', entry=name)

    # Ethernet and DoIP

    def packet_decryptor(self) -> PacketDecryptor:
        return PacketDecryptor.from_keystore(self.store)

    def tls_context(self, trusted: str, client: str):
        """A DoIP TLS context, or True (TLS without verifying the entity) when no entry is chosen."""
        if not trusted and not client:
            return True
        return client_context(self.store, trusted=trusted, client=client)
