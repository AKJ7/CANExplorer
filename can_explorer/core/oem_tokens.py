"""OEM right-to-repair token providers: the legitimate path for obtaining diagnostic access through a vehicle
security gateway.

A modern vehicle places a security gateway between the diagnostic connector and the internal buses. Independent
repair shops reach the ECUs behind it the same way the manufacturer's own tools do: not by circumventing the
gateway, but by obtaining a short-lived access token from the OEM's right-to-repair portal and presenting it. The
shop authenticates to the portal with its own credentials (often an account mandated by right-to-repair
legislation), identifies the vehicle, and the portal issues a time-limited unlock token.

This module models that portal side, which is well defined and vendor-neutral:

- `OemTokenProvider` — the protocol a portal integration satisfies.
- `OAuthTokenProvider` / `HttpTokenProvider` — ready providers for OAuth 2.0 portals and simple REST portals.
- `ProviderRegistry` — the loaded providers, extensible through the `can_explorer.oem_token_providers` entry point
  group so a third party can ship a provider for a specific OEM portal without changing this package.
- `OemToken` — an issued token, with its lifetime and scope.

What this module deliberately does **not** do is fabricate the vehicle-side frame that presents a token to a
particular OEM's gateway. The exact service, sub-function and byte layout of a gateway unlock are OEM-specific and
frequently paywalled; guessing them in a tool people trust would be dishonest. A provider that knows a specific
portal supplies its own `gateway_unlock` recipe (see `GatewayUnlock`); without one, the obtained token is handed to
the caller to present through whatever documented mechanism the vehicle uses.

All network access goes through an injectable transport so the providers are unit-testable without a portal.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable

from can_explorer.core import audit

ENTRY_POINT_GROUP = 'can_explorer.oem_token_providers'

# How a token is carried once issued
BEARER = 'bearer'  # An HTTP/DoIP bearer token, presented to the portal-backed gateway service
ONE_TIME = 'one-time'  # A single-use unlock code (e.g. an SGW unlock), spent on one gateway session
CHALLENGE = 'challenge'  # A value used in a challenge-response the gateway runs (UDS 0x27/0x29)
TOKEN_KINDS = (BEARER, ONE_TIME, CHALLENGE)


class OemTokenError(Exception):
    """A portal could not be reached, refused the request, or answered without a token."""


@dataclass
class TokenRequest:
    """What the portal needs to issue a token for one vehicle. `vin` and `make` identify the vehicle; `extra`
    carries any portal-specific fields (e.g. a work-order id). No secrets live here — credentials are passed
    separately so they are never logged with the request."""

    vin: str = ''
    make: str = ''
    extra: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.vin = self.vin.strip().upper()
        self.make = self.make.strip()
        if self.vin and not self.vin.isalnum():
            raise OemTokenError('A VIN is alphanumeric')


@dataclass
class OemToken:
    """A token issued by an OEM portal. `value` is the secret; `kind` is one of `TOKEN_KINDS`; `expires_at` is an
    epoch time (0 means the portal gave no lifetime); `scope` and `metadata` are whatever the portal returned."""

    value: str
    kind: str = BEARER
    expires_at: float = 0.0
    scope: str = ''
    oem: str = ''
    metadata: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in TOKEN_KINDS:
            raise OemTokenError(f'Unknown token kind {self.kind!r}; one of {", ".join(TOKEN_KINDS)}')

    def __repr__(self) -> str:  # Never show the token value
        return f'OemToken(kind={self.kind!r}, oem={self.oem!r}, scope={self.scope!r}, expires_at={self.expires_at})'

    def expired(self, now: Optional[float] = None, margin: float = 0.0) -> bool:
        """Whether the token is past its lifetime (with an optional safety margin). A token with no lifetime
        (`expires_at == 0`) never reports expired here; a one-time token's real end is the gateway spending it."""
        if not self.expires_at:
            return False
        return (time.time() if now is None else now) >= self.expires_at - margin


@dataclass
class GatewayUnlock:
    """How to present a token to a specific gateway, supplied by a provider that knows its portal's vehicle side.

    `service` is the UDS service id the token is sent with (e.g. 0x27, 0x29, or an OEM service); `build` turns the
    token value into the request payload bytes. This is intentionally provider-supplied: the layout is OEM-specific
    and not guessed here (see the module docstring and CLAUDE.md 'Honesty about unknowns')."""

    service: int
    build: Callable[[OemToken], bytes]
    description: str = ''


