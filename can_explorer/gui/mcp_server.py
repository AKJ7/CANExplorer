"""The in-app MCP server of a connection: an opt-in, localhost, token-gated server that exposes this connection's
live session to an AI agent (see can_explorer.mcp.http_server). Default off; the permission tier is chosen here."""

import asyncio
from typing import Dict, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

TIERS = ('read-only', 'diagnostics', 'control', 'send')
TIER_FLAGS: Dict[str, Dict[str, bool]] = {
    'diagnostics': {'allow_diagnostics': True},
    'control': {'allow_control': True},
    'send': {'allow_send': True},
}


class McpServerController(QObject):
    """Starts and stops the in-app MCP server for one connection. Exposes the server URL and token for the agent's
    client, and the chosen permission tier. It needs the `mcp` extra; `available` says whether it is installed."""

    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        self._server = None  # InAppMcpServer while running
        self._tier = 'read-only'
        self._url = ''
        self._token = ''
        self._error = ''
        self._starting = False

    @pyqtProperty(bool, constant=True)
    def available(self) -> bool:
        """Whether the MCP SDK (the `mcp` extra) is installed."""
        try:
            import mcp  # noqa: F401

            return True
        except ImportError:
            return False

    @pyqtProperty(bool, notify=changed)
    def running(self) -> bool:
        return self._server is not None

    @pyqtProperty('QStringList', constant=True)
    def tiers(self) -> list:
        return list(TIERS)

    @pyqtProperty(str, notify=changed)
    def tier(self) -> str:
        return self._tier

    @pyqtProperty(str, notify=changed)
    def url(self) -> str:
        return self._url

    @pyqtProperty(str, notify=changed)
    def token(self) -> str:
        return self._token

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._error

    @pyqtSlot(str, result=str)
    def start(self, tier: str) -> str:
        """Start the server with the given permission tier. Returns an error, or ''."""
        if self._server is not None or self._starting:
            return ''
        if not self.connection.session.is_open:
            return 'Connect to a bus first'
        if tier in TIERS:
            self._tier = tier
        try:
            from can_explorer.mcp.http_server import InAppMcpServer
            from can_explorer.mcp.service import CanService
        except ImportError:
            return "The MCP SDK is not installed. Install it with:  pip install 'can-explorer[mcp]'"
        service = CanService.attach(self.connection.session, **TIER_FLAGS.get(self._tier, {}))
        self._error = ''
        self._starting = True
        self.changed.emit()
        asyncio.ensure_future(self._start(InAppMcpServer(service)))
        return ''

    async def _start(self, server) -> None:
        try:
            url = await server.start()
        except Exception as e:
            logger.warning('In-app MCP server failed to start: %s', e)
            self._error = str(e)
            self._starting = False
            self.changed.emit()
            return
        self._server = server
        self._url = url
        self._token = server.token
        self._starting = False
        self.changed.emit()

    @pyqtSlot()
    def stop(self) -> None:
        server, self._server = self._server, None
        self._url = self._token = ''
        self.changed.emit()
        if server is not None:
            asyncio.ensure_future(self._stop(server))

    async def _stop(self, server) -> None:
        try:
            await server.stop()
        except Exception as e:  # Stopping must not raise
            logger.debug('In-app MCP server stop: %s', e)

    def shutdown(self) -> None:
        self.stop()
