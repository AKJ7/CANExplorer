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
    'j1939_request',
    'uds_read_dtcs',
    'uds_dtc_details',
    'uds_find_ecus',
    'xcp_connect',
    'xcp_disconnect',
    'xcp_status',
    'xcp_read',
    'xcp_read_memory',
    'xcp_measure',
    'xcp_daq_info',
    'xcp_flash_info',
}
CONTROL_TOOLS = {
    'obd_clear',
    'uds_reset',
    'uds_routine',
    'uds_request',
    'uds_secured_request',
    'uds_authenticate',
    'uds_deauthenticate',
    'uds_transmit_certificate',
    'xcp_write',
    'xcp_write_memory',
    'xcp_stimulate',
    'xcp_program',
}
SEND_TOOLS = {'send_frame', 'send_message', 'j1939_send'}


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
        'bit_activity',
        'extract_field',
        'related_signals',
        'capture_reaction',
        'mark',
        'clear_markers',
        'reaction_at_markers',
        'load_database',
        'unload_database',
        'secoc_status',
        'correlate',
        'promote_to_dbc',
        'list_interfaces',
        'flexray_load_cluster',
        'flexray_read_log',
        'flexray_analyze_log',
        'flexray_decode',
        'xcp_list_variables',
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


async def test_everything_has_a_name_title_and_description():
    """An agent picks tools by their description and a client shows people the title: every tool, resource and
    prompt (of every tier) carries both, and each gated tool says which tier it needs."""
    pytest.importorskip('mcp')
    from can_explorer.mcp.server import build_mcp

    server = build_mcp(_service(allow_send=True))  # Every tier
    tools = await server.list_tools()
    resources = await server.list_resources()
    prompts = await server.list_prompts()
    for item in [*tools, *resources, *prompts]:
        assert item.name and item.title and item.description, item.name
        assert len(item.description) > 20, item.name
    for tool in tools:
        if tool.name in DIAGNOSTIC_READS:
            assert '--allow-diagnostics' in tool.description, tool.name
        elif tool.name in CONTROL_TOOLS:
            assert '--allow-control' in tool.description, tool.name
        elif tool.name in SEND_TOOLS:
            assert '--allow-send' in tool.description, tool.name
        else:
            assert '--allow-' not in tool.description, tool.name
    # Resources are named for what they hold, not after the Python function behind them
    by_uri = {str(resource.uri): resource.name for resource in resources}
    assert by_uri == {
        'can://status': 'status',
        'can://identifiers': 'identifiers',
        'can://frames': 'frames',
        'can://database': 'database',
    }
    # Titles are distinct, so a client listing them can tell them apart
    titles = [tool.title for tool in tools]
    assert len(titles) == len(set(titles))


def test_cli_help_is_fast():
    """The command's --help works without building the server (the server is imported lazily inside main)."""
    result = CliRunner().invoke(main, ['--help'])
    assert result.exit_code == 0
    assert '--allow-send' in result.output and '--interface' in result.output
