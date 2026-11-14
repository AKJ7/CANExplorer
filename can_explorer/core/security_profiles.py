"""Security profiles: plug-ins supplying manufacturer-specific security — SecOC keys or authenticators, freshness
values, seed/key algorithms and UDS Authentication certificates and signatures — possibly from a key server, so
secrets need not be kept on this computer.

A profile is any object with a `name` and some of these methods (missing ones are simply not offered):

    secoc_key(pdu: str, data_id: int | None) -> (key: bytes, algorithm: str) | None
    secoc_authenticator(pdu: str, data_id: int | None, data: bytes) -> bytes | None   # The full MAC, made elsewhere
    freshness() -> FreshnessManager | None                                             # See core/secoc_check.py
    compute_key(level: int, seed: bytes) -> bytes | None                               # UDS security access
    certificate() -> cryptography.x509.Certificate | None                              # UDS Authentication (0x29)
    sign(data: bytes, scheme: str) -> bytes                                            # Proof of ownership
    prove_challenge(challenge: bytes, algorithm_indicator: bytes) -> bytes | None      # 0x29 challenge-response
    verify_challenge(challenge: bytes, proof: bytes, indicator: bytes) -> bool | None  # The ECU's proof (0x07)

A method answering None has no answer for that request: the next way is tried (e.g. `sign` for a challenge).

Profiles are loaded from a Python file defining `profile` (an instance) or `create_profile()`, or from installed
packages registering a factory in the entry point group `can_explorer.security_profiles` — the way a
manufacturer would deliver one. Loading executes the code: only load profiles you trust.

`KeyServerProfile` is the built-in remote provider: it talks to a key server over HTTPS with a bearer token
(the REST API is below), so seed/key and proof-of-ownership signatures are computed by the server and the private
keys never reach this computer. SecOC keys are fetched once and held in memory only (never written to disk):
verifying every frame on the bus cannot wait for a server.

    GET  /secoc/keys      -> {"keys": [{"scope": "*" | "PDU, 0x12", "key": "<hex>", "algorithm": ""}]}
    POST /uds/key         {"level": 1, "seed": "<hex>"}           -> {"key": "<hex>"}
    GET  /auth/certificate                                         -> {"certificate": "<PEM>"}
    POST /auth/sign       {"data": "<hex>", "scheme": "ecdsa-sha256"} -> {"signature": "<hex>"}
    POST /auth/prove      {"challenge": "<hex>", "algorithm_indicator": "<hex>"} -> {"proof": "<hex>"}
    POST /auth/verify     {"challenge": "<hex>", "proof": "<hex>", "algorithm_indicator": "<hex>"} -> {"valid": true}

A server implements the endpoints it supports and answers 404 for the others.
"""

import importlib.metadata
import importlib.util
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography import x509

from can_explorer.core import audit

ENTRY_POINT_GROUP = 'can_explorer.security_profiles'
CAPABILITIES = (
    'secoc_key',
    'secoc_authenticator',
    'freshness',
    'compute_key',
    'certificate',
    'sign',
    'prove_challenge',
    'verify_challenge',
)
PREFIX = 'profile:'  # Key sources and certificate choices naming a profile


class SecurityProfileError(Exception):
    pass


def capabilities(profile) -> List[str]:
    return [name for name in CAPABILITIES if callable(getattr(profile, name, None))]


@dataclass
class LoadedProfile:
    profile: Any
    source: str  # A file path, 'entry point <name>', or 'key server <entry>'

    @property
    def name(self) -> str:
        return str(getattr(self.profile, 'name', '') or 'Unnamed profile')

    def as_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'source': self.source,
            'description': str(getattr(self.profile, 'description', '') or ''),
            'capabilities': capabilities(self.profile),
        }


