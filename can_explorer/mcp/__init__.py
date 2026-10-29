"""An MCP (Model Context Protocol) interface to a CAN bus, so an agent can inspect and (optionally) drive it.

`service.py` holds the logic (a Qt-free, MCP-free wrapper around a BusSession) and `server.py` exposes it as MCP
tools over stdio. The `can-mcp` command runs the server; the `mcp` extra installs the SDK it needs.
"""

from can_explorer.mcp.service import CanService

__all__ = ['CanService']
