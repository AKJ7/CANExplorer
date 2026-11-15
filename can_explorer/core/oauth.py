"""OAuth 2.0 access tokens for key servers (core/security_profiles.py), so a key server can sit behind a company's
identity provider instead of a long-lived static token.

Two grants, as their RFCs specify them:

- **Client credentials** (RFC 6749 §4.4): for a bench computer with an account of its own. The client ID and secret
  are kept encrypted in the key store; tokens are fetched when needed and before they expire.
- **Device authorization** (RFC 8628): for a person signing in with their own account. The tester shows a code and
  a web address; the user signs in there (in any browser, with whatever their identity provider asks for), and the
  tester polls the token endpoint until it is done. The refresh token it receives (RFC 6749 §6) is kept encrypted,
  so later starts need no new sign-in until the identity provider ends the session.

Clients authenticate with HTTP Basic when they have a secret (RFC 6749 §2.3.1, which every server must support), and
send only their client ID otherwise (public clients, typical for the device grant). Only the standard parameters are
used; nothing provider-specific.
"""

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional

CLIENT_CREDENTIALS = 'client-credentials'
DEVICE = 'device'
GRANTS = (CLIENT_CREDENTIALS, DEVICE)
DEVICE_CODE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'  # RFC 8628 §3.4
EXPIRY_MARGIN = 30.0  # Seconds: a token expiring sooner is renewed first


class OAuthError(Exception):
    def __init__(self, message: str, code: str = ''):
        super().__init__(message)
        self.code = code  # The RFC 6749 §5.2 error code of an error response, e.g. 'invalid_grant'


class SignInRequired(OAuthError):
    """The device grant has no (valid) refresh token: the user must sign in again."""


@dataclass
class OAuthConfig:
    grant: str  # CLIENT_CREDENTIALS or DEVICE
    token_url: str
    client_id: str
    client_secret: str = ''
    scope: str = ''
    device_url: str = ''  # The device authorization endpoint (DEVICE only)

    def __post_init__(self):
        if self.grant not in GRANTS:
            raise OAuthError(f'Unknown OAuth grant {self.grant!r}; one of {", ".join(GRANTS)}')
        for name, url in (('token', self.token_url), ('device authorization', self.device_url)):
            if url and not url.startswith(('https://', 'http://')):
                raise OAuthError(f'The {name} endpoint starts with https:// (or http:// for local tests)')
        if not self.token_url or not self.client_id:
            raise OAuthError('OAuth needs the token endpoint and the client ID')
        if self.grant == CLIENT_CREDENTIALS and not self.client_secret:
            raise OAuthError('The client credentials grant needs the client secret')
        if self.grant == DEVICE and not self.device_url:
            raise OAuthError('Device sign-in needs the device authorization endpoint')

    def __repr__(self) -> str:  # Never show the secret
        return f'OAuthConfig({self.grant!r}, {self.token_url!r}, client_id={self.client_id!r})'