# A transport: given (url, data, headers, timeout) it returns the decoded JSON body. `data` is None for GET.
Transport = Callable[[str, Optional[bytes], Mapping[str, str], float], Dict[str, Any]]


def _urllib_transport(url: str, data: Optional[bytes], headers: Mapping[str, str], timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url, data, dict(headers), method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme checked by caller
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b'{}')
        except ValueError:
            body = {}
        detail = body.get('error_description') or body.get('error') or e.reason
        raise OemTokenError(f'{url} answered HTTP {e.code}: {detail}') from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OemTokenError(f'{url} cannot be reached: {e}') from e


@runtime_checkable
class OemTokenProvider(Protocol):
    """A right-to-repair portal integration. `name` is unique in the registry; `oem` is the manufacturer it serves.
    `request_token` obtains a token for a vehicle, given the shop's decrypted `credentials`. `gateway_unlock`
    optionally tells the caller how to present the token to that OEM's gateway (None when the mechanism is not
    known/public, in which case the token is returned for the caller to present some documented way)."""

    name: str
    oem: str

    def request_token(self, request: TokenRequest, credentials: Mapping[str, bytes]) -> OemToken: ...

    def gateway_unlock(self, token: OemToken) -> Optional[GatewayUnlock]: ...


def _require_https(url: str, what: str) -> str:
    url = url.strip()
    if not url.startswith(('https://', 'http://')):
        raise OemTokenError(f'The {what} starts with https:// (or http:// for local tests)')
    return url


