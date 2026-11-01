import uuid

import pytest

from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.mcp.http_server import TokenAuth


async def _call(middleware, headers=None, query=b''):
    scope = {'type': 'http', 'headers': headers or [], 'query_string': query}
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {'type': 'http.request', 'body': b''}

    await middleware(scope, receive, send)
    return sent


async def test_token_auth_middleware():
    passed = []

    async def inner(scope, receive, send):
        passed.append(scope['type'])

    middleware = TokenAuth(inner, 'secret')

    assert (await _call(middleware))[0]['status'] == 401 and not passed  # No token
    assert (await _call(middleware, headers=[(b'authorization', b'Bearer nope')]))[0]['status'] == 401  # Wrong token

    await _call(middleware, headers=[(b'authorization', b'Bearer secret')])  # Bearer header
    await _call(middleware, query=b'token=secret')  # Query parameter
    assert passed == ['http', 'http']

    passed.clear()
    await middleware({'type': 'lifespan'}, None, None)  # Non-HTTP scopes pass through unchecked
    assert passed == ['lifespan']


async def test_in_app_server_is_loopback_and_token_gated():
    pytest.importorskip('mcp')
    import aiohttp

    from can_explorer.mcp.http_server import InAppMcpServer
    from can_explorer.mcp.service import CanService

    session = BusSession(LiveSource(BusConfig('virtual', f'mcp-{uuid.uuid4().hex}')))
    session.start()
    server = InAppMcpServer(CanService.attach(session))
    url = await server.start()
    try:
        assert url.startswith('http://127.0.0.1:') and server.token
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as response:
                assert response.status == 401  # No token
            async with http.get(url, headers={'Authorization': f'Bearer {server.token}'}) as response:
                assert response.status != 401  # Reachable and authorised (a bare GET is otherwise rejected)
    finally:
        await server.stop()
        session.stop()