def load_profile_file(path: str | Path) -> Any:
    """A profile from a Python file: its `profile` object, or the result of its `create_profile()`."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f'can_explorer_profile_{path.stem}', path)
    if spec is None or spec.loader is None:
        raise SecurityProfileError(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        if callable(getattr(module, 'create_profile', None)):
            return module.create_profile()
    except Exception as e:
        raise SecurityProfileError(f'Error in security profile {path.name}: {e}') from e
    profile = getattr(module, 'profile', None)
    if profile is None:
        raise SecurityProfileError(f'{path.name} defines neither profile nor create_profile()')
    return profile


def entry_point_profiles() -> List[LoadedProfile]:
    """The profiles installed packages register (entry point group can_explorer.security_profiles)."""
    loaded = []
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            factory = entry_point.load()
            loaded.append(LoadedProfile(factory() if callable(factory) else factory, f'entry point {entry_point.name}'))
        except Exception as e:  # noqa: BLE001 - a broken plug-in must not stop the others
            audit.record('security profile failed to load', entry_point=entry_point.name, error=str(e))
    return loaded


class ProfileRegistry:
    """The loaded profiles, consulted in order (the first one able to answer does)."""

    def __init__(self) -> None:
        self.profiles: List[LoadedProfile] = []
        self._audited: set = set()  # (profile, PDU) whose SecOC key use was recorded: once, not for every frame

    def _record_secoc(self, profile: str, pdu: str) -> None:
        if (profile, pdu) not in self._audited:
            self._audited.add((profile, pdu))
            audit.record('secoc key used', profile=profile, pdu=pdu)

    def add(self, loaded: LoadedProfile) -> None:
        self.profiles = [existing for existing in self.profiles if existing.name != loaded.name] + [loaded]
        audit.record('security profile loaded', profile=loaded.name, source=loaded.source)

    def remove(self, name: str) -> None:
        self.profiles = [loaded for loaded in self.profiles if loaded.name != name]

    def get(self, name: str) -> Any:
        for loaded in self.profiles:
            if loaded.name == name:
                return loaded.profile
        raise SecurityProfileError(f'No security profile named {name!r}')

    def with_capability(self, capability: str) -> List[Any]:
        return [loaded.profile for loaded in self.profiles if capability in capabilities(loaded.profile)]

    # What the other modules ask for

    def secoc_key(self, config) -> Tuple[Optional[Any], str]:
        """A SecOcKey (core/secoc_check.py) for a secured PDU from the profiles, or None and why."""
        from can_explorer.core.secoc_check import SecOcKey

        for profile in self.profiles:
            answer_key = getattr(profile.profile, 'secoc_key', None)
            if callable(answer_key):
                found = answer_key(config.pdu, config.data_id)
                if found:
                    key, algorithm = found
                    self._record_secoc(profile.name, config.pdu)
                    return SecOcKey(bytes(key), algorithm or '', f'{PREFIX}{profile.name}'), ''
            authenticator = getattr(profile.profile, 'secoc_authenticator', None)
            if callable(authenticator):
                bound = profile.profile

                def compute(data: bytes, pdu=config.pdu, data_id=config.data_id, profile_object=bound) -> bytes:
                    mac = profile_object.secoc_authenticator(pdu, data_id, data)
                    if mac is None:
                        raise SecurityProfileError(
                            f'{getattr(profile_object, "name", "The profile")} has no key for {pdu}'
                        )
                    return bytes(mac)

                self._record_secoc(profile.name, config.pdu)
                return SecOcKey(b'', 'profile', f'{PREFIX}{profile.name}', authenticator=compute), ''
        return None, 'no security profile has a key for it'

    def key_for(self, config) -> Tuple[Optional[Any], str]:
        """The SecOcKeys protocol (core/secoc_check.py): keys and authenticators from the profiles."""
        return self.secoc_key(config)

    def freshness(self):
        for profile in self.with_capability('freshness'):
            manager = profile.freshness()
            if manager is not None:
                return manager
        return None


_registry = ProfileRegistry()


def registry() -> ProfileRegistry:
    """The application's profiles."""
    return _registry


def set_registry(profiles: ProfileRegistry) -> None:
    global _registry
    _registry = profiles


# --- The key server ---


