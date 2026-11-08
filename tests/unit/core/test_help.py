"""Tests for the in-app help (can_explorer.core.help), including that its navigation matches mkdocs.yml."""

from pathlib import Path

import yaml

from can_explorer.core import help as help_module

ROOT = Path(__file__).resolve().parents[3]


def _nav_paths(nav) -> list:
    """The ordered (title_or_None, path) entries of an mkdocs nav tree."""
    entries = []
    for item in nav:
        if isinstance(item, str):
            entries.append((None, item))
        elif isinstance(item, dict):
            ((label, value),) = item.items()
            if isinstance(value, str):
                entries.append((label, value))
            else:
                entries.extend(_nav_paths(value))
    return entries


class _IgnoreUnknownTags(yaml.SafeLoader):
    """mkdocs.yml carries !!python/name: tags (Material emoji) that SafeLoader rejects; we only need the nav."""


_IgnoreUnknownTags.add_multi_constructor('tag:yaml.org,2002:python/name:', lambda loader, suffix, node: None)


def test_nav_matches_mkdocs():
    """HELP_NAV must list the same pages, in the same order, as mkdocs.yml — so the app and the website agree."""
    mkdocs = yaml.load((ROOT / 'mkdocs.yml').read_text(encoding='utf-8'), Loader=_IgnoreUnknownTags)
    expected = _nav_paths(mkdocs['nav'])
    actual = [(page.title, page.path) for section in help_module.HELP_NAV for page in section.pages]
    assert [path for _, path in actual] == [path for _, path in expected]
    # Where mkdocs gives an explicit title (not a bare path), the app's title must match it.
    for (label, path), (title, _) in zip(expected, actual, strict=True):
        if label is not None:
            assert title == label, path


def test_nav_sections_are_user_guide_only():
    """The in-app table of contents lists only the user guide (the full HELP_NAV still mirrors the website)."""
    assert [section.title for section in help_module.nav_sections()] == ['User guide']
    for section in help_module.nav_sections():
        assert all(page.path.startswith('user-guide/') for page in section.pages)


def test_page_blocks_split_standalone_images_from_text():
    blocks = help_module.page_blocks('user-guide/preferences.md')
    assert blocks and all(block['type'] in ('text', 'image') for block in blocks)
    images = [block for block in blocks if block['type'] == 'image']
    assert images and all(block['source'].startswith('file://') for block in images)
    assert any(block['type'] == 'text' and 'Preferences' in block['text'] for block in blocks)


def test_page_blocks_keep_inline_and_listed_images_in_text():
    """Only images that are the whole line become image blocks; indented ones stay in the surrounding text."""
    blocks = help_module.page_blocks('reference/links.md')
    assert all(block['type'] == 'text' for block in blocks)


def test_every_page_exists():
    missing = [page.path for page in help_module.pages() if not (help_module.docs_dir() / page.path).is_file()]
    assert missing == []


def test_tool_help_points_to_real_pages():
    known = {page.path for page in help_module.pages()}
    assert all(target in known for target in help_module.TOOL_HELP.values())
    assert help_module.resolve_tool('ethernet') == 'user-guide/ethernet.md'
    assert help_module.resolve_tool('nonsense') == 'user-guide/index.md'


def test_every_page_preprocesses_without_leftover_markup():
    for page in help_module.pages():
        rendered = help_module.load_page(page.path)
        assert '!!!' not in rendered and '???' not in rendered, page.path  # Admonitions converted
        assert '--8<--' not in rendered, page.path  # Snippet markers dropped
        assert '```mermaid' not in rendered, page.path  # Mermaid replaced by a fallback


def test_admonition_becomes_blockquote():
    rendered = help_module.load_page('user-guide/connections.md')
    assert '> **Note**' in rendered


def test_internal_links_become_help_scheme():
    rendered = help_module.load_page('user-guide/trace-and-overview.md')
    assert 'help:' in rendered  # At least one cross-reference rewritten for in-app navigation
    # No raw relative .md links survive (they would not resolve in the app).
    assert '](../' not in rendered and '](./' not in rendered


def test_images_become_file_urls():
    rendered = help_module.load_page('user-guide/trace-and-overview.md')
    assert '](file://' in rendered
    assert '](../assets' not in rendered


def test_mermaid_falls_back_to_online_link():
    rendered = help_module.load_page('development/architecture.md')
    assert 'view it in the online docs' in rendered
    assert help_module.SITE_URL in rendered


def test_online_url():
    assert help_module.online_url('index.md') == help_module.SITE_URL
    assert help_module.online_url('user-guide/ethernet.md') == help_module.SITE_URL + 'user-guide/ethernet/'


def test_search_finds_pages():
    help_module.reset_cache()
    results = help_module.search('ethernet')
    assert any(result['path'] == 'user-guide/ethernet.md' for result in results)
    assert help_module.search('') == []
    # A title hit ranks before a body-only hit.
    assert help_module.search('ethernet')[0]['title'].lower().startswith('ethernet')


def test_unknown_page_is_handled():
    assert 'Not found' in help_module.load_page('does/not/exist.md')
