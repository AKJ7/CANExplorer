"""The skills shipped in plugins/canexplorer must only name MCP tools the server really has, and the plugin
manifests must stay valid JSON in step with the package version."""

import json
import re
import tomllib
import uuid
from pathlib import Path

import pytest

from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.mcp.service import CanService

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / 'plugins' / 'canexplorer'
SKILLS = sorted((PLUGIN / 'skills').glob('*/SKILL.md'))
# Backticked identifiers that are arguments, values or field kinds, not tools
NOT_TOOLS = {
    'baseline_seconds', 'action_seconds', 'include_constant', 'min_confidence', 'byte_order', 'little_endian',
    'big_endian', 'extended', 'fields', 'path', 'start', 'series', 'min', 'max', 'comment', 'capabilities',
    'error_frames', 'checksum', 'counter', 'physical', 'enum', 'flag', 'multiplexer', 'constant', 'unknown',
    'freshness', 'mac', 'random',
}  # fmt: skip


def test_skills_exist():
    assert SKILLS


@pytest.mark.parametrize('skill', SKILLS, ids=lambda p: p.parent.name)
def test_skill_front_matter(skill):
    text = skill.read_text(encoding='utf-8')
    match = re.match(r'---\n(.*?)\n---\n', text, re.S)
    assert match, 'missing YAML front matter'
    assert f'name: {skill.parent.name}' in match.group(1)
    assert 'description:' in match.group(1)


async def _all_tool_names():
    from can_explorer.mcp.server import build_mcp

    server = build_mcp(CanService(LiveSource(BusConfig('virtual', f'skills-{uuid.uuid4().hex}')), allow_send=True))
    return {tool.name for tool in await server.list_tools()}


@pytest.mark.parametrize('skill', SKILLS, ids=lambda p: p.parent.name)
async def test_skill_names_only_real_tools(skill):
    tools = await _all_tool_names()
    named = set(re.findall(r'`([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[a-z]+)`', skill.read_text(encoding='utf-8')))
    candidates = {name for name in named if '_' in name or name in tools} - NOT_TOOLS
    assert candidates, 'the skill names no tools'
    assert candidates <= tools, f'unknown tools: {sorted(candidates - tools)}'


def test_manifests_valid_and_versioned():
    marketplace = json.loads((ROOT / '.claude-plugin' / 'marketplace.json').read_text(encoding='utf-8'))
    plugin = json.loads((PLUGIN / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    assert [entry['source'] for entry in marketplace['plugins']] == ['./plugins/canexplorer']
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    assert plugin['version'] == version
