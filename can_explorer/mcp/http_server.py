"""Run the MCP server inside the application over localhost HTTP, so an agent can attach to a live connection the
GUI already has open (which a separate can-mcp process usually cannot, as most adapters are single-access).

It is loopback-only (127.0.0.1) and token-gated: every request must carry the token as an `Authorization: Bearer`
header or a `?token=` query parameter. It uses the MCP streamable-HTTP transport, served by uvicorn as a task on
the running event loop. Needs the `mcp` extra (which brings uvicorn and starlette).
"""

import asyncio
import secrets
import socket
from typing import Optional
from urllib.parse import parse_qs

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

DEFAULT_PATH = '/mcp'


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


class TokenAuth:
    """ASGI middleware requiring the token on every HTTP request; non-HTTP scopes (lifespan) pass through."""

    def __init__(self, app, token: str):
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and not self._authorised(scope):
            await send({'type': 'http.response.start', 'status': 401, 'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': b'Unauthorized: missing or wrong token'})
            return
        await self._app(scope, receive, send)

    def _authorised(self, scope) -> bool:
        headers = {key.lower(): value for key, value in scope.get('headers') or []}
        authorization = headers.get(b'authorization', b'').decode('latin-1')
        if authorization.startswith('Bearer ') and secrets.compare_digest(authorization[7:], self._token):
            return True
        query = parse_qs(scope.get('query_string', b'').decode('latin-1'))
        return secrets.compare_digest(query.get('token', [''])[0], self._token)


class InAppMcpServer:
    """A localhost, token-gated MCP server over streamable HTTP for a CanService, run on the current event loop."""

    def __init__(self, service, *, host: str = '127.0.0.1', port: int = 0, token: Optional[str] = None):
        self.service = service
        self.host = host
        self.port = port or _free_port(host)
        self.token = token or secrets.token_urlsafe(16)
        self.path = DEFAULT_PATH
        self._server = None
        self._task: Optional[asyncio.Future] = None

    @property
    def url(self) -> str:
        return f'http://{self.host}:{self.port}{self.path}'

    async def start(self) -> str:
        """Start serving; returns the URL. Raises if the server could not start."""
        import uvicorn

        from can_explorer.mcp.server import build_mcp

        mcp = build_mcp(self.service)
        app = TokenAuth(mcp.streamable_http_app(host=self.host, streamable_http_path=self.path), self.token)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level='warning', lifespan='on')
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # Qt owns the signals; do not touch them
        self._task = asyncio.ensure_future(self._server.serve())
        while not self._server.started and not self._task.done():
            await asyncio.sleep(0.02)
        if self._task.done() and self._task.exception() is not None:
            raise self._task.exception()
        logger.info('In-app MCP server listening on %s', self.url)
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001 - stop must not raise
                self._task.cancel()
        self._task = None
        self._server = None
