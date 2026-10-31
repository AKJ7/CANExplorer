import uuid

import pytest
from click.testing import CliRunner

from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.mcp.__main__ import main
from can_explorer.mcp.service import CanService


def _service(**kwargs):
    source = LiveSource(BusConfig('virtual', f'mcp-{uuid.uuid4().hex}'))
    return CanService(source, **kwargs)


async def _tool_names(**kwargs):
    from can_explorer.mcp.server import build_mcp

    server = build_mcp(_service(**kwargs))
    return {tool.name for tool in await server.list_tools()}


DIAGNOSTIC_READS = {
    'query_obd',
    'obd_status',
    'obd_dtcs',
    'obd_vin',
    'obd_supported',
    'obd_read',
    'uds_read_did',
    'uds_read_dtcs',
}
CONTROL_TOOLS = {'obd_clear', 'uds_reset', 'uds_routine', 'uds_request'}
SEND_TOOLS = {'send_frame', 'send_message'}


async def test_tools_registered_read_only():
    pytest.importorskip('mcp')
    names = await _tool_names()
    assert names == {
        'bus_status',
        'connect',
        'disconnect',
        'list_identifiers',
        'get_frames',
        'decode_frame',
        'decode_protocol',
        'timing',
        'analyze_identifier',
        'related_signals',
        'capture_reaction',
        'load_database',
        'unload_database',
        'correlate',
        'promote_to_dbc',
        'list_interfaces',
    }
    # No diagnostics, control or send tools in read-only mode
    assert not (names & (DIAGNOSTIC_READS | CONTROL_TOOLS | SEND_TOOLS))


async def test_permission_tiers_gate_tools():
    pytest.importorskip('mcp')
    diagnostics = await _tool_names(allow_diagnostics=True)
    assert DIAGNOSTIC_READS <= diagnostics and not (diagnostics & (CONTROL_TOOLS | SEND_TOOLS))

    control = await _tool_names(allow_control=True)
    assert (DIAGNOSTIC_READS | CONTROL_TOOLS) <= control and not (control & SEND_TOOLS)

    send = await _tool_names(allow_send=True)
    assert (DIAGNOSTIC_READS | CONTROL_TOOLS | SEND_TOOLS) <= send  # --allow-send grants everything


async def test_resources_and_prompts_registered():
    pytest.importorskip('mcp')
    from can_explorer.mcp.server import build_mcp

    server = build_mcp(_service())
    resources = {str(resource.uri) for resource in await server.list_resources()}
    assert {'can://status', 'can://identifiers', 'can://frames', 'can://database'} <= resources
    prompts = {prompt.name for prompt in await server.list_prompts()}
    assert {'reverse_engineer_identifier', 'diagnose_trouble_codes', 'find_signal'} <= prompts


def test_cli_help_without_mcp_sdk():
    """The command's --help works even when the MCP SDK is not installed (it is imported lazily)."""
    result = CliRunner().invoke(main, ['--help'])
    assert result.exit_code == 0
    assert '--allow-send' in result.output and '--interface' in result.output