@dataclass
class DeviceAuthorization:
    """What the user needs to sign in (RFC 8628 §3.2)."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str = ''
    interval: float = 5.0
    expires_at: float = 0.0


def _post_form(url: str, fields: Dict[str, str], config: OAuthConfig, timeout: float, context=None) -> Dict:
    """POST an application/x-www-form-urlencoded request; the JSON answer, or OAuthError with the RFC 6749 §5.2
    error code (`OAuthError.code`) for an error response."""
    data = {key: value for key, value in fields.items() if value}
    headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
    if config.client_secret:  # RFC 6749 §2.3.1: form-encoded credentials in HTTP Basic
        user = urllib.parse.quote(config.client_id, safe='') + ':' + urllib.parse.quote(config.client_secret, safe='')
        headers['Authorization'] = 'Basic ' + base64.b64encode(user.encode('utf-8')).decode('ascii')
    else:
        data['client_id'] = config.client_id
    request = urllib.request.Request(url, urllib.parse.urlencode(data).encode('ascii'), headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b'{}')
        except ValueError:
            body = {}
        detail = body.get('error_description') or body.get('error') or e.reason
        raise OAuthError(f'{url} answered HTTP {e.code}: {detail}', str(body.get('error', ''))) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OAuthError(f'{url} cannot be reached: {e}') from e


class TokenSource:
    """Access tokens for one key server: cached until shortly before they expire, then renewed with the client
    credentials or the refresh token. Thread safe (key servers are called from worker threads).

    `on_refresh_token` is called with a new refresh token whenever the server issues one (store it: refresh tokens
    may rotate, RFC 6749 §6), and with '' when it is no longer valid."""

    def __init__(
        self,
        config: OAuthConfig,
        refresh_token: str = '',
        on_refresh_token: Optional[Callable[[str], None]] = None,
        timeout: float = 10.0,
        context=None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self.timeout = timeout
        self._context = context
        self._clock = clock
        self._access_token = ''
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f'TokenSource({self.config!r})'

    @property
    def signed_in(self) -> bool:
        """Whether tokens can be had without the user (always for client credentials)."""
        return self.config.grant == CLIENT_CREDENTIALS or bool(self._refresh_token or self._access_token)

    def access_token(self) -> str:
        with self._lock:
            if self._access_token and self._clock() < self._expires_at - EXPIRY_MARGIN:
                return self._access_token
            if self.config.grant == CLIENT_CREDENTIALS:
                self._take(self._token_request({'grant_type': 'client_credentials', 'scope': self.config.scope}))
            elif self._refresh_token:
                self._refresh()
            else:
                raise SignInRequired('Sign in to the key server first')
            return self._access_token

    def invalidate(self) -> None:
        """Forget the access token (e.g. after the server refused it); the next call gets a new one."""
        with self._lock:
            self._access_token, self._expires_at = '', 0.0

    def _refresh(self) -> None:
        fields = {'grant_type': 'refresh_token', 'refresh_token': self._refresh_token, 'scope': self.config.scope}
        try:
            self._take(self._token_request(fields))
        except OAuthError as e:
            if e.code == 'invalid_grant':  # Expired or revoked: sign in again
                self._set_refresh_token('')
                raise SignInRequired('The sign-in to the key server has expired; sign in again') from e
            raise

    def _token_request(self, fields: Dict[str, str]) -> Dict:
        return _post_form(self.config.token_url, fields, self.config, self.timeout, self._context)

    def _take(self, answer: Dict) -> None:
        token = answer.get('access_token')
        if not token or str(answer.get('token_type', 'bearer')).lower() != 'bearer':
            raise OAuthError('The token endpoint returned no bearer token')
        self._access_token = str(token)
        expires_in = answer.get('expires_in')
        self._expires_at = self._clock() + (float(expires_in) if expires_in else 3600.0)
        if answer.get('refresh_token') and answer['refresh_token'] != self._refresh_token:
            self._set_refresh_token(str(answer['refresh_token']))

    def _set_refresh_token(self, token: str) -> None:
        self._refresh_token = token
        if self._on_refresh_token is not None:
            self._on_refresh_token(token)

    # The device grant (RFC 8628)

    def start_device_authorization(self) -> DeviceAuthorization:
        """Ask for a user code (RFC 8628 §3.1); show its `user_code` and `verification_uri` to the user."""
        if self.config.grant != DEVICE:
            raise OAuthError('Only device sign-in asks the user to sign in')
        answer = _post_form(
            self.config.device_url, {'scope': self.config.scope}, self.config, self.timeout, self._context
        )
        try:
            return DeviceAuthorization(
                device_code=str(answer['device_code']),
                user_code=str(answer['user_code']),
                verification_uri=str(answer.get('verification_uri') or answer['verification_url']),
                verification_uri_complete=str(answer.get('verification_uri_complete', '')),
                interval=float(answer.get('interval', 5)),
                expires_at=self._clock() + float(answer.get('expires_in', 600)),
            )
        except (KeyError, ValueError) as e:
            raise OAuthError(f'The device authorization endpoint answered without {e}') from e

    def poll_device_authorization(self, authorization: DeviceAuthorization) -> bool:
        """One poll of the token endpoint (RFC 8628 §3.4/§3.5): True once signed in, False while the user has not
        finished (wait `authorization.interval` seconds, which slow_down lengthens). Raises OAuthError when the
        user refused, the code expired, or the server failed."""
        if self._clock() > authorization.expires_at:
            raise OAuthError('The sign-in code has expired; start again')
        fields = {'grant_type': DEVICE_CODE_GRANT, 'device_code': authorization.device_code}
        try:
            answer = self._token_request(fields)
        except OAuthError as e:
            code = e.code
            if code == 'authorization_pending':
                return False
            if code == 'slow_down':
                authorization.interval += 5.0  # RFC 8628 §3.5
                return False
            if code == 'access_denied':
                raise OAuthError('The sign-in was refused') from e
            if code == 'expired_token':
                raise OAuthError('The sign-in code has expired; start again') from e
            raise
        with self._lock:
            self._take(answer)
        return True

    def sign_out(self) -> None:
        with self._lock:
            self._access_token, self._expires_at = '', 0.0
            self._set_refresh_token('')
