import uuid

import pytest
from click.testing import CliRunner

from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.mcp.__main__ import main
from can_explorer.mcp.service import CanService


def _service(allow_send):
    source = LiveSource(BusConfig('virtual', f'mcp-{uuid.uuid4().hex}'))
    return CanService(source, allow_send=allow_send)


async def test_tools_registered_read_only():
    pytest.importorskip('mcp')
    from can_explorer.mcp.server import build_mcp

    server = build_mcp(_service(allow_send=False))
    names = {tool.name for tool in await server.list_tools()}
    assert names == {
        'bus_status',
        'list_identifiers',
        'get_frames',
        'decode_frame',
        'analyze_identifier',
        'load_database',
        'correlate',
        'promote_to_dbc',
    }


async def test_send_tools_only_when_allowed():
    pytest.importorskip('mcp')
    from can_explorer.mcp.server import build_mcp

    server = build_mcp(_service(allow_send=True))
    names = {tool.name for tool in await server.list_tools()}
    assert {'send_frame', 'query_obd'} <= names


def test_cli_help_without_mcp_sdk():
    """The command's --help works even when the MCP SDK is not installed (it is imported lazily)."""
    result = CliRunner().invoke(main, ['--help'])
    assert result.exit_code == 0
    assert '--allow-send' in result.output and '--interface' in result.output
