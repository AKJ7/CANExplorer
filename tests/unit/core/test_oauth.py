import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from can_explorer.core import oauth
from can_explorer.core.oauth import OAuthConfig, OAuthError, SignInRequired, TokenSource
from can_explorer.core.security_profiles import KeyServerProfile, SecurityProfileError


class IdentityProvider(BaseHTTPRequestHandler):
    """An OAuth 2.0 authorization server as RFC 6749 and RFC 8628 describe it, plus a key server accepting its
    access tokens."""

    state = {}

    def log_message(self, *args):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, error, code=400):
        self._json(code, {'error': error})

    def do_POST(self):
        state = IdentityProvider.state
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers['Content-Length'])).decode()))
        state['requests'].append((self.path, form, self.headers.get('Authorization', '')))
        if self.path == '/keys/uds/key':  # The key server
            if self.headers.get('Authorization') not in {f'Bearer {token}' for token in state['valid_access']}:
                return self._error('invalid_token', 401)
            return self._json(200, {'key': 'abcd'})
        if self.path == '/device':
            return self._json(
                200,
                {
                    'device_code': 'DEVICE-1',
                    'user_code': 'WDJB-MJHT',
                    'verification_uri': 'https://login.example.com/device',
                    'interval': 0,
                    'expires_in': 600,
                },
            )
        if self.path != '/token':
            return self._error('not_found', 404)
        grant = form.get('grant_type')
        if grant == 'client_credentials':
            expected = 'Basic ' + base64.b64encode(b'bench:s%3Acret').decode()
            if self.headers.get('Authorization') != expected:
                return self._error('invalid_client', 401)
            return self._issue(refresh=False)
        if grant == 'refresh_token':
            if form.get('refresh_token') != state['refresh']:
                return self._error('invalid_grant')
            return self._issue(refresh=True)
        if grant == oauth.DEVICE_CODE_GRANT:
            answers = state['device_answers']
            answer = answers.pop(0) if answers else 'approve'
            if answer != 'approve':
                return self._error(answer)
            return self._issue(refresh=True)
        return self._error('unsupported_grant_type')

    def _issue(self, refresh):
        state = IdentityProvider.state
        state['issued'] += 1
        token = f'access-{state["issued"]}'
        state['valid_access'].add(token)
        body = {'access_token': token, 'token_type': 'Bearer', 'expires_in': 300}
        if refresh:
            state['refresh'] = f'refresh-{state["issued"]}'  # Rotated on every use
            body['refresh_token'] = state['refresh']
        self._json(200, body)


@pytest.fixture
def provider():
    IdentityProvider.state = {
        'requests': [],
        'issued': 0,
        'refresh': '',
        'valid_access': set(),
        'device_answers': [],
    }
    server = ThreadingHTTPServer(('127.0.0.1', 0), IdentityProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}', IdentityProvider.state
    server.shutdown()
    server.server_close()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_config_checks():
    with pytest.raises(OAuthError, match='Unknown OAuth grant'):
        OAuthConfig('password', 'https://idp/token', 'id')
    with pytest.raises(OAuthError, match='needs the client secret'):
        OAuthConfig(oauth.CLIENT_CREDENTIALS, 'https://idp/token', 'id')
    with pytest.raises(OAuthError, match='device authorization endpoint'):
        OAuthConfig(oauth.DEVICE, 'https://idp/token', 'id')
    with pytest.raises(OAuthError, match='starts with https://'):
        OAuthConfig(oauth.DEVICE, 'ftp://idp/token', 'id', device_url='https://idp/device')
    assert 'secret' not in repr(OAuthConfig(oauth.CLIENT_CREDENTIALS, 'https://idp/token', 'id', 'secret'))


def test_client_credentials_are_renewed_before_they_expire(provider):
    url, state = provider
    clock = Clock()
    config = OAuthConfig(oauth.CLIENT_CREDENTIALS, f'{url}/token', 'bench', 's:cret', scope='keys')
    source = TokenSource(config, clock=clock)
    assert source.signed_in and source.access_token() == 'access-1'
    path, form, authorization = state['requests'][-1]
    assert form == {'grant_type': 'client_credentials', 'scope': 'keys'}  # The secret only in HTTP Basic
    assert authorization.startswith('Basic ')
    clock.now += 200
    assert source.access_token() == 'access-1'  # Cached
    clock.now += 80  # Within the 30 s margin of its 300 s
    assert source.access_token() == 'access-2'
    wrong = TokenSource(OAuthConfig(oauth.CLIENT_CREDENTIALS, f'{url}/token', 'bench', 'wrong'))
    with pytest.raises(OAuthError, match='invalid_client') as refused:
        wrong.access_token()
    assert refused.value.code == 'invalid_client'


