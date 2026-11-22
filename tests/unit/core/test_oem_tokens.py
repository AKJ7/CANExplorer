import pytest

from can_explorer.core import oem_tokens
from can_explorer.core.oem_tokens import (
    BEARER,
    CHALLENGE,
    ONE_TIME,
    GatewayUnlock,
    HttpTokenProvider,
    OAuthTokenProvider,
    OemToken,
    OemTokenError,
    OemTokenProvider,
    ProviderRegistry,
    TokenRequest,
)


def fake_transport(answer, record=None):
    def transport(url, data, headers, timeout):
        if record is not None:
            record.append((url, data, dict(headers)))
        return answer

    return transport


def test_token_request_normalises_vin_and_make():
    request = TokenRequest(vin=' 1hgbh41jxmn ', make='  Honda ')
    assert request.vin == '1HGBH41JXMN'
    assert request.make == 'Honda'


def test_token_request_rejects_non_alphanumeric_vin():
    with pytest.raises(OemTokenError):
        TokenRequest(vin='1HG-41J')


def test_token_repr_hides_the_value_and_kind_is_validated():
    token = OemToken(value='s3cr3t', kind=ONE_TIME, oem='Stellantis', scope='diag')
    assert 's3cr3t' not in repr(token)
    assert 'one-time' in repr(token)
    with pytest.raises(OemTokenError):
        OemToken(value='x', kind='nonsense')


def test_token_expiry_margin_and_no_lifetime():
    assert OemToken('x', expires_at=0.0).expired(now=10_000) is False  # No lifetime never expires here
    token = OemToken('x', expires_at=100.0)
    assert token.expired(now=90) is False
    assert token.expired(now=90, margin=15) is True
    assert token.expired(now=100) is True


def test_http_provider_posts_credentials_and_reads_the_token():
    record = []
    transport = fake_transport({'token': 'ABC123', 'expires_in': 600, 'scope': 'diagnostics'}, record)
    provider = HttpTokenProvider(
        'autoauth',
        'Stellantis',
        'https://portal.example.com/token',
        kind=ONE_TIME,
        transport=transport,
        clock=lambda: 1000.0,
    )
    token = provider.request_token(TokenRequest(vin='1HGBH41JXMN', make='Jeep'), {'access_token': b'login-bearer'})
    assert token.value == 'ABC123'
    assert token.kind == ONE_TIME
    assert token.oem == 'Stellantis'
    assert token.scope == 'diagnostics'
    assert token.expires_at == 1600.0
    url, data, headers = record[0]
    assert headers['Authorization'] == 'Bearer login-bearer'
    assert b'vin=1HGBH41JXMN' in data


def test_http_provider_sends_username_password_when_no_bearer():
    record = []
    provider = HttpTokenProvider(
        'portal',
        'Acme',
        'https://portal.example.com/token',
        transport=fake_transport({'token': 'T'}, record),
    )
    provider.request_token(TokenRequest(make='Acme'), {'username': b'shop', 'password': b'pw'})
    _, data, _ = record[0]
    assert b'username=shop' in data and b'password=pw' in data


def test_http_provider_raises_when_no_token_returned():
    provider = HttpTokenProvider('p', 'Acme', 'https://portal.example.com/token', transport=fake_transport({}))
    with pytest.raises(OemTokenError):
        provider.request_token(TokenRequest(), {})


def test_http_provider_requires_https():
    with pytest.raises(OemTokenError):
        HttpTokenProvider('p', 'Acme', 'ftp://portal.example.com/token')


class FakeTokenSource:
    def __init__(self, token='oauth-access'):
        self._token = token

    def access_token(self):
        return self._token


def test_oauth_provider_uses_the_token_source():
    provider = OAuthTokenProvider(
        'odis',
        'VW',
        FakeTokenSource(),
        scope='rtr',
        lifetime=3600.0,
        clock=lambda: 50.0,
    )
    token = provider.request_token(TokenRequest(vin='WVWZZZ1KZAW000001'), {})
    assert token.value == 'oauth-access'
    assert token.oem == 'VW'
    assert token.scope == 'rtr'
    assert token.expires_at == 3650.0


def test_gateway_unlock_is_provider_supplied():
    unlock = GatewayUnlock(service=0x27, build=lambda token: token.value.encode(), description='OEM SGW unlock')
    provider = HttpTokenProvider(
        'p',
        'Acme',
        'https://portal.example.com/token',
        transport=fake_transport({'token': 'KEY'}),
        unlock=unlock,
    )
    token = provider.request_token(TokenRequest(), {})
    got = provider.gateway_unlock(token)
    assert got is unlock
    assert got.service == 0x27
    assert got.build(token) == b'KEY'


def test_provider_without_unlock_returns_none():
    provider = HttpTokenProvider(
        'p', 'Acme', 'https://portal.example.com/token', transport=fake_transport({'token': 'K'})
    )
    assert provider.gateway_unlock(OemToken('K')) is None


def test_providers_satisfy_the_protocol():
    assert isinstance(HttpTokenProvider('p', 'Acme', 'https://x.example.com/t'), OemTokenProvider)
    assert isinstance(OAuthTokenProvider('o', 'VW', FakeTokenSource()), OemTokenProvider)


def test_registry_add_get_remove_and_lookup_by_oem():
    registry = ProviderRegistry()
    a = HttpTokenProvider('autoauth', 'Stellantis', 'https://a.example.com/t')
    b = OAuthTokenProvider('odis', 'VW', FakeTokenSource())
    registry.add(a)
    registry.add(b)
    assert set(registry.names()) == {'autoauth', 'odis'}
    assert registry.get('odis') is b
    assert registry.for_oem('stellantis') == [a]
    registry.remove('autoauth')
    assert registry.names() == ['odis']
    with pytest.raises(OemTokenError):
        registry.get('autoauth')


def test_registry_add_replaces_same_name():
    registry = ProviderRegistry()
    first = HttpTokenProvider('p', 'Acme', 'https://a.example.com/t')
    second = HttpTokenProvider('p', 'Acme2', 'https://b.example.com/t')
    registry.add(first)
    registry.add(second)
    assert registry.names() == ['p']
    assert registry.get('p') is second


def test_registry_rejects_nameless_provider():
    registry = ProviderRegistry()
    with pytest.raises(OemTokenError):
        registry.add(HttpTokenProvider('', 'Acme', 'https://a.example.com/t'))


def test_module_registry_is_shared_and_swappable():
    original = oem_tokens.registry()
    try:
        replacement = ProviderRegistry()
        oem_tokens.set_registry(replacement)
        assert oem_tokens.registry() is replacement
    finally:
        oem_tokens.set_registry(original)


def test_challenge_kind_is_accepted():
    token = OemToken('abcd', kind=CHALLENGE)
    assert token.kind == CHALLENGE
    assert token.kind in oem_tokens.TOKEN_KINDS and BEARER in oem_tokens.TOKEN_KINDS
