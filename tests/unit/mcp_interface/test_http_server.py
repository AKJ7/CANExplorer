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


async def test_in_app_server_disables_uvicorn_log_config(monkeypatch):
    """A windowed Windows build has no console, so sys.stdout is None. uvicorn's default log config builds a colour
    formatter whose __init__ calls sys.stdout.isatty(); with no stdout that raises and logging.config.dictConfig
    re-wraps it as "Unable to configure formatter 'default'", so selecting an MCP (agent) tier failed to start the
    server. The server must therefore pass log_config=None so uvicorn configures no logging of its own."""
    pytest.importorskip('mcp')

    import uvicorn

    from can_explorer.mcp.http_server import InAppMcpServer
    from can_explorer.mcp.service import CanService

    captured = {}

    class _ConfigSpy(uvicorn.Config):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(uvicorn, 'Config', _ConfigSpy)
    # Emulate the windowed-Windows console: no stdout for uvicorn's formatter to probe. With log_config left at its
    # default this very construction raises ValueError('Unable to configure formatter ...'); log_config=None avoids it.
    monkeypatch.setattr('sys.stdout', None)

    session = BusSession(LiveSource(BusConfig('virtual', f'mcp-{uuid.uuid4().hex}')))
    session.start()
    server = InAppMcpServer(CanService.attach(session))
    try:
        await server.start()  # Must not raise "Unable to configure formatter 'default'"
        assert 'log_config' in captured and captured['log_config'] is None
    finally:
        await server.stop()
        session.stop()