def test_device_sign_in_and_rotated_refresh_tokens(provider):
    url, state = provider
    clock = Clock()
    kept = []
    config = OAuthConfig(oauth.DEVICE, f'{url}/token', 'tester', device_url=f'{url}/device', scope='keys')
    source = TokenSource(config, on_refresh_token=kept.append, clock=clock)
    assert not source.signed_in
    with pytest.raises(SignInRequired):
        source.access_token()
    authorization = source.start_device_authorization()
    assert (authorization.user_code, authorization.verification_uri) == (
        'WDJB-MJHT',
        'https://login.example.com/device',
    )
    assert state['requests'][-1][1] == {'scope': 'keys', 'client_id': 'tester'}  # A public client
    state['device_answers'] = ['authorization_pending', 'slow_down']
    assert source.poll_device_authorization(authorization) is False
    assert source.poll_device_authorization(authorization) is False
    assert authorization.interval == 5.0  # slow_down adds 5 s (RFC 8628 §3.5)
    assert source.poll_device_authorization(authorization) is True
    assert source.signed_in and kept == ['refresh-1'] and source.access_token() == 'access-1'

    clock.now += 400  # Expired: refreshed, and the rotated refresh token kept
    assert source.access_token() == 'access-2' and kept == ['refresh-1', 'refresh-2']
    assert state['requests'][-1][1]['refresh_token'] == 'refresh-1'

    # A later start with the kept refresh token needs no sign-in
    later = TokenSource(config, refresh_token=kept[-1], on_refresh_token=kept.append)
    assert later.signed_in and later.access_token() == 'access-3'
    # Revoked at the identity provider: sign in again
    state['refresh'] = 'revoked'
    stale = TokenSource(config, refresh_token='refresh-3', on_refresh_token=kept.append)
    with pytest.raises(SignInRequired, match='expired'):
        stale.access_token()
    assert kept[-1] == '' and not stale.signed_in


@pytest.mark.parametrize(
    'answer, message', [('access_denied', 'refused'), ('expired_token', 'expired'), ('server_error', 'server_error')]
)
def test_device_sign_in_failures(provider, answer, message):
    url, state = provider
    config = OAuthConfig(oauth.DEVICE, f'{url}/token', 'tester', device_url=f'{url}/device')
    source = TokenSource(config)
    authorization = source.start_device_authorization()
    state['device_answers'] = [answer]
    with pytest.raises(OAuthError, match=message):
        source.poll_device_authorization(authorization)
    clock = Clock()
    expired = TokenSource(config, clock=clock)
    authorization = expired.start_device_authorization()
    clock.now += 601
    with pytest.raises(OAuthError, match='code has expired'):
        expired.poll_device_authorization(authorization)
    with pytest.raises(OAuthError, match='Only device sign-in'):
        TokenSource(OAuthConfig(oauth.CLIENT_CREDENTIALS, f'{url}/token', 'id', 's')).start_device_authorization()


def test_key_server_with_oauth_tokens(provider):
    url, state = provider
    config = OAuthConfig(oauth.CLIENT_CREDENTIALS, f'{url}/token', 'bench', 's:cret')
    source = TokenSource(config)
    profile = KeyServerProfile('Keys', f'{url}/keys', source)
    assert profile.signed_in and not profile.device_sign_in
    assert profile.compute_key(1, b'\x01') == bytes.fromhex('abcd')
    assert state['requests'][-1][2] == 'Bearer access-1'
    state['valid_access'].clear()  # Revoked early: the key server answers 401, a new token is fetched once
    assert profile.compute_key(1, b'\x01') == bytes.fromhex('abcd')
    assert state['requests'][-1][2] == 'Bearer access-2'
    state['valid_access'].clear()
    source.access_token = lambda: 'never-valid'  # The server keeps refusing: reported, not retried forever
    with pytest.raises(SecurityProfileError, match='HTTP 401'):
        profile.compute_key(1, b'\x01')

    device = TokenSource(OAuthConfig(oauth.DEVICE, f'{url}/token', 'tester', device_url=f'{url}/device'))
    signed_out = KeyServerProfile('Keys', f'{url}/keys', device)
    assert signed_out.device_sign_in and not signed_out.signed_in
    with pytest.raises(SecurityProfileError, match='Sign in to the key server first'):
        signed_out.compute_key(1, b'\x01')