class HttpTokenProvider:
    """A provider for a REST right-to-repair portal that issues a token from a single POST.

    The shop's credentials (from the key store) are sent as a bearer `access_token` secret, or as `username` /
    `password` secrets in the body, whichever the portal uses. `token_field` and `expires_field` name the response
    keys to read; `field_map` maps `TokenRequest` attributes to the body field names the portal expects."""

    def __init__(
        self,
        name: str,
        oem: str,
        token_url: str,
        kind: str = BEARER,
        token_field: str = 'token',
        expires_field: str = 'expires_in',
        scope_field: str = 'scope',
        field_map: Optional[Mapping[str, str]] = None,
        transport: Optional[Transport] = None,
        timeout: float = 15.0,
        unlock: Optional[GatewayUnlock] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.oem = oem
        self.token_url = _require_https(token_url, 'portal token endpoint')
        if kind not in TOKEN_KINDS:
            raise OemTokenError(f'Unknown token kind {kind!r}; one of {", ".join(TOKEN_KINDS)}')
        self.kind = kind
        self.token_field = token_field
        self.expires_field = expires_field
        self.scope_field = scope_field
        self.field_map = dict(field_map or {'vin': 'vin', 'make': 'make'})
        self._transport = transport or _urllib_transport
        self.timeout = timeout
        self._unlock = unlock
        self._clock = clock

    def __repr__(self) -> str:
        return f'HttpTokenProvider(name={self.name!r}, oem={self.oem!r})'

    def _body(self, request: TokenRequest, credentials: Mapping[str, bytes]) -> Tuple[Dict[str, str], Dict[str, str]]:
        fields: Dict[str, str] = {}
        for attribute, portal_field in self.field_map.items():
            value = getattr(request, attribute, '') if attribute != 'extra' else ''
            if value:
                fields[portal_field] = value
        fields.update(request.extra)
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
        access = credentials.get('access_token')
        if access:
            headers['Authorization'] = 'Bearer ' + access.decode('utf-8')
        else:
            username, password = credentials.get('username'), credentials.get('password')
            if username:
                fields['username'] = username.decode('utf-8')
            if password:
                fields['password'] = password.decode('utf-8')
        return fields, headers

    def request_token(self, request: TokenRequest, credentials: Mapping[str, bytes]) -> OemToken:
        fields, headers = self._body(request, credentials)
        data = urllib.parse.urlencode(fields).encode('ascii')
        answer = self._transport(self.token_url, data, headers, self.timeout)
        value = answer.get(self.token_field)
        if not value:
            raise OemTokenError(f'{self.oem} portal returned no {self.token_field!r}')
        expires_in = answer.get(self.expires_field)
        expires_at = self._clock() + float(expires_in) if expires_in else 0.0
        audit.record('oem token issued', provider=self.name, oem=self.oem, vin=request.vin or '', kind=self.kind)
        return OemToken(
            value=str(value),
            kind=self.kind,
            expires_at=expires_at,
            scope=str(answer.get(self.scope_field, '')),
            oem=self.oem,
            metadata={k: str(v) for k, v in answer.items() if k not in (self.token_field, self.expires_field)},
        )

    def gateway_unlock(self, token: OemToken) -> Optional[GatewayUnlock]:
        return self._unlock


class OAuthTokenProvider:
    """A provider for an OAuth 2.0 right-to-repair portal. The access token from an `oauth.TokenSource` is itself
    the gateway access token (the common pattern for portals built on OAuth). The `TokenSource` is created by the
    caller from stored credentials (client-credentials or a signed-in device grant), so this provider holds no
    secrets of its own."""

    def __init__(
        self,
        name: str,
        oem: str,
        token_source: Any,
        kind: str = BEARER,
        scope: str = '',
        lifetime: float = 0.0,
        unlock: Optional[GatewayUnlock] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.oem = oem
        self._token_source = token_source
        if kind not in TOKEN_KINDS:
            raise OemTokenError(f'Unknown token kind {kind!r}; one of {", ".join(TOKEN_KINDS)}')
        self.kind = kind
        self.scope = scope
        self.lifetime = lifetime
        self._unlock = unlock
        self._clock = clock

    def __repr__(self) -> str:
        return f'OAuthTokenProvider(name={self.name!r}, oem={self.oem!r})'

    def request_token(self, request: TokenRequest, credentials: Mapping[str, bytes]) -> OemToken:
        value = self._token_source.access_token()
        expires_at = self._clock() + self.lifetime if self.lifetime else 0.0
        audit.record('oem token issued', provider=self.name, oem=self.oem, vin=request.vin or '', kind=self.kind)
        return OemToken(value=value, kind=self.kind, expires_at=expires_at, scope=self.scope, oem=self.oem)

    def gateway_unlock(self, token: OemToken) -> Optional[GatewayUnlock]:
        return self._unlock


@dataclass
class LoadedProvider:
    provider: OemTokenProvider
    source: str


def entry_point_providers() -> List[LoadedProvider]:
    """The providers installed packages register (entry point group `can_explorer.oem_token_providers`). A broken
    provider is skipped and recorded, not allowed to stop the others."""
    import importlib.metadata

    loaded: List[LoadedProvider] = []
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            factory = entry_point.load()
            provider = factory() if callable(factory) else factory
            loaded.append(LoadedProvider(provider, f'entry point {entry_point.name}'))
        except Exception as e:  # noqa: BLE001 - a broken plug-in must not stop the others
            audit.record('oem token provider failed to load', entry_point=entry_point.name, error=str(e))
    return loaded


class ProviderRegistry:
    """The loaded OEM token providers, looked up by name or by OEM."""

    def __init__(self) -> None:
        self.providers: List[LoadedProvider] = []

    def add(self, provider: OemTokenProvider, source: str = 'added') -> None:
        if not getattr(provider, 'name', ''):
            raise OemTokenError('A provider needs a name')
        self.providers = [p for p in self.providers if p.provider.name != provider.name]
        self.providers.append(LoadedProvider(provider, source))
        audit.record('oem token provider loaded', provider=provider.name, oem=getattr(provider, 'oem', ''))

    def remove(self, name: str) -> None:
        self.providers = [p for p in self.providers if p.provider.name != name]

    def names(self) -> List[str]:
        return [p.provider.name for p in self.providers]

    def get(self, name: str) -> OemTokenProvider:
        for loaded in self.providers:
            if loaded.provider.name == name:
                return loaded.provider
        raise OemTokenError(f'No OEM token provider named {name!r}')

    def for_oem(self, oem: str) -> List[OemTokenProvider]:
        oem = oem.strip().casefold()
        return [p.provider for p in self.providers if getattr(p.provider, 'oem', '').casefold() == oem]

    def load_entry_points(self) -> None:
        for loaded in entry_point_providers():
            self.providers = [p for p in self.providers if p.provider.name != loaded.provider.name]
            self.providers.append(loaded)


_registry = ProviderRegistry()


def registry() -> ProviderRegistry:
    """The application's OEM token providers."""
    return _registry


def set_registry(providers: ProviderRegistry) -> None:
    global _registry
    _registry = providers