class KeyServerProfile:
    """A profile backed by a key server (see the module documentation for its REST API)."""

    description = 'Keys, seed/key and signatures from a key server'

    def __init__(self, name: str, url: str, token: str = '', context: Optional[ssl.SSLContext] = None, timeout=5.0):
        if not url.startswith(('https://', 'http://')):
            raise SecurityProfileError('A key server URL starts with https:// (or http:// for local tests)')
        self.name = name
        self.url = url.rstrip('/')
        self._token = token
        self._context = context
        self.timeout = timeout
        self._secoc_keys: Optional[List[Dict[str, str]]] = None  # Fetched once, in memory only

    def __repr__(self) -> str:  # Never show the token
        return f'KeyServerProfile({self.name!r}, {self.url!r})'

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """The JSON answer of an endpoint, or None when the server does not offer it (404)."""
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode('utf-8') if body is not None else None,
            method=method,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        )
        if self._token:
            request.add_header('Authorization', f'Bearer {self._token}')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._context) as response:
                return json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise SecurityProfileError(f'The key server {self.url} refused {path}: HTTP {e.code} {e.reason}') from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SecurityProfileError(f'The key server {self.url} cannot be reached: {e}') from e

    def secoc_key(self, pdu: str, data_id: Optional[int]) -> Optional[Tuple[bytes, str]]:
        if self._secoc_keys is None:
            answer = self._call('GET', '/secoc/keys')
            self._secoc_keys = list((answer or {}).get('keys', []))
            audit.record('secoc keys fetched from the key server', server=self.url, keys=len(self._secoc_keys))
        best: Tuple[int, Optional[Dict[str, str]]] = (0, None)
        for entry in self._secoc_keys:
            rank = _scope_rank(str(entry.get('scope', '*')), pdu, data_id)
            if rank > best[0]:
                best = (rank, entry)
        if best[1] is None:
            return None
        return bytes.fromhex(best[1]['key']), str(best[1].get('algorithm', ''))

    def compute_key(self, level: int, seed: bytes) -> Optional[bytes]:
        answer = self._call('POST', '/uds/key', {'level': level, 'seed': bytes(seed).hex()})
        if answer is None:
            return None
        audit.record('seed/key computed by the key server', server=self.url, level=level)
        return bytes.fromhex(answer['key'])

    def certificate(self) -> Optional[x509.Certificate]:
        answer = self._call('GET', '/auth/certificate')
        if answer is None:
            return None
        return x509.load_pem_x509_certificate(answer['certificate'].encode('ascii'))

    def sign(self, data: bytes, scheme: str) -> bytes:
        answer = self._call('POST', '/auth/sign', {'data': bytes(data).hex(), 'scheme': scheme})
        if answer is None:
            raise SecurityProfileError(f'The key server {self.url} does not sign')
        audit.record('proof of ownership signed by the key server', server=self.url, scheme=scheme)
        return bytes.fromhex(answer['signature'])

    def prove_challenge(self, challenge: bytes, algorithm_indicator: bytes) -> Optional[bytes]:
        body = {'challenge': bytes(challenge).hex(), 'algorithm_indicator': bytes(algorithm_indicator).hex()}
        answer = self._call('POST', '/auth/prove', body)
        if answer is None:
            return None  # Not offered: the proof is then a signature (/auth/sign)
        audit.record('challenge proven by the key server', server=self.url)
        return bytes.fromhex(answer['proof'])

    def verify_challenge(self, challenge: bytes, proof: bytes, algorithm_indicator: bytes) -> Optional[bool]:
        body = {
            'challenge': bytes(challenge).hex(),
            'proof': bytes(proof).hex(),
            'algorithm_indicator': bytes(algorithm_indicator).hex(),
        }
        answer = self._call('POST', '/auth/verify', body)
        if answer is None:
            return None  # Not offered: checked against trusted certificates, if any
        return bool(answer.get('valid'))

    def forget(self) -> None:
        """Drop the SecOC keys held in memory."""
        self._secoc_keys = None


def _scope_rank(scope: str, pdu: str, data_id: Optional[int]) -> int:
    best = 0
    for token in (part.strip() for part in scope.split(',')):
        if token == pdu:
            return 3
        if token == '*':
            best = max(best, 1)
            continue
        try:
            if data_id is not None and int(token, 0) == data_id:
                best = max(best, 2)
        except ValueError:
            pass
    return best
